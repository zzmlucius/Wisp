"""Measure exactly one foreground workload; setup and cleanup stay outside wall_s."""
import csv
import json
import os
from pathlib import Path
import signal
import select
import threading
import time

from .metrics import BuildCgroup
from .interrupts import Interrupts
from .policy import PolicyError
from .system import power_state, power_differences, power_error, read, temperature


SAMPLE_FIELDS = ["relative_s", "cpu_time_s", "memory_current_bytes", "peak_memory_bytes",
                 "temp_c", "sched_state", "sched_ops", "enable_seq", "task_ext_enabled",
                 "host_load1", "host_mem_available_bytes", "power_state_json", "power_changes_json", "error"]


def terminate_group(pid):
    for sig, delay in ((signal.SIGTERM, 1), (signal.SIGKILL, 0)):
        try:
            os.killpg(pid, sig)
        except ProcessLookupError:
            return
        if delay:
            time.sleep(delay)


def abort_and_wait(pid, started):
    """Wait for the leader immediately; escalation and descendant cleanup are separate."""
    def send(sig):
        try:
            os.killpg(pid, sig)
        except ProcessLookupError:
            pass

    if not started:
        # The child may not have completed setsid() when cancellation arrives.
        os.kill(pid, signal.SIGKILL)
        return os.wait4(pid, 0)
    send(signal.SIGTERM)
    timer = threading.Timer(1, send, args=(signal.SIGKILL,))
    timer.daemon = True
    timer.start()
    try:
        return os.wait4(pid, 0)
    finally:
        timer.cancel()
        timer.join()


