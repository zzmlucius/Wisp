import contextlib
import csv
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from compilebench.cli import experiment, load_config, manifest, preread, main
from compilebench.interrupts import Interrupts
from compilebench.preflight import check_environment
from compilebench.metrics import BuildCgroup, is_delegated
from compilebench.policy import PolicyError, Scheduler, policy_name
from compilebench.system import power_state, power_differences, power_error
from compilebench.workload import measure

ROOT = Path(__file__).resolve().parents[1]


class FakeScheduler:
    """Read-only test double: it deliberately has no start/stop/sudo methods."""
    def __init__(self):
        self.state = {"state": "disabled", "ops": None, "enable_seq": "0", "switch_all": "0"}

    def snapshot(self):
        return self.state.copy()

    def initial(self):
        return self.snapshot()

    def verify(self, expected, pid=None):
        if self.state != expected:
            raise PolicyError("strategy changed")


class BenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="compiletest-tests-")
        self.base = Path(self.temp.name)
        self.source = self.base / "source"
        self.source.mkdir()
        self.config = self.base / "fixed/kernel.config"
        self.config.parent.mkdir()
        self.config.write_text("# fixed short-workload config\n")
        self.script = self.source / "task.py"
        self.script.write_text("import time\ntime.sleep(0.18)\n")
        (self.source / "Makefile").write_text(
            ".PHONY: olddefconfig vmlinux\nolddefconfig:\n\t@true\n"
            f"vmlinux:\n\t@{sys.executable} $(CURDIR)/task.py $(O)\n")
        self.config_path = self.base / "compiletest.json"
        self.config_path.write_text(json.dumps({
            "source": str(self.source), "kernel_config": str(self.config),
            "results_dir": str(self.base / "results"), "build_root": str(self.base / "builds"),
            "jobs": 2, "cooldown_s": 0, "sample_interval_s": 0.05, "required_tools": []}))

    def tearDown(self):
        self.temp.cleanup()

    def batch(self):
        return next((self.base / "results").iterdir())

    def rows(self):
        with (self.batch() / "summary.csv").open() as stream:
            return list(csv.DictReader(stream))

    def run_fake(self, cls=FakeScheduler):
        cfg = load_config(self.config_path)
        with patch("compilebench.cli.Scheduler", cls):
            return experiment(cfg)

    def test_single_build_is_silent_and_records_current_scheduler(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            self.assertEqual(self.run_fake(), 0)
        self.assertEqual(output.getvalue(), "")
        row, = self.rows()
        self.assertEqual(row["policy"], "default")
        self.assertEqual(row["valid"], "True")
        self.assertEqual(row["run"], "1")
        snapshot = json.loads((self.batch() / "snapshot.json").read_text())
        self.assertEqual(snapshot["parameters"]["jobs"], 2)
        self.assertEqual(snapshot["scheduler_name"], "default")
        self.assertEqual((self.batch() / "compiletest.json").read_bytes(), self.config_path.read_bytes())
        self.assertEqual((self.batch() / "kernel.config").read_bytes(), self.config.read_bytes())

    def test_external_scx_is_used_and_left_untouched(self):
        scheduler = FakeScheduler()
        scheduler.state.update(state="enabled", ops="lavd_1.1.4_x86_64_unknown_linux_gnu", switch_all="1")
        initial = scheduler.snapshot()
        with patch("compilebench.cli.Scheduler", return_value=scheduler):
            self.assertEqual(experiment(load_config(self.config_path)), 0)
        row, = self.rows()
        self.assertEqual(row["policy"], "scx_lavd")
        self.assertEqual(scheduler.snapshot(), initial)
        snapshot = json.loads((self.batch() / "snapshot.json").read_text())
        self.assertEqual(snapshot["scheduler"], initial)

    def test_invalid_command_line_arguments_are_rejected_before_start(self):
        for arguments in (["0"], ["-1"], ["1.5"], ["1", "2"], ["--help"], ["--check"], ["-c", str(self.config_path)]):
            with self.subTest(arguments=arguments), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(arguments), 2)
        self.assertFalse((self.base / "results").exists())

    def test_new_invocations_keep_separate_results_and_empty_build_directories(self):
        self.assertEqual(self.run_fake(), 0)
        self.assertEqual(self.run_fake(), 0)
        batches = list((self.base / "results").iterdir())
        self.assertEqual(len(batches), 2)
        builds = [json.loads((batch / "metadata.json").read_text())["build_dir"] for batch in batches]
        self.assertNotEqual(builds[0], builds[1])
        self.assertTrue(all(Path(build, ".config").is_file() for build in builds))

    def test_mid_build_settings_edit_invalidates_without_changing_saved_snapshot(self):
        self.script.write_text(f"import pathlib,time\np=pathlib.Path({str(self.config_path)!r})\np.write_text(p.read_text()+' ')\ntime.sleep(0.1)\n")
        before = self.config_path.read_bytes()
        self.assertEqual(self.run_fake(), 1)
        row, = self.rows()
        self.assertEqual(row["exit_code"], "0")
        self.assertIn("参数快照不匹配", row["error"])
        self.assertEqual((self.batch() / "compiletest.json").read_bytes(), before)

    def test_source_edit_during_build_invalidates_result(self):
        self.script.write_text("import pathlib\npathlib.Path('changed-input').write_text('new')\n")
        self.assertEqual(self.run_fake(), 1)
        row, = self.rows()
        self.assertIn("源码输入与参数快照不匹配", row["error"])

    def test_real_observer_rejects_reloaded_identical_scheduler(self):
        root = self.base / "sysfs"
        (root / "root").mkdir(parents=True)
        for field, value in (("state", "enabled"), ("root/ops", "bpfland_1.1.4"),
                             ("enable_seq", "2"), ("switch_all", "1")):
            (root / field).write_text(value)
        scheduler = Scheduler(root)
        before = scheduler.initial()
        self.assertEqual(policy_name(before), "scx_bpfland")
        (root / "enable_seq").write_text("6")
        with self.assertRaisesRegex(PolicyError, "enable_seq"):
            scheduler.verify(before)

    def test_actual_cli_interrupt_saves_invalid_row_and_preserves_scheduler(self):
        scheduler = Scheduler()
        before = scheduler.initial()
        if before["state"] != "disabled":
            self.skipTest("This integration check needs the default scheduler")
        self.script.write_text("import os,pathlib,sys,time\npathlib.Path(sys.argv[1], 'pid').write_text(str(os.getpid()))\ntime.sleep(60)\n")
        with (self.base / "cli.log").open("w") as log:
            process = subprocess.Popen([str(ROOT / "compiletest")], cwd=self.base,
                                       stdout=log, stderr=log, start_new_session=True)
            try:
                deadline = time.monotonic() + 10
                files = []
                while process.poll() is None and time.monotonic() < deadline:
                    files = list((self.base / "builds").glob("*/run-001-default/pid"))
                    if files:
                        break
                    time.sleep(0.02)
                self.assertTrue(files, (self.base / "cli.log").read_text())
                pid = int(files[0].read_text())
                process.send_signal(signal.SIGINT)
                self.assertEqual(process.wait(timeout=10), 130)
                row, = self.rows()
                self.assertEqual(row["valid"], "False")
                self.assertIn("中断", row["error"])
                self.assertFalse(Path(f"/proc/{pid}").exists())
                self.assertEqual(scheduler.snapshot(), before)
                self.assertEqual((self.base / "cli.log").read_text(), "")
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()

    def test_actual_cli_succeeds_silently_with_one_result(self):
        before = Scheduler().initial()
        if before["state"] != "disabled":
            self.skipTest("This integration check needs the default scheduler")
        result = subprocess.run([str(ROOT / "compiletest")], cwd=self.base,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout + result.stderr, "")
        row, = self.rows()
        self.assertEqual(row["valid"], "True")
        self.assertEqual(row["policy"], "default")
        self.assertEqual(Scheduler().snapshot(), before)

    def test_build_failure_is_retained(self):
        self.script.write_text("raise SystemExit(23)\n")
        self.assertEqual(self.run_fake(), 1)
        row, = self.rows()
        self.assertEqual(row["valid"], "False")
        self.assertNotEqual(row["exit_code"], "0")
        self.assertIn("编译失败", row["error"])
        self.assertGreater(float(row["wall_s"]), 0)

    def test_install_targets_rejected(self):
        cfg = json.loads(self.config_path.read_text())
        cfg["targets"] = ["modules_install"]
        self.config_path.write_text(json.dumps(cfg))
        with self.assertRaisesRegex(ValueError, "禁止安装"):
            load_config(self.config_path)

    def test_config_drift_is_invalid_outside_timer(self):
        makefile = self.source / "Makefile"
        makefile.write_text(makefile.read_text().replace("@true", "@echo changed >> $(O)/.config"))
        self.assertEqual(self.run_fake(), 1)
        row, = self.rows()
        self.assertEqual(row["wall_s"], "")
        self.assertIn("改变了固定", row["error"])

    def test_manifest_supports_internal_kernel_symlinks(self):
        (self.source / "internal.py").symlink_to("task.py")
        paths = manifest(self.source)
        first = preread(self.source, paths)
        self.script.write_text("changed\n")
        self.assertNotEqual(first, preread(self.source, paths))
        (self.source / "external").symlink_to("/etc/hosts")
        with self.assertRaisesRegex(ValueError, "树外"):
            manifest(self.source)

    def test_wall_excludes_preparation_and_cooldown(self):
        makefile = self.source / "Makefile"
        makefile.write_text(makefile.read_text().replace("@true", "@sleep 0.4"))
        cfg = load_config(self.config_path)
        cfg["cooldown_s"] = 0.4
        start = time.monotonic()
        with patch("compilebench.cli.Scheduler", FakeScheduler), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(experiment(cfg), 0)
        elapsed = time.monotonic() - start
        row, = self.rows()
        self.assertGreater(elapsed - float(row["wall_s"]), 0.75)
        self.assertLess(float(row["wall_s"]), 0.7)

    def test_memory_and_cpu_missing_are_explained(self):
        self.assertEqual(self.run_fake(), 0)
        row, = self.rows()
        details = json.loads((self.batch() / "run-001/default/attempt.json").read_text())
        for field in ("cpu_time_s", "peak_memory_bytes"):
            if row[field] == "":
                self.assertIn(field, details["missing_metrics"])

    def test_fallback_and_reload_invalidate_measurement(self):
        class ReloadScheduler(FakeScheduler):
            def verify(inner, expected, pid=None):
                if (self.base / "reload-marker").exists():
                    inner.state["enable_seq"] = "2"
                super(ReloadScheduler, inner).verify(expected, pid)

        marker = self.base / "reload-marker"
        scheduler = ReloadScheduler()
        outcome, notes, interrupted = measure(
            [sys.executable, "-c", f"import pathlib,time;pathlib.Path({str(marker)!r}).touch();time.sleep(0.2)"],
            self.base, dict(os.environ), self.base / "build.log", self.base / "samples.csv",
            scheduler, scheduler.initial(), None, power_state(), interval=0.05)
        self.assertFalse(outcome["valid"])
        self.assertEqual(outcome["exit_code"], 0)
        self.assertIn("strategy changed", outcome["error"])
        self.assertFalse(interrupted)

    def test_metrics_read_full_cgroup_totals(self):
        group = BuildCgroup.__new__(BuildCgroup)
        group.path = self.base
        (self.base / "cpu.stat").write_text("usage_usec 1750000\nuser_usec 1000000\n")
        (self.base / "memory.current").write_text("123\n")
        (self.base / "memory.peak").write_text("456\n")
        self.assertEqual(group.values(), {"cpu_time_s": 1.75, "memory_current_bytes": 123,
                                         "peak_memory_bytes": 456})

    def test_interrupt_during_persistence_does_not_lose_row(self):
        from compilebench.system import write_json
        control = Interrupts()
        fired = False

        def persist(path, data):
            nonlocal fired
            if Path(path).name == "attempt.json" and not fired:
                fired = True
                os.kill(os.getpid(), signal.SIGINT)
            return write_json(path, data)

        control.install()
        try:
            cfg = load_config(self.config_path)
            with patch("compilebench.cli.Scheduler", FakeScheduler), patch("compilebench.cli.write_json", persist), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(experiment(cfg, control), 130)
        finally:
            control.restore()
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(json.loads((self.batch() / "metadata.json").read_text())["status"], "interrupted")

    def test_cancellation_before_child_setsid_does_not_hang(self):
        control = Interrupts()
        original = os.fork

        def fork():
            pid = original()
            if pid == 0:
                time.sleep(0.2)
            else:
                control.handle(signal.SIGINT, None)
            return pid

        scheduler = FakeScheduler()
        with patch("compilebench.workload.os.fork", fork):
            outcome, _, interrupted = measure([sys.executable, "-c", "pass"], self.base,
                dict(os.environ), self.base / "build.log", self.base / "samples.csv", scheduler,
                scheduler.initial(), None, power_state(), interrupts=control)
        self.assertTrue(interrupted)
        self.assertFalse(outcome["valid"])
        self.assertIsNone(outcome["wall_s"])
        self.assertEqual(outcome["exit_code"], -signal.SIGKILL)

    def test_late_temperature_sample_is_outside_build_window(self):
        def sensor(_):
            if threading.current_thread() is not threading.main_thread():
                time.sleep(0.3)
                return 99.0
            return 40.0

        scheduler = FakeScheduler()
        with patch("compilebench.workload.temperature", sensor):
            outcome, notes, _ = measure([sys.executable, "-c", "import time;time.sleep(0.08)"], self.base,
                dict(os.environ), self.base / "build.log", self.base / "samples.csv", scheduler,
                scheduler.initial(), self.base / "sensor", power_state(), interval=0.05)
        self.assertTrue(outcome["valid"], outcome["error"])
        self.assertLess(outcome["wall_s"], 0.25)
        self.assertIsNone(outcome["temp_peak_sampled_c"])
        with (self.base / "samples.csv").open() as stream:
            self.assertEqual(list(csv.DictReader(stream)), [])
        self.assertIn("temp_peak_sampled_c", notes["missing_metrics"])

    def test_peripheral_batteries_do_not_change_power_conditions(self):
        supply_root = self.base / "supplies"
        supply_root.mkdir()
        for name, kind, scope, online in (("adapter", "Mains", "System", "0"),
                                          ("mouse", "Battery", "Device", "1"),
                                          ("usb-device", "USB", "Device", "1")):
            supply = supply_root / name
            supply.mkdir()
            for key, value in (("type", kind), ("scope", scope), ("online", online)):
                (supply / key).write_text(value)

        def path(value):
            return supply_root if str(value) == "/sys/class/power_supply" else Path(value)

        with patch("compilebench.system.Path", path):
            before = power_state()
            (supply_root / "mouse/online").write_text("0")
            self.assertEqual(before, power_state())
            (supply_root / "adapter/online").write_text("1")
            self.assertNotEqual(before, power_state())

    def test_dependency_probe_reports_failure_before_a_batch(self):
        cfg = load_config(self.config_path)
        cfg["compiler"] = ["false"]
        with self.assertRaisesRegex(ValueError, "C toolchain"):
            check_environment(cfg)
        self.assertFalse((self.base / "results/summary.csv").exists())

    def test_delegation_fallback_requires_exact_user_unit_controlgroup(self):
        path = Path('/sys/fs/cgroup/user.slice/example.service')
        result = subprocess.CompletedProcess([], 0, 'Delegate=yes\nControlGroup=/user.slice/example.service\n', '')
        with patch('compilebench.metrics.os.getxattr', side_effect=OSError(61, 'no data')):
            with patch('compilebench.metrics.subprocess.run', return_value=result):
                self.assertTrue(is_delegated(path))
            result.stdout = 'Delegate=yes\nControlGroup=/user.slice/some-other.service\n'
            with patch('compilebench.metrics.subprocess.run', return_value=result):
                self.assertFalse(is_delegated(path))

    def test_epp_change_is_logged_without_invalidating_balanced_mode(self):
        baseline = {"desktop_profile": "balanced", "/sys/class/power_supply/AC/online": "0",
                    "/sys/devices/system/cpu/cpufreq/policy0/energy_performance_preference": "balance_power"}
        current = {**baseline, "/sys/devices/system/cpu/cpufreq/policy0/energy_performance_preference": "balance_performance"}
        scheduler = FakeScheduler()
        with patch("compilebench.workload.power_state", return_value=current):
            outcome, notes, _ = measure([sys.executable, "-c", "import time;time.sleep(0.15)"], self.base,
                dict(os.environ), self.base / "build.log", self.base / "samples.csv", scheduler,
                scheduler.initial(), None, baseline, interval=0.05)
        self.assertTrue(outcome["valid"], outcome["error"])
        self.assertEqual(notes["power"]["end"]["desktop_profile"], "balanced")
        self.assertTrue(notes["power"]["events"])
        with (self.base / "samples.csv").open() as stream:
            samples = list(csv.DictReader(stream))
        change, = json.loads(samples[0]["power_changes_json"]).values()
        self.assertEqual(change, {"before": "balance_power", "after": "balance_performance", "kind": "diagnostic"})
        self.assertEqual(json.loads(samples[0]["power_state_json"])["desktop_profile"], "balanced")

    def test_real_power_condition_changes_remain_invalid(self):
        baseline = {"desktop_profile": "balanced", "/sys/class/power_supply/AC/online": "0",
                    "/sys/devices/system/cpu/cpufreq/policy0/scaling_governor": "powersave"}
        for key, changed in (("desktop_profile", "performance"),
                             ("/sys/class/power_supply/AC/online", "1"),
                             ("/sys/devices/system/cpu/cpufreq/policy0/scaling_governor", "performance")):
            with self.subTest(key=key):
                current = {**baseline, key: changed}
                self.assertIn(key, power_error(baseline, current, "测试"))
                self.assertIn(key, power_differences(baseline, current))
                scheduler = FakeScheduler()
                with patch("compilebench.workload.power_state", return_value=current):
                    outcome, _, _ = measure([sys.executable, "-c", "pass"], self.base,
                        dict(os.environ), self.base / "build.log", self.base / "samples.csv", scheduler,
                        scheduler.initial(), None, baseline, interval=0.05)
                self.assertFalse(outcome["valid"])
                self.assertIsNone(outcome["wall_s"])
                self.assertIn(key, outcome["error"])

    def test_mid_build_profile_change_has_field_level_evidence(self):
        marker = self.base / "changed-profile"
        baseline = {"desktop_profile": "balanced"}
        changed = {"desktop_profile": "power-saver"}
        scheduler = FakeScheduler()
        command = [sys.executable, "-c", f"import time,pathlib;time.sleep(0.06);pathlib.Path({str(marker)!r}).touch();time.sleep(0.12)"]
        with patch("compilebench.workload.power_state", side_effect=lambda: changed if marker.exists() else baseline):
            outcome, notes, _ = measure(command, self.base, dict(os.environ), self.base / "build.log",
                self.base / "samples.csv", scheduler, scheduler.initial(), None, baseline, interval=0.03)
        self.assertFalse(outcome["valid"])
        self.assertEqual(outcome["exit_code"], 0)
        self.assertIn("desktop_profile", outcome["error"])
        self.assertEqual(notes["power"]["end"], changed)
        with (self.base / "samples.csv").open() as stream:
            samples = list(csv.DictReader(stream))
        evidence = [json.loads(row["power_changes_json"]) for row in samples if row["error"]]
        self.assertTrue(evidence)
        self.assertEqual(evidence[0]["desktop_profile"]["kind"], "condition")

