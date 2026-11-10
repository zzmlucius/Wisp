import contextlib
import csv
import io
import json
import os
from pathlib import Path
import random
import signal
import sys
import time
import unittest
from unittest.mock import patch

from compilebench_runner.cli import experiment, load_config, main
from compilebench_runner.control import BatchScheduler, ControlError, POLICIES
from compilebench_runner.interrupts import Interrupts
from compilebench_runner.policy import PolicyError, Scheduler
import test_compilebench as single_tests

FakeScheduler = single_tests.FakeScheduler


class FakeBatchScheduler(FakeScheduler):
    instances = []
    fail_policy = None
    cancel_policy = None

    def __init__(self, specs, directory, interrupts, **kwargs):
        super().__init__()
        self.specs, self.interrupts = specs, interrupts
        self.info = {"status": "not_started"}
        self.selected = []
        self.instances.append(self)

    def open(self):
        self.info["status"] = "ready"

    def select(self, policy, logfile):
        self.selected.append(policy)
        if policy == self.fail_policy:
            raise PolicyError("injected scheduler loading failure")
        if policy != "default":
            self.state.update(state="enabled", ops=self.specs[policy]["ops"], switch_all="1",
                              enable_seq=str(int(self.state["enable_seq"]) + 1))
        if policy == self.cancel_policy:
            self.interrupts.requested = True
            raise KeyboardInterrupt
        return self.snapshot()

    def stop(self):
        self.state.update(state="disabled", ops=None, switch_all="0")

    def close(self):
        self.stop()
        self.info["status"] = "closed"


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.fixture = single_tests.BenchmarkTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.base, self.config_path = self.fixture.base, self.fixture.config_path
        cfg = json.loads(self.config_path.read_text())
        cfg["schedulers"] = {name: {"command": [sys.executable], "ops": name[4:] + "_test"}
                             for name in POLICIES[1:]}
        self.config_path.write_text(json.dumps(cfg))
        FakeBatchScheduler.fail_policy = FakeBatchScheduler.cancel_policy = None

    def run_batch(self, rounds, seed=431, interrupts=None):
        with patch("compilebench.cli.Scheduler", FakeScheduler), patch("compilebench.control.BatchScheduler", FakeBatchScheduler):
            return experiment(load_config(self.config_path), interrupts, rounds=rounds, seed=seed)

    def test_two_rounds_have_eight_rows_in_saved_random_order(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            self.assertEqual(self.run_batch(2), 0)
        self.assertEqual(output.getvalue(), "")
        batch = self.fixture.batch()
        metadata = json.loads((batch / "metadata.json").read_text())
        rows = self.fixture.rows()
        self.assertEqual(len(rows), 8)
        rng = random.Random(431)
        for run, order in enumerate(metadata["orders"], 1):
            expected = list(POLICIES)
            rng.shuffle(expected)
            self.assertEqual(order, expected)
            block = [row for row in rows if int(row["run"]) == run]
            self.assertEqual([row["policy"] for row in block], order)
            self.assertTrue(all(row["valid"] == "True" for row in block))
            for policy in order:
                attempt = batch / f"run-{run:03d}" / policy
                self.assertTrue((attempt / "build.log").exists())
                self.assertTrue((attempt / "snapshot.json").exists())
        self.assertEqual(metadata["seed"], 431)
        self.assertEqual(metadata["final_scheduler"]["state"], "disabled")
        self.assertEqual(len(list((self.base / "builds").glob("*/run-*"))), 8)
        self.assertEqual(FakeBatchScheduler.instances[-1].info["status"], "closed")

    def test_one_round_retains_loading_failure_and_runs_remaining_policies(self):
        FakeBatchScheduler.fail_policy = "scx_bpfland"
        self.assertEqual(self.run_batch(1), 1)
        rows = self.fixture.rows()
        self.assertEqual(len(rows), 4)
        failed, = [row for row in rows if row["valid"] == "False"]
        self.assertEqual(failed["policy"], "scx_bpfland")
        self.assertEqual(failed["wall_s"], "")
        self.assertIn("loading failure", failed["error"])
        self.assertIsNone(json.loads((self.fixture.batch() / "metadata.json").read_text())["fatal_error"])

    def test_batch_interrupt_saves_attempt_and_restores_default(self):
        order = list(POLICIES)
        random.Random(431).shuffle(order)
        FakeBatchScheduler.cancel_policy = order[0]
        self.assertEqual(self.run_batch(2), 130)
        row, = self.fixture.rows()
        self.assertEqual(row["valid"], "False")
        metadata = json.loads((self.fixture.batch() / "metadata.json").read_text())
        self.assertEqual(metadata["status"], "interrupted")
        self.assertEqual(metadata["final_scheduler"]["state"], "disabled")

    def test_external_scheduler_is_refused_before_authorization(self):
        observer = FakeScheduler()
        observer.state.update(state="enabled", ops="external", switch_all="1")
        with patch("compilebench.cli.Scheduler", return_value=observer), patch("compilebench.control.BatchScheduler") as controller:
            with self.assertRaisesRegex(PolicyError, "外部 scx"):
                experiment(load_config(self.config_path), rounds=1)
            controller.assert_not_called()

    def test_batch_holds_one_config_and_source_snapshot(self):
        self.fixture.script.write_text("import pathlib\npathlib.Path('modified-source').write_text('changed')\n")
        self.assertEqual(self.run_batch(1), 1)
        rows = self.fixture.rows()
        self.assertEqual(len(rows), 4)
        self.assertTrue(all(row["valid"] == "False" for row in rows))
        self.assertTrue(all("源码输入" in row["error"] for row in rows))

    def test_positive_integer_dispatches_to_batch_mode(self):
        with patch("compilebench.cli.acquire_lock", return_value=None), patch("compilebench.cli.load_config", return_value={}), patch("compilebench.cli.experiment", return_value=0) as run:
            self.assertEqual(main(["3"]), 0)
            self.assertEqual(run.call_args.kwargs["rounds"], 3)


class WorkerTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "sched_ext"
        (self.root / "root").mkdir(parents=True)
        for field, value in (("state", "disabled"), ("enable_seq", "0"), ("switch_all", "0")):
            (self.root / field).write_text(value)
        program = """import os,pathlib,signal,sys,time
r=pathlib.Path(sys.argv[1]);ops=sys.argv[2]
def stop(*args):
 (r/'state').write_text('disabled');(r/'switch_all').write_text('0');(r/'root/ops').unlink(missing_ok=True)
 raise SystemExit(0)
signal.signal(signal.SIGINT,stop);signal.signal(signal.SIGTERM,stop)
(r/'child.pid').write_text(str(os.getpid()));(r/'root/ops').write_text(ops)
(r/'enable_seq').write_text(str(int((r/'enable_seq').read_text())+1))
(r/'switch_all').write_text('1');(r/'state').write_text('enabled')
while True: time.sleep(.05)
"""
        self.specs = {name: {"command": [sys.executable, "-c", program, str(self.root), name[4:]], "ops": name[4:]}
                      for name in POLICIES[1:]}

    def controller(self, prefix=None, **kwargs):
        kwargs.setdefault("inhibit", False)
        controller = BatchScheduler(self.specs, self.base, Interrupts(), prefix=prefix or [], timeout=2, settle=0, **kwargs)
        controller.root = self.root
        self.addCleanup(controller.close)
        return controller

    def wait_child(self):
        deadline = time.monotonic() + 3
        while not (self.root / "child.pid").exists() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue((self.root / "child.pid").exists())
        return int((self.root / "child.pid").read_text())

    def test_credential_expiration_cannot_interrupt_three_rounds(self):
        sudo = self.base / "sudo"
        calls, token = self.base / "calls", self.base / "token"
        sudo.write_text(f"#!{sys.executable}\nimport os,pathlib,sys\n"
            f"p=pathlib.Path({str(token)!r});calls=pathlib.Path({str(calls)!r})\n"
            "with calls.open('a') as f:f.write(sys.argv[1]+'\\n')\n"
            "if sys.argv[1]=='-v':p.touch();sys.exit(0)\n"
            "if not p.exists():sys.exit(1)\n"
            "p.unlink();os.execv(sys.argv[2],sys.argv[2:])\n")
        sudo.chmod(0o755)
        controller = self.controller(prefix=[str(sudo), "-n"])
        controller.open()
        self.assertFalse(token.exists())
        for run in range(3):
            self.assertFalse(controller.call("status")["running"])  # Default policy.
            for index in range(3):
                controller.call("start", index=index)
                self.wait_child()
                self.assertTrue(controller.call("status")["running"])
                self.assertFalse(controller.call("stop")["unexpected_exit"])
                (self.root / "child.pid").unlink()
        with self.assertRaisesRegex(ControlError, "重复授权"):
            controller.open()
        controller.close()
        self.assertEqual(calls.read_text().splitlines(), ["-v", "-n"])

    def test_eof_cleans_only_its_running_child(self):
        controller = self.controller()
        controller.open()
        controller.call("start", index=0)
        pid = self.wait_child()
        controller.close()
        self.assertFalse(Path(f"/proc/{pid}").exists())
        self.assertEqual((self.root / "state").read_text(), "disabled")

    def test_sleep_inhibitor_spans_all_switches_and_releases_on_close(self):
        guard = self.base / "systemd-inhibit"
        marker = self.base / "sleep-inhibition-held"
        args = self.base / "inhibitor-arguments"
        guard.write_text(f"#!{sys.executable}\nimport json,pathlib,subprocess,sys\n"
            f"marker=pathlib.Path({str(marker)!r})\n"
            f"pathlib.Path({str(args)!r}).write_text(json.dumps(sys.argv[1:]))\n"
            "marker.touch()\n"
            "try:\n"
            " process=subprocess.Popen(sys.argv[sys.argv.index('--')+1:])\n"
            " code=process.wait()\n"
            "finally:marker.unlink()\n"
            "sys.exit(code)\n")
        guard.chmod(0o755)
        controller = self.controller(inhibit=True)
        with patch("compilebench.control.shutil.which", return_value=str(guard)):
            controller.open()
        self.assertTrue(marker.exists())
        self.assertEqual(controller.info["sleep_inhibition"]["status"], "held")
        for index in range(3):
            controller.call("start", index=index)
            self.wait_child()
            controller.call("stop")
            (self.root / "child.pid").unlink()
            self.assertTrue(marker.exists())
        argv = json.loads(args.read_text())
        self.assertIn("--no-ask-password", argv)
        self.assertIn("--what=idle:sleep", argv)
        self.assertIn("--mode=block", argv)
        controller.close()
        self.assertFalse(marker.exists())
        self.assertEqual(controller.info["sleep_inhibition"]["status"], "released")

    def test_missing_sleep_inhibitor_fails_before_authorization(self):
        controller = self.controller(inhibit=True)
        with patch("compilebench.control.shutil.which", return_value=None), patch("compilebench.control.subprocess.run") as authorize:
            with self.assertRaisesRegex(ControlError, "systemd-inhibit"):
                controller.open()
            authorize.assert_not_called()
        self.assertIsNone(controller.process)

    def test_sleep_inhibition_denied_never_launches_a_scheduler(self):
        guard = self.base / "systemd-inhibit"
        guard.write_text(f"#!{sys.executable}\nimport sys\nprint('Failed to inhibit: denied',file=sys.stderr)\nsys.exit(1)\n")
        guard.chmod(0o755)
        controller = self.controller(inhibit=True)
        with patch("compilebench.control.shutil.which", return_value=str(guard)):
            with self.assertRaises(ControlError):
                controller.open()
        with self.assertRaises(ControlError):
            controller.close()
        self.assertFalse((self.root / "child.pid").exists())
        self.assertEqual(controller.info["sleep_inhibition"]["status"], "not_acquired")

    def test_unregistered_commands_are_rejected(self):
        controller = self.controller()
        controller.open()
        with self.assertRaisesRegex(ControlError, "unregistered"):
            controller.call("start", index=99)
        with self.assertRaisesRegex(ControlError, "unknown action"):
            controller.call("exec", command=["/bin/sh"])

    def test_sigint_cleanup_is_not_reported_as_a_supervisor_failure(self):
        controller = self.controller()
        controller.open()
        controller.call("start", index=0)
        controller.active = True
        pid = self.wait_child()
        controller.interrupts.requested = True
        os.kill(controller.process.pid, signal.SIGINT)
        self.assertEqual(controller.process.wait(timeout=10), 0)
        self.assertIsNone(controller.stop())
        self.assertFalse(Path(f"/proc/{pid}").exists())

    def test_interrupt_during_default_policy_closes_worker_without_error(self):
        controller = self.controller()
        controller.open()
        controller.interrupts.requested = True
        self.assertIsNone(controller.stop())
        controller.verify(controller.initial())
        self.assertEqual(controller.info["status"], "closed")

    def test_loading_waits_until_enable_sequence_is_stable(self):
        controller = self.controller()
        controller.settle = .12
        controller.open()
        # An extra thread changes fake sysfs after the worker begins loading.
        import threading
        def restart():
            self.wait_child()
            time.sleep(.08)
            (self.root / "enable_seq").write_text("2")
        thread = threading.Thread(target=restart)
        thread.start()
        try:
            with patch.object(controller, "verify", side_effect=lambda state, pid=None: Scheduler.verify(controller, state)):
                state = controller.select("scx_lavd", self.base / "scheduler.log")
            self.assertEqual(state["enable_seq"], "2")
        finally:
            thread.join()
        self.assertIsNone(controller.stop())
        self.assertTrue((self.base / "scheduler.log").exists())