def measure(command, directory, environment, log_path, sample_path, scheduler,
            expected, sensor, expected_power, interval=1.0, cgroup_parent=None, interrupts=None,
            verify_parameters=None, cpu_affinity=None, workload_label="编译"):
    """Gated fork: attach and verify the task before starting its make exec.

    No background threads exist at fork. wall_s starts immediately before SIGCONT
    and ends immediately after wait4(make); only exec/wakeup overhead is included.
    """
    interrupts = interrupts or Interrupts()
    cgroup = BuildCgroup(cgroup_parent)
    result = {"exit_code": None, "wall_s": None, "cpu_time_s": None,
              "peak_memory_bytes": None, "temp_start_c": None,
              "temp_peak_sampled_c": None, "error": "", "valid": False}
    notes = {"cgroup": str(cgroup.path) if cgroup.path else None,
             "missing_metrics": dict(cgroup.notes), "timing": "monotonic SIGCONT-before-exec to wait4 return"}
    notes["power"] = {"baseline": expected_power, "events": [],
                      "rule": "主机供电、桌面 ActiveProfile、platform profile、driver/governor 为条件；EPP 仅诊断"}
    if cgroup.reason:
        notes["missing_metrics"].update(cpu_time_s=cgroup.reason, peak_memory_bytes=cgroup.reason)
    if sensor is None:
        notes["missing_metrics"]["temperature"] = "未发现 CPU 温度传感器；可配置 temperature_sensor"
    errors, observations = [], []
    stop = threading.Event()
    ready = threading.Event()
    thread = None
    pid = None
    reaped = False
    start = end = None
    pidfd = None
    interrupted = False
    with open(log_path, "ab", buffering=0) as log, open(sample_path, "w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=SAMPLE_FIELDS)
        writer.writeheader()

        def observe():
            sample_errors = []
            try:
                if verify_parameters:
                    verify_parameters()
                scheduler.verify(expected)
                if expected["state"] == "enabled" and scheduler.snapshot()["switch_all"] != "1":
                    raise PolicyError("switch_all 不为 1；无法保证全部负载任务受目标 scx 管理")
                # The leader may have exited between observation and wait4.
                stat = read(f"/proc/{pid}/stat") or ""
                alive = stat and stat.rsplit(")", 1)[-1].strip().split()[0] not in {"Z", "X"}
                if alive:
                    scheduler.verify(expected, pid)
                    if cpu_affinity is not None and os.sched_getaffinity(pid) != set(cpu_affinity):
                        raise PolicyError("负载 CPU affinity 在运行期间变化")
            except (PolicyError, OSError) as exc:
                sample_errors.append(str(exc))
            observed_power = power_state()
            changes = power_differences(expected_power, observed_power, include_diagnostics=True)
            issue = power_error(expected_power, observed_power, "采样")
            if issue:
                sample_errors.append(issue)
            if changes and (not notes["power"]["events"] or notes["power"]["events"][-1]["changes"] != changes):
                notes["power"]["events"].append({"relative_s": time.monotonic() - start, "changes": changes})
            current = scheduler.snapshot()
            temp = temperature(sensor)
            observed_at = time.monotonic()
            if temp is None and sensor:
                notes["missing_metrics"]["temperature"] = "选定传感器采样期间读取失败"
            memory = dict(line.split(":", 1) for line in (read("/proc/meminfo") or "").splitlines())
            available = memory.get("MemAvailable", "").split()
            task_ext = ""
            for line in (read(f"/proc/{pid}/sched") or "").splitlines():
                if line.startswith("ext.enabled"):
                    task_ext = line.split(":", 1)[1].strip()
            observations.append({"observed_at": observed_at,
                             "relative_s": f"{observed_at - start:.6f}",
                             **cgroup.values(), "temp_c": temp,
                             "sched_state": current["state"], "sched_ops": current["ops"],
                             "enable_seq": current["enable_seq"], "task_ext_enabled": task_ext,
                             "host_load1": os.getloadavg()[0],
                             "host_mem_available_bytes": int(available[0]) * 1024 if available else None,
                             "power_state_json": json.dumps(observed_power, ensure_ascii=False, sort_keys=True),
                             "power_changes_json": json.dumps(changes, ensure_ascii=False, sort_keys=True),
                             "error": "; ".join(sample_errors)})
            writer.writerow({key: value for key, value in observations[-1].items() if key != "observed_at"})
            stream.flush()
            errors.extend(sample_errors)

        def monitor():
            ready.wait()
            next_sample = start
            while not stop.is_set():
                try:
                    # Faster status checks catch fallback/reload between resource samples.
                    scheduler.verify(expected)
                    if time.monotonic() >= next_sample:
                        observe()
                        next_sample += interval
                except Exception as exc:
                    errors.append(f"采样失败: {exc}")
                stop.wait(min(0.1, interval))

        try:
            with interrupts.defer():
                pid = os.fork()
            if pid == 0:
                try:
                    os.setsid()
                    if cpu_affinity is not None:
                        os.sched_setaffinity(0, cpu_affinity)
                    signal.signal(signal.SIGINT, signal.SIG_DFL)
                    signal.signal(signal.SIGTERM, signal.SIG_DFL)
                    os.dup2(log.fileno(), 1)
                    os.dup2(log.fileno(), 2)
                    with open(os.devnull, "rb") as null:
                        os.dup2(null.fileno(), 0)
                    os.chdir(directory)
                    os.kill(os.getpid(), signal.SIGSTOP)
                    os.execvpe(command[0], command, environment)
                except BaseException as exc:
                    os.write(2, f"exec failed: {exc}\n".encode())
                    os._exit(127)
            interrupts.check()
            _, status = os.waitpid(pid, os.WUNTRACED)
            if not os.WIFSTOPPED(status):
                reaped = True
                raise RuntimeError(f"{workload_label}进程未能进入计时门控")
            cgroup.attach(pid)
            if verify_parameters:
                verify_parameters()
            scheduler.verify(expected, pid)
            if expected["state"] == "enabled" and scheduler.snapshot()["switch_all"] != "1":
                raise PolicyError("仅支持 switch_all=1 的 scx，拒绝 partial 模式")
            observed_power = power_state()
            notes["power"]["start"] = observed_power
            issue = power_error(expected_power, observed_power, "计时前")
            if issue:
                raise RuntimeError(issue)
            result["temp_start_c"] = temperature(sensor)
            pidfd = os.pidfd_open(pid)
            thread = threading.Thread(target=monitor, daemon=True)
            thread.start()
            with interrupts.defer():
                start = time.monotonic()
                os.kill(pid, signal.SIGCONT)
                ready.set()
            interrupts.check()
            select.select([pidfd], [], [])
            # pidfd readiness means wait4 won't block. Defer cancellation only
            # through reaping and assigning the status, never while make runs.
            with interrupts.defer():
                _, status, _ = os.wait4(pid, 0)
                end = time.monotonic()
                reaped = True
                result["exit_code"] = os.waitstatus_to_exitcode(status)
            stop.set()
            thread.join()
            if verify_parameters:
                verify_parameters()
            scheduler.verify(expected)
            observed_power = power_state()
            notes["power"]["end"] = observed_power
            issue = power_error(expected_power, observed_power, f"{workload_label}结束")
            if issue:
                raise RuntimeError(issue)
            interrupts.check()
            if result["exit_code"] != 0:
                errors.append(f"{workload_label}失败，退出码 {result['exit_code']}")
        except KeyboardInterrupt:
            interrupted = True
            errors.append("用户中断")
        except Exception as exc:
            errors.append(str(exc))
        finally:
            with interrupts.defer():
                stop.set()
                ready.set()
                if thread and thread.ident is not None:
                    thread.join()
                if pid and not reaped:
                    try:
                        _, status, _ = abort_and_wait(pid, start is not None)
                        result["exit_code"] = os.waitstatus_to_exitcode(status)
                        if start is not None:
                            end = time.monotonic()
                    except ChildProcessError:
                        errors.append(f"{workload_label}进程退出状态丢失")
                    terminate_group(pid)
                elif pid:
                    try:
                        os.killpg(pid, 0)
                    except ProcessLookupError:
                        pass
                    else:
                        errors.append(f"{workload_label}退出后仍有子进程；已终止本次进程组")
                        terminate_group(pid)
                if pidfd is not None:
                    os.close(pidfd)
                if start is not None and end is not None:
                    result["wall_s"] = end - start
                # An observation can overlap exit; only persist readings whose
                # temperature timestamp is inside the measured make window.
                kept = [sample for sample in observations
                        if start is not None and end is not None
                        and start <= sample["observed_at"] <= end]
                stream.seek(0)
                stream.truncate()
                writer.writeheader()
                for sample in kept:
                    sample.pop("observed_at")
                    writer.writerow(sample)
                stream.flush()
                temps = [sample["temp_c"] for sample in kept if sample["temp_c"] is not None]
                result["temp_peak_sampled_c"] = max(temps) if temps else None
                if sensor and not temps:
                    notes["missing_metrics"]["temp_peak_sampled_c"] = f"正式{workload_label}窗口内没有有效温度样本"
                if result["temp_start_c"] is None:
                    notes["missing_metrics"]["temp_start_c"] = "起始温度不可用"
                try:
                    values = cgroup.values()
                    for field in ("cpu_time_s", "peak_memory_bytes"):
                        result[field] = values[field]
                        if values[field] is None:
                            notes["missing_metrics"].setdefault(field, "对应 cgroup 统计不可读取")
                    if cgroup.pids():
                        errors.append("编译 cgroup 尚有存活子进程；已终止")
                        cgroup.kill_remaining()
                        deadline = time.monotonic() + 2
                        while cgroup.pids() and time.monotonic() < deadline:
                            time.sleep(0.02)
                    cgroup.close()
                except (OSError, ValueError) as exc:
                    errors.append(f"cgroup 收尾失败: {exc}")
                interrupted = interrupted or interrupts.requested
                if interrupted and "用户中断" not in errors:
                    errors.append("用户中断（收尾阶段）")
    result["error"] = "; ".join(dict.fromkeys(errors))
    result["valid"] = not errors and result["exit_code"] == 0
    return result, notes, interrupted
