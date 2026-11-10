"""One shared scheduler and sampler for foreground/background native workloads."""
import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import random
import re
import subprocess
import sys
import time

from compilebench_runner.cli import ConfigError, acquire_lock, build_environment, scheduler_specs
from compilebench_runner.control import BatchScheduler, ControlError, POLICIES
from compilebench_runner.interrupts import Interrupts
from compilebench_runner.policy import PolicyError, Scheduler, policy_name
from compilebench_runner.system import digest, machine_info, read, sensor_path, write_json
from compilebench_runner.workload import SAMPLE_FIELDS, measure
from schbench_runner.cli import append_error, effective

from .configuration import active_sides, commands, load_config
from .observation import apply_running_wakeup, metric_policy, summarize_trace, trace_environment
from .results import FIELDS, TIMER_SOURCES, TIMING_VALIDATION, assess, baseline_ratios, finalize_validity, parse_back, parse_front, summarize, synchronization, validity_rule

PROJECT = Path(__file__).resolve().parents[1]


def save_inputs(cfg, directory, initial):
    write_json(directory / "mixtest.json", effective(cfg))
    raw = Path(cfg["_config_path"]).read_bytes()
    if hashlib.sha256(raw).hexdigest() != cfg["_config_sha256"]:
        raise ConfigError("mixtest.json 在读取后变化")
    (directory / "mixtest.input.json").write_bytes(raw)
    sensor = sensor_path(cfg["temperature_sensor"])
    # Freeze all Python adapters as well as both executables for long batches.
    inputs = {str(path): digest(path) for package in (PROJECT / "mixbench_runner",
        PROJECT.parent / "Compile/compilebench_runner", PROJECT.parent / "Schbench/schbench_runner")
        for path in package.glob("*.py")}
    inputs.update({cfg[side]["program"]: cfg["_program_sha256"][side] for side in active_sides(cfg["case"])})
    inputs.update(cfg.get("_observation_files", {}))
    if cfg["case"] != "back":
        inputs[cfg["front"]["provenance"]] = cfg["_front"]["_provenance_sha256"]
    snapshot = {"created_utc": datetime.now(timezone.utc).isoformat(), "parameters": effective(cfg),
        "settings_sha256": cfg["_config_sha256"], "effective_settings_sha256": digest(directory / "mixtest.json"),
        "program_sha256": cfg["_program_sha256"], "versions": cfg["_versions"], "frozen_files": inputs,
        "timing_validation": dict(TIMING_VALIDATION),
        "machine": machine_info(), "online_cpus": read("/sys/devices/system/cpu/online"), "scheduler": initial,
        "environment": {**build_environment(cfg), "LC_ALL": "C", "LANG": "C"},
        "temperature_sensor": {"path": str(sensor) if sensor else None,
            "driver": read(sensor.parent / "name") if sensor else None,
            "label": read(sensor.with_name(sensor.name.replace("_input", "_label"))) if sensor else None, "unit": "°C"}}
    write_json(directory / "snapshot.json", snapshot)
    return snapshot, sensor


def verify_inputs(cfg, directory, snapshot):
    inputs = {**snapshot["frozen_files"], cfg["_config_path"]: cfg["_config_sha256"],
        str(directory / "mixtest.input.json"): cfg["_config_sha256"],
        str(directory / "mixtest.json"): snapshot["effective_settings_sha256"]}
    for path, expected in inputs.items():
        if digest(path) != expected:
            raise ConfigError(f"固定输入发生变化: {path}")
    if sorted(os.sched_getaffinity(0)) != snapshot["machine"]["available_cpus"]:
        raise ConfigError("可用 CPU affinity 变化")
    if read("/sys/devices/system/cpu/online") != snapshot["online_cpus"]:
        raise ConfigError("在线 CPU 变化")


def restrict_samples(attempt, notes, evidence, sensor, row):
    """Remove coordinator preparation/cleanup from the reported workload window."""
    children = evidence.get("children", {}).values()
    starts = [c["exec_monotonic_s"] for c in children if c.get("exec_monotonic_s") is not None]
    ends = [c["exit_monotonic_s"] for c in children if c.get("exit_monotonic_s") is not None]
    if not starts or not ends:
        row["wall_s"] = None
        return
    begin, end = min(starts), max(ends)
    row["temp_start_c"] = evidence.get("temp_start_c")
    row["wall_s"] = end - begin
    notes["workload_window"] = {"start_monotonic_s": begin, "end_monotonic_s": end,
        "timing": "首个原生 exec 紧前至最后一个 waitpid 退出观测；共同门控/协调进程启动与收尾不计入"}
    origin = notes.get("started_monotonic_s")
    if origin is None:
        raise ValueError("共享采样器缺少单调时钟起点；无法核对正式采样窗口")
    sample = attempt / "samples.csv"
    with sample.open() as stream:
        values = [s for s in csv.DictReader(stream) if begin <= origin + float(s["relative_s"]) <= end]
    for s in values:
        s["relative_s"] = f"{origin + float(s['relative_s']) - begin:.6f}"
    with sample.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=SAMPLE_FIELDS)
        writer.writeheader();writer.writerows(values)
    temps = [float(s["temp_c"]) for s in values if s["temp_c"]]
    row["temp_peak_sampled_c"] = max(temps) if temps else None
    if not temps:
        notes.setdefault("missing_metrics", {})["temp_peak_sampled_c"] = "正式混合负载窗口内没有有效温度样本"


