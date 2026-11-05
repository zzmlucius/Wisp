import contextlib
import csv
import io
import json
import os
from pathlib import Path
import random
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import schbench_runner
from schbench_runner import cli, cleanup
from schbench_runner.results import FIELDS, assess, parse_results, summarize
from compilebench.control import ControlError, POLICIES
from compilebench.interrupts import Interrupts
from compilebench.policy import PolicyError


HELP = "schbench usage: -m -t -r -F -n -s -w -z -i -R -L -j (--json)"
TEXT = """Wakeup Latencies percentiles (usec) runtime 1 (s) (100 total samples)
  50.0th: 1 (50 samples)
* 99.0th: 7 (49 samples)
  99.9th: 99 (1 samples)
Request Latencies percentiles (usec) runtime 1 (s) (100 total samples)
  50.0th: 10 (50 samples)
* 99.0th: 250 (49 samples)
  99.9th: 999 (1 samples)
RPS percentiles (requests) runtime 1 (s) (1 total samples)
* 50.0th: 800 (1 samples)
average rps: 1000.00
"""
NATIVE = {"normal": {"version": "this-is-a-kernel-release"},
          "int": {"wakeup_latency_pct99.0": 7, "request_latency_pct99.0": 250, "runtime": 1}}


class FakeScheduler:
    def __init__(self):
        self.state = {"state": "disabled", "ops": None, "switch_all": "0", "enable_seq": "0"}

    def snapshot(self):
        return self.state.copy()

    def initial(self):
        return self.snapshot()

    def verify(self, expected, pid=None):
        if self.snapshot() != expected:
            raise PolicyError("策略变化")