def run_attempt(cfg, directory, snapshot, sensor, scheduler, initial, policy, run, automatic, interrupts):
    attempt = directory / f"run-{run:03d}" / (policy if policy in POLICIES else "current")
    attempt.mkdir(parents=True)
    cmd = commands(cfg, attempt)
    for side in cmd:
        (attempt / f"{side}.log").touch()
    with (attempt / "samples.csv").open("w", newline="") as stream:
        csv.writer(stream).writerow(SAMPLE_FIELDS)
    row = dict.fromkeys(FIELDS)
    row.update(policy=policy, run=run, valid=False, error="")
    details = {"commands": cmd, "case": cfg["case"], "parameters": effective(cfg),
        "cpu_affinity": cfg["cpus"], "timeout_s": cfg["timeout_s"]}
    cancelled, fatal = False, None
    frozen = {"parameters_snapshot": "../../snapshot.json", "scheduler_name": policy, "scheduler": None, **details}
    write_json(attempt / "snapshot.json", frozen)
    try:
        verify = lambda: verify_inputs(cfg, directory, snapshot)
        verify()
        expected = scheduler.select(policy, attempt / "scheduler.log") if automatic else initial
        details["expected_scheduler"] = expected
        write_json(attempt / "snapshot.json", {**frozen, "scheduler": expected})
        time.sleep(cfg["cooldown_s"])
        interrupts.check()
        verify()
        scheduler.verify(expected)
        spec = {"commands": cmd, "cpus": cfg["cpus"], "sample_interval_s": cfg["sample_interval_s"],
                "sensor": str(sensor) if sensor else None, "expected_scheduler": expected}
        if cfg.get("wakeup_observation", {}).get("enabled"):
            spec["front_environment"] = trace_environment(attempt, cfg)
        write_json(attempt / "launch.json", spec)
        launch_sha = digest(attempt / "launch.json")
        def verify_launch():
            verify()
            if digest(attempt / "launch.json") != launch_sha:
                raise ConfigError("本次原生命令配置在门控后变化")
        evidence_path = attempt / "execution.json"
        worker = [sys.executable, str(PROJECT / "mixbench_runner/worker.py"), str(attempt / "launch.json"), str(evidence_path)]
        outcome, notes, cancelled = measure(worker, attempt, snapshot["environment"],
            attempt / "prepare.log", attempt / "samples.csv", scheduler, expected, sensor,
            snapshot["machine"]["power"], cfg["sample_interval_s"], cfg["cgroup_parent"], interrupts,
            verify_parameters=verify_launch, cpu_affinity=cfg["cpus"], workload_label="mixed workload", timeout_s=cfg["timeout_s"])
        row.update(outcome)
        details.update(notes)
        evidence = json.loads(evidence_path.read_text()) if evidence_path.exists() else {}
        details["execution"] = evidence
        details["exit_codes"] = {side: child.get("exit_code") for side, child in evidence.get("children", {}).items()}
        if evidence.get("error"):
            append_error(row, evidence["error"])
        restrict_samples(attempt, details, evidence, sensor, row)
        back = None
        for side in cmd:
            try:
                if side == "front":
                    metrics, sources = parse_front(attempt / "front.log",
                        attempt / "front-native.json" if cfg["_front"]["_native_json"] else None, cfg["runtime_s"])
                    row.update(metrics)
                    details["front_metric_sources"] = sources
                else:
                    back = parse_back(attempt / "back.log")
                    row["back_events_s"] = back["events_s"]
                    details["back_native"] = back
            except (OSError, ValueError) as exc:
                append_error(row, f"{side} 原生结果核验失败: {exc}")
        sync, issues = synchronization(evidence, cfg, back)
        details["synchronization"] = sync
        for issue in issues:
            append_error(row, issue)
        # Zero only means ALL required native workloads exited normally.
        if any(details["exit_codes"].get(side) != 0 for side in cmd) and row["exit_code"] == 0:
            row["exit_code"] = 1
        details["resource_scope"] = "共享父 cgroup 的完整双方进程树及协调器少量开销；内存包含缓存/内核内存，不含 swap；不相加子组峰值"
        verify()
        scheduler.verify(expected)
    except KeyboardInterrupt:
        cancelled = True
        append_error(row, "用户中断")
    except Exception as exc:
        append_error(row, str(exc))
        if isinstance(exc, ControlError):
            fatal = str(exc)
    finally:
        with interrupts.defer():
            if automatic:
                try:
                    issue = scheduler.stop()
                    if issue:
                        append_error(row, issue)
                    scheduler.verify(scheduler.initial())
                except Exception as exc:
                    fatal = str(exc)
                    append_error(row, fatal)
            if cfg.get("wakeup_observation", {}).get("enabled"):
                try:
                    details["wakeup_observation"] = summarize_trace(attempt)
                except Exception as exc:
                    details["wakeup_observation"] = {"status": "unavailable", "capture_complete": False, "error": str(exc)}
                issue = apply_running_wakeup(row, details, details["wakeup_observation"])
                if issue:
                    append_error(row, issue)
            cancelled = cancelled or interrupts.requested
            if cancelled:
                append_error(row, "用户中断")
            finalize_validity(row, details, cfg)
            details["constraints"] = assess(row, cfg)
            details["baselines"] = baseline_ratios(cfg, snapshot, row)
            details["result"] = row
            write_json(attempt / "attempt.json", details)
    return row, cancelled, fatal, details, str(attempt.relative_to(directory))


def experiment(cfg, interrupts=None, rounds=None, seed=None):
    interrupts = interrupts or Interrupts()
    observer = Scheduler()
    initial = observer.initial()
    observer.verify(initial, os.getpid())
    if rounds is not None and initial["state"] != "disabled":
        raise PolicyError("发现已有外部 scx；多轮模式拒绝接管，请先自行停止")
    specs = scheduler_specs(cfg) if rounds is not None else None
    seed = random.SystemRandom().getrandbits(64) if seed is None else seed
    rng = random.Random(seed)
    orders = []
    for _ in range(rounds or 1):
        order = list(POLICIES) if rounds is not None else [policy_name(initial)]
        if rounds is not None:
            rng.shuffle(order)
        orders.append(order)
    directory = Path(cfg["results_dir"]) / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ") + f"-{os.getpid()}")
    directory.mkdir(parents=True)
    from .cleanup import register_root
    register_root(Path(cfg["results_dir"]))
    scheduler = BatchScheduler(specs, directory, interrupts, timeout=cfg.get("scheduler_timeout_s", 30),
        settle=cfg.get("scheduler_settle_s", 5), workload_name="mixtest") if rounds is not None else observer
    metadata = {"schema_version": 2, "workload": "mixbench", "config": effective(cfg),
                "validity_policy": validity_rule(cfg),
                "scope": "randomized_blocks" if rounds is not None else "single_current_scheduler",
                "rounds": rounds or 1, "seed": seed if rounds is not None else None,
                "orders": orders, "initial_scheduler": initial, "attempts": [], "status": "running",
                "started_utc": datetime.now(timezone.utc).isoformat(),
                "metrics": {"avg_rps": "前台整次平均请求/秒", "request_p99_us": "前台请求 p99，µs",
                    "wakeup_p99_us": "前台运行阶段唤醒 p99（stopping=0），µs" if cfg.get("wakeup_observation", {}).get("enabled") else "前台原生全量唤醒 p99，µs",
                    "back_events_s": "后台原生 CPU speed events/s，不用配置时长重算",
                    "wall_s": "首个原生 exec 紧前至最后一个退出观测，单调时钟秒",
                    "cpu_time_s": "双方完整进程树和协调器的父 cgroup CPU 秒",
                    "peak_memory_bytes": "父 cgroup 内存峰值字节，不相加子组峰值；含缓存及内核内存，不含 swap",
                    "temperature": "同一传感器，°C；峰值仅为正式负载期间采样最高值"},
                "native_timing": TIMER_SOURCES, "synchronization": cfg["synchronization"],
                "timing_validation": dict(TIMING_VALIDATION),
                "wakeup_metric": metric_policy(bool(cfg.get("wakeup_observation", {}).get("enabled"))),
                "commands": commands(cfg, directory / "run-NNN/POLICY")}

    rows, assessments, cancelled, fatal = [], [], False, None
    write_json(directory / "metadata.json", metadata)
    with (directory / "summary.csv").open("w", newline="") as summary:
        writer = csv.DictWriter(summary, fieldnames=FIELDS)
        writer.writeheader()
        summary.flush()
        try:
            snapshot, sensor = save_inputs(cfg, directory, initial)
            metadata["snapshot"] = "snapshot.json"
            if rounds is not None:
                scheduler.open()
                metadata.update(scheduler_commands=specs, scheduler_controller=scheduler.info)
            write_json(directory / "metadata.json", metadata)
            for run, order in enumerate(orders, 1):
                for policy in order:
                    row, stopped, issue, details, relative = run_attempt(cfg, directory, snapshot, sensor,
                        scheduler, initial, policy, run, rounds is not None, interrupts)
                    with interrupts.defer():
                        writer.writerow(row)
                        summary.flush()
                        os.fsync(summary.fileno())
                        rows.append(row)
                        assessments.append(details["constraints"])
                        metadata["attempts"].append(relative)
                        if rounds is None:
                            metadata.update(result=row, attempt=relative)
                        write_json(directory / "metadata.json", metadata)
                    cancelled, fatal = stopped or interrupts.requested, issue or fatal
                    if cancelled or fatal:
                        break
                if cancelled or fatal:
                    break
        except KeyboardInterrupt:
            cancelled = True
        except Exception as exc:
            fatal = str(exc)
        finally:
            with interrupts.defer():
                if rounds is not None:
                    try:
                        scheduler.close()
                    except Exception as exc:
                        fatal = "; ".join(filter(None, [fatal, str(exc)]))
                    metadata["scheduler_controller"] = scheduler.info
                cancelled = cancelled or interrupts.requested
                metadata.update(status="interrupted" if cancelled else "failed" if fatal or any(not r["valid"] for r in rows) else "completed",
                    fatal_error=fatal, final_scheduler=scheduler.snapshot(), analysis=summarize(rows, assessments, cfg["case"]),
                    finished_utc=datetime.now(timezone.utc).isoformat())
                write_json(directory / "metadata.json", metadata)
    return 130 if cancelled else 1 if fatal or any(not row["valid"] for row in rows) else 0