class FakeBatch(FakeScheduler):
    fail = fatal = cancel = None
    opens = 0
    last = None

    def __init__(self, specs, directory, interrupts, **kwargs):
        super().__init__()
        self.specs, self.interrupts = specs, interrupts
        self.info = {"status": "not_started"}
        self.current = None
        self.last = self
        type(self).last = self

    def open(self):
        type(self).opens += 1
        self.info["status"] = "ready"

    def select(self, policy, logfile):
        self.current = policy
        if policy != "default":
            Path(logfile).write_text("fake scheduler test evidence\n")
            self.state.update(state="enabled", ops=self.specs[policy]["ops"], switch_all="1",
                              enable_seq=str(int(self.state["enable_seq"]) + 1))
        if policy == self.fail:
            raise PolicyError("injected loading failure")
        if policy == self.cancel:
            self.interrupts.requested = True
            raise KeyboardInterrupt
        return self.snapshot()

    def stop(self):
        self.state.update(state="disabled", ops=None, switch_all="0")
        if self.current == self.fatal:
            raise ControlError("injected controller loss")

    def close(self):
        self.state.update(state="disabled", ops=None, switch_all="0")
        self.info["status"] = "closed"


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="schtest-tests-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.program = self.root / "native"
        self.config = self.root / "schtest.json"
        self.config.write_text(json.dumps({
            "program": str(self.program), "cpus": ",".join(map(str, sorted(os.sched_getaffinity(0)))),
            "workload": {"runtime_s": 1, "worker_threads": 1},
            "results_dir": "records", "cooldown_s": 0, "sample_interval_s": 0.03,
            "rps": 2000, "schedulers": {name: {"command": [sys.executable], "ops": name[4:] + "_test"}
                                          for name in POLICIES[1:]}}))
        self.write_program()
        self.patchers = [patch.object(cli, "PROJECT", self.root), patch.object(cleanup, "PROJECT", self.root)]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        FakeBatch.fail = FakeBatch.fatal = FakeBatch.cancel = None
        FakeBatch.opens = 0

    def write_program(self, extra="", exit_code=0, json_support=True, sleep=0.1):
        help_text = HELP if json_support else HELP.replace("-j (--json)", "")
        self.program.write_text(f"#!{sys.executable}\nimport sys,time,json,os\nfrom pathlib import Path\n"
            f"if '--help' in sys.argv:\n print({help_text!r}); sys.exit(1)\n"
            f"time.sleep({sleep})\n{extra}\n"
            f"print({TEXT!r})\n"
            f"if '-j' in sys.argv: Path(sys.argv[sys.argv.index('-j')+1]).write_text({json.dumps(NATIVE)!r})\n"
            f"sys.exit({exit_code})\n")
        self.program.chmod(0o755)

    def change_config(self, **values):
        obj = json.loads(self.config.read_text())
        obj.update(values)
        self.config.write_text(json.dumps(obj))

    def load(self, **kwargs):
        return cli.load_config(self.config, **kwargs)

    def batch(self):
        return next((self.root / "records").iterdir())

    def rows(self):
        with (self.batch() / "summary.csv").open() as stream:
            return list(csv.DictReader(stream))

    def run_experiment(self, fixed=False, rounds=None, observer=None):
        observer = observer or FakeScheduler()
        with patch.object(cli, "Scheduler", return_value=observer), patch.object(cli, "BatchScheduler", FakeBatch):
            return cli.experiment(self.load(fixed=fixed), rounds=rounds, seed=42)

    def isolated_cli(self):
        original = Path(cli.__file__).resolve().parents[1]
        project = self.root / "isolated/Schbench"
        project.mkdir(parents=True)
        (project.parent / "Compile").symlink_to(original.parent / "Compile", target_is_directory=True)
        shutil.copytree(original / "schbench_runner", project / "schbench_runner", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copyfile(original / "schtest", project / "schtest")
        obj = json.loads(self.config.read_text())
        obj["results_dir"] = str(self.root / "records")
        (project / "schtest.json").write_text(json.dumps(obj))
        return project / "schtest"


class ParserTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.log = Path(self.temp.name) / "schbench.log"
        self.native = self.log.with_name("native.json")
        self.log.write_text(TEXT)
        self.native.write_text(json.dumps(NATIVE))

    def test_final_average_is_not_rps_median(self):
        result, _ = parse_results(self.log, self.native, runtime=1)
        self.assertEqual(result, {"avg_rps": 1000, "request_p99_us": 250, "wakeup_p99_us": 7})

    def test_real_upstream_typed_json_and_log_fixture(self):
        fixture = Path(__file__).parent / "fixtures"
        result, _ = parse_results(fixture / "upstream.log", fixture / "upstream.json", runtime=2)
        self.assertEqual(result, {"avg_rps": 828.5, "request_p99_us": 6344, "wakeup_p99_us": 155})

    def test_final_percentiles_are_not_window_averages(self):
        self.log.write_text(TEXT.replace("250", "50000").replace("7 (", "7000 (") + TEXT)
        result, _ = parse_results(self.log, runtime=1)
        self.assertEqual(result["request_p99_us"], 250)
        self.assertEqual(result["wakeup_p99_us"], 7)

    def test_missing_average_does_not_fall_back_to_rps_percentile(self):
        self.log.write_text(TEXT.replace("average rps: 1000.00", "current rps: 1000.00"))
        with self.assertRaisesRegex(ValueError, "average rps"):
            parse_results(self.log)

    def test_99_9_is_not_99(self):
        self.log.write_text(TEXT.replace("* 99.0th:", "* 98.0th:"))
        with self.assertRaisesRegex(ValueError, "p99"):
            parse_results(self.log)

    def test_json_mismatch_or_invalid_json_fails(self):
        for text in ('{bad', json.dumps({**NATIVE, "int": {**NATIVE["int"], "request_latency_pct99.0": 100}})):
            self.native.write_text(text)
            with self.assertRaises(ValueError):
                parse_results(self.log, self.native)

    def test_wrong_runtime_or_zero_samples_fails(self):
        with self.assertRaises(ValueError):
            parse_results(self.log, runtime=2)
        self.log.write_text(TEXT.replace("100 total samples", "0 total samples"))
        with self.assertRaises(ValueError):
            parse_results(self.log)

    def test_nonfinite_metric_fails(self):
        self.log.write_text(TEXT.replace("1000.00", "1e999"))
        with self.assertRaises(ValueError):
            parse_results(self.log)


class ConfigTests(Fixture):
    def test_cpu_ranges_are_explicit_and_checked(self):
        self.assertEqual(cli.cpuset("0-2,4,2"), [0, 1, 2, 4])
        self.assertEqual(cli.cpuset([4, 1, 4]), [1, 4])
        for text in ("auto", "2-1", "0-1024", "-1", "", [True], []):
            with self.subTest(text=text), self.assertRaises(ValueError):
                cli.cpuset(text)

    def test_missing_fixed_rps_rejected_before_sudo(self):
        self.change_config(rps=None)
        with self.assertRaisesRegex(ValueError, "rps"):
            self.load(fixed=True)

    def test_fixed_rps_uses_integer_per_message_thread(self):
        self.change_config(rps=101, workload={"message_threads": 2})
        with self.assertRaisesRegex(ValueError, "整除"):
            self.load(fixed=True)

    def test_mode_command_and_json_capability(self):
        cfg = self.load()
        command = cli.native_command(cfg, self.root)
        self.assertNotIn("-R", command)
        self.assertNotIn("-A", command)
        self.assertEqual(command[command.index("-w")+1], "0")
        self.assertEqual(command[command.index("-z")+1], "0")
        cfg = self.load(fixed=True, rps=500)
        command = cli.native_command(cfg, self.root)
        self.assertEqual(command[command.index("-R")+1], "500")
        self.assertIn("-j", command)
        self.write_program(json_support=False)
        self.assertNotIn("-j", cli.native_command(self.load(), self.root))

    def test_no_calibration_pipe_or_stat_reset(self):
        for workload in ({"warmup_s": 1}, {"zero_interval_s": 1}, {"calibrate": True}, {"args": ["-p", "1"]}, {"locking": "false"}):
            self.change_config(workload=workload)
            with self.subTest(workload=workload), self.assertRaises(ValueError):
                self.load()

    def test_invalid_timing_and_targets_rejected(self):
        for values in ({"sample_interval_s": 0}, {"cooldown_s": -1}, {"rps_tolerance": 1}, {"rps": True}, {"request_p99_limit_us": -1}):
            original = self.config.read_bytes()
            self.change_config(**values)
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.load()
            self.config.write_bytes(original)

    def test_output_override_does_not_change_live_config(self):
        original = self.config.read_bytes()
        cfg = self.load(output=self.root / "custom-output")
        self.assertEqual(cfg["results_dir"], str(self.root / "custom-output"))
        self.assertEqual(self.config.read_bytes(), original)
        with self.assertRaises(ValueError):
            self.load(output=self.root)

    def test_rps_without_mode_rejected(self):
        with self.assertRaises(ValueError):
            self.load(rps=100)

    def test_effective_frozen_settings_can_be_loaded_again(self):
        cfg = self.load(fixed=True)
        copied = self.root / "frozen.json"
        copied.write_text(json.dumps(cli.effective(cfg)))
        loaded = cli.load_config(copied, fixed=True)
        self.assertEqual(cli.effective(loaded), cli.effective(cfg))


class ExecutionTests(Fixture):
    def test_actual_cli_exits_silently_and_keeps_default_scheduler(self):
        if Path("/sys/kernel/sched_ext/state").read_text().strip() != "disabled":
            self.skipTest("requires default scheduler")
        entry = self.isolated_cli()
        result = subprocess.run([sys.executable, str(entry)], capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(result.stdout, b"")
        self.assertEqual(result.stderr, b"")
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(Path("/sys/kernel/sched_ext/state").read_text().strip(), "disabled")

    def test_actual_cli_interrupt_reaps_its_workload_group(self):
        if Path("/sys/kernel/sched_ext/state").read_text().strip() != "disabled":
            self.skipTest("requires default scheduler")
        pidfile = self.root / "descendant.pid"
        self.write_program(sleep=0, extra="import subprocess\n"
            f"child = subprocess.Popen([{sys.executable!r}, '-c', 'import time; time.sleep(30)'])\n"
            f"Path({str(pidfile)!r}).write_text(str(child.pid))\ntime.sleep(30)")
        entry = self.isolated_cli()
        process = subprocess.Popen([sys.executable, str(entry)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 10
            while not pidfile.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(pidfile.exists())
            process.send_signal(signal.SIGINT)
            stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 130, stderr.decode())
            self.assertEqual(stdout, b"")
            row, = self.rows()
            self.assertEqual(row["valid"], "False")
            self.assertIn("中断", row["error"])
            pid = int(pidfile.read_text())
            stat = Path(f"/proc/{pid}/stat")
            if stat.exists():
                self.assertIn(stat.read_text().rsplit(")", 1)[-1].split()[0], {"Z", "X"})
            self.assertEqual(Path("/sys/kernel/sched_ext/state").read_text().strip(), "disabled")
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()

    def test_all_four_entry_modes_and_silent_output(self):
        for args, mode, count in (([], "best_effort", 1), (["1"], "best_effort", 4),
                                  (["-R"], "fixed_rps", 1), (["-R", "1"], "fixed_rps", 4)):
            with self.subTest(args=args):
                root = self.root / ("output-" + str(count) + mode)
                output = io.StringIO()
                with patch.object(cli, "Scheduler", FakeScheduler), patch.object(cli, "BatchScheduler", FakeBatch), contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                    self.assertEqual(cli.main([*args, "--output", str(root)]), 0)
                self.assertEqual(output.getvalue(), "")
                batch = next(root.iterdir())
                with (batch / "summary.csv").open() as stream:
                    rows = list(csv.DictReader(stream))
                self.assertEqual(len(rows), count)
                self.assertEqual({r["run"] for r in rows}, {"1"})
                self.assertTrue(all(r["valid"] == "True" for r in rows))
                self.assertEqual(list(rows[0]), FIELDS)
                self.assertEqual(json.loads((batch / "schtest.json").read_text())["mode"], mode)

    def test_three_rounds_are_complete_random_blocks_with_one_open(self):
        self.assertEqual(self.run_experiment(rounds=3), 0)
        metadata = json.loads((self.batch() / "metadata.json").read_text())
        rng, orders = random.Random(42), []
        for _ in range(3):
            order = list(POLICIES)
            rng.shuffle(order)
            orders.append(order)
        self.assertEqual(metadata["orders"], orders)
        self.assertEqual([r["policy"] for r in self.rows()], sum(orders, []))
        self.assertEqual(FakeBatch.opens, 1)
        self.assertEqual(metadata["final_scheduler"]["state"], "disabled")
        self.assertEqual(len(self.rows()), 12)

    def test_borrowed_unknown_scheduler_remains_untouched(self):
        observer = FakeScheduler()
        observer.state.update(state="enabled", ops="custom_test", switch_all="1")
        original = observer.snapshot()
        self.assertEqual(self.run_experiment(observer=observer), 0)
        self.assertEqual(observer.snapshot(), original)
        self.assertTrue((self.batch() / "run-001/current/attempt.json").exists())
        self.assertEqual(self.rows()[0]["policy"], "custom_test")
        self.assertEqual(FakeBatch.opens, 0)

    def test_external_scx_refused_before_authorization(self):
        observer = FakeScheduler()
        observer.state.update(state="enabled", ops="rusty_test", switch_all="1")
        with self.assertRaises(PolicyError):
            self.run_experiment(rounds=1, observer=observer)
        self.assertEqual(FakeBatch.opens, 0)

    def test_loading_failure_retains_row_and_continues(self):
        FakeBatch.fail = "scx_rusty"
        self.assertEqual(self.run_experiment(rounds=1), 1)
        rows = self.rows()
        self.assertEqual(len(rows), 4)
        bad = next(r for r in rows if r["policy"] == "scx_rusty")
        self.assertEqual(bad["valid"], "False")
        self.assertEqual(bad["wall_s"], "")
        self.assertEqual(FakeBatch.last.snapshot()["state"], "disabled")

    def test_native_failure_is_retained(self):
        self.write_program(exit_code=7)
        self.assertEqual(self.run_experiment(), 1)
        self.assertEqual(self.rows()[0]["exit_code"], "7")
        self.assertEqual(self.rows()[0]["valid"], "False")

    def test_missing_required_metrics_invalidates_successful_exit(self):
        self.write_program(extra="sys.exit(0)")
        self.assertEqual(self.run_experiment(), 1)
        row, = self.rows()
        self.assertEqual(row["exit_code"], "0")
        self.assertEqual(row["avg_rps"], "")
        self.assertIn("解析失败", row["error"])

    def test_under_target_is_valid_but_not_eligible(self):
        self.assertEqual(self.run_experiment(fixed=True, rounds=1), 0)
        self.assertTrue(all(r["valid"] == "True" for r in self.rows()))
        details = json.loads((self.batch() / "run-001/default/attempt.json").read_text())
        self.assertFalse(details["constraints"]["met"])
        analysis = json.loads((self.batch() / "metadata.json").read_text())["analysis"]
        self.assertEqual(analysis["policies"]["scx_rusty"]["paired"], [])

    def test_configuration_drift_invalidates_run(self):
        self.write_program(extra=f"Path({str(self.config)!r}).write_text('{{}}')")
        self.assertEqual(self.run_experiment(), 1)
        self.assertIn("快照不匹配", self.rows()[0]["error"])

    def test_binary_change_before_attempt_is_not_executed(self):
        cfg = self.load()
        original = cli.save_inputs
        def change(*args):
            result = original(*args)
            self.program.write_text(self.program.read_text() + "\n# changed\n")
            return result
        with patch.object(cli, "Scheduler", FakeScheduler), patch.object(cli, "save_inputs", change):
            self.assertEqual(cli.experiment(cfg), 1)
        self.assertEqual(self.rows()[0]["wall_s"], "")

    def test_native_cpu_affinity_is_applied_before_execution(self):
        cpu = min(os.sched_getaffinity(0))
        self.change_config(cpus=str(cpu))
        self.write_program(extra=f"assert os.sched_getaffinity(0) == {{{cpu}}}")
        self.assertEqual(self.run_experiment(), 0)

    def test_affinity_change_during_run_is_invalid(self):
        if len(os.sched_getaffinity(0)) < 2:
            self.skipTest("requires two CPUs")
        cpu = min(os.sched_getaffinity(0))
        self.write_program(extra=f"os.sched_setaffinity(0, {{{cpu}}}); time.sleep(0.15)")
        self.assertEqual(self.run_experiment(), 1)
        self.assertIn("affinity", self.rows()[0]["error"])

    def test_interrupt_stops_without_fabricating_remaining_rows(self):
        FakeBatch.cancel = "scx_bpfland"  # first in the seed=42 permutation
        self.assertEqual(self.run_experiment(rounds=3), 130)
        self.assertEqual(len(self.rows()), 1)
        metadata = json.loads((self.batch() / "metadata.json").read_text())
        self.assertEqual(metadata["status"], "interrupted")
        self.assertEqual(metadata["final_scheduler"]["state"], "disabled")

    def test_controller_loss_is_fatal_even_after_default_restored(self):
        FakeBatch.fatal = "scx_bpfland"
        self.assertEqual(self.run_experiment(rounds=2), 1)
        self.assertEqual(len(self.rows()), 1)
        metadata = json.loads((self.batch() / "metadata.json").read_text())
        self.assertIn("controller loss", metadata["fatal_error"])


class AnalysisTests(unittest.TestCase):
    def test_pairing_uses_same_round_valid_and_constraints(self):
        rows = [{"policy": "default", "run": 1, "valid": True, "avg_rps": 1000, "request_p99_us": 200},
                {"policy": "scx_rusty", "run": 1, "valid": True, "avg_rps": 1200, "request_p99_us": 150},
                {"policy": "scx_rusty", "run": 2, "valid": False, "avg_rps": 9000, "request_p99_us": 1}]
        flags = [{"met": True}] * 3
        result = summarize(rows, flags, "best_effort")["policies"]["scx_rusty"]
        self.assertEqual(result["paired"][0]["improvement_pct"], 20)
        result = summarize(rows, flags, "fixed_rps")["policies"]["scx_rusty"]
        self.assertEqual(result["paired"][0]["improvement_pct"], 25)
        self.assertEqual(result["attempted"], 2)
        flags[1] = {"met": False}
        self.assertEqual(summarize(rows, flags, "fixed_rps")["policies"]["scx_rusty"]["paired"], [])

    def test_latency_constraints_are_separate_from_validity(self):
        row = {"avg_rps": 1000, "request_p99_us": 500, "wakeup_p99_us": 5}
        cfg = {"mode": "best_effort", "request_p99_limit_us": 400, "wakeup_p99_limit_us": None}
        self.assertFalse(assess(row, cfg)["met"])


class CleanupTests(Fixture):
    def create_batch(self, root, name, owned=True):
        p = root / name
        p.mkdir(parents=True)
        (p / "metadata.json").write_text(json.dumps({"workload": "schbench" if owned else "compiletest"}))
        (p / "schtest.json").write_text("{}")
        return p

    def test_clean_selected_and_custom_output_preserves_everything_else(self):
        own = self.create_batch(self.root / "records", "one")
        kept = self.create_batch(self.root / "records", "two")
        foreign = self.create_batch(self.root / "records", "foreign", owned=False)
        custom = self.create_batch(self.root / "elsewhere", "custom")
        cleanup.register_root(custom.parent)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cleanup.main(["-c", str(self.config), "one", "custom", "--dry-run"]), 0)
            self.assertTrue(own.exists())
            self.assertEqual(cleanup.main(["-c", str(self.config), "one", "custom"]), 0)
        self.assertFalse(own.exists())
        self.assertFalse(custom.exists())
        self.assertTrue(kept.exists() and foreign.exists() and self.program.exists() and self.config.exists())

    def test_symlinks_and_unsafe_roots_cannot_erase_inputs(self):
        self.create_batch(self.root / "records", "one")
        (self.root / "records/link").symlink_to(self.root, target_is_directory=True)
        self.assertEqual(len(cleanup.plan(self.config, [])), 1)
        self.change_config(results_dir=".")
        with self.assertRaises(ValueError):
            cleanup.plan(self.config, [])

    def test_shared_mutex_prevents_clean_while_either_workload_runs(self):
        lock = cli.acquire_lock()
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cleanup.main(["-c", str(self.config)]), 2)
        finally:
            os.close(lock)

    def test_unknown_batch_fails_before_deleting_known_batch(self):
        own = self.create_batch(self.root / "records", "one")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cleanup.main(["-c", str(self.config), "one", "missing"]), 2)
        self.assertTrue(own.exists())


if __name__ == "__main__":
    unittest.main()