def parser():
    p = argparse.ArgumentParser(description="schbench 前台 + sysbench CPU 后台混合测试", allow_abbrev=False,
        epilog="示例：./mixtest 1 -1000 或 ./mixtest 1 -n 1000，四种调度器各测一次，目标 RPS=1000。")
    p.add_argument("rounds", nargs="?", help="正整数轮数；省略时只测当前调度器一次")
    p.add_argument("--case", choices=("mixed", "front", "back"), default="mixed")
    p.add_argument("-n", "--rps", type=int, help="覆盖独立预实验确定的统一前台 RPS；也可简写为 -1000")
    p.add_argument("--bg-threads", type=int, help="覆盖后台线程数")
    p.add_argument("--output", type=Path, help="结果根目录；相对于调用时工作目录")
    p.add_argument("--observe-wakeup", action="store_true", help="逐次唤醒观测；汇总 p99 仅统计 stopping=0，原始记录保留")
    return p


def parse_arguments(argv=None):
    tokens = iter(sys.argv[1:] if argv is None else argv)
    normalized = []
    for token in tokens:
        if token == "--":
            normalized.extend([token, *tokens])
            break
        if token in {"-n", "--rps", "--case", "--bg-threads", "--output"}:
            # An option value must retain its sign, even when it looks like shorthand.
            normalized.append(token)
            value = next(tokens, None)
            if value is not None:
                normalized.append(value)
        elif re.fullmatch(r"-[0-9]+", token):
            normalized.extend(["--rps", token[1:]])
        else:
            normalized.append(token)
    return parser().parse_args(normalized)


def main(argv=None):
    lock = None
    interrupts = Interrupts()
    try:
        args = parse_arguments(argv)
        if args.rounds is not None and (not re.fullmatch(r"[0-9]+", args.rounds) or int(args.rounds) < 1):
            raise ConfigError("轮数必须为正整数")
        if os.getuid() == 0:
            raise ConfigError("请以普通用户运行 mixtest；只有多轮监督进程提权")
        lock = acquire_lock()
        cfg = load_config(PROJECT / "mixtest.json", args.case, args.rps, args.bg_threads, args.output,
                          observe_wakeup=args.observe_wakeup)
        interrupts.install()
        return experiment(cfg, interrupts, rounds=int(args.rounds) if args.rounds is not None else None)
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"mixtest 未能开始: {exc}", file=sys.stderr)
        return 2
    finally:
        interrupts.restore()
        if lock is not None:
            os.close(lock)

