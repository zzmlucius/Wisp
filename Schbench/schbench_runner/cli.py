"""One schbench execution path for current-policy and randomized block tests."""
import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
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
from compilebench_runner.system import command_output, digest, machine_info, read, sensor_path, write_json
from compilebench_runner.workload import SAMPLE_FIELDS, measure

from .results import FIELDS, assess, parse_results, summarize

PROJECT = Path(__file__).resolve().parents[1]


def cpuset(text):
    if isinstance(text, list):
        if not text or any(type(cpu) is not int or not 0 <= cpu < 1024 for cpu in text):
            raise ConfigError("CPU 数组须包含 0–1023 范围内的整数")
        return sorted(set(text))
    if not isinstance(text, str) or not re.fullmatch(r"\d+(?:-\d+)?(?:,\d+(?:-\d+)?)*", text):
        raise ConfigError("cpus 使用明确的 CPU 范围，例如 0-13 或 0-3,6；不使用 auto")
    cpus = set()
    for group in text.split(","):
        start, _, end = group.partition("-")
        start, end = int(start), int(end or start)
        if end < start or end >= 1024:
            raise ConfigError("CPU 范围倒置或超过 schbench CPU_SETSIZE=1024")
        cpus.update(range(start, end + 1))
    return sorted(cpus)


def finite(value, name, minimum=0, positive=False):
    if type(value) not in (int, float) or not math.isfinite(value) or value < minimum or (positive and value == 0):
        raise ConfigError(f"{name} 必须为有限{'正' if positive else '非负'}数")
    return value


def effective(cfg):
    return {key: value for key, value in cfg.items() if not key.startswith("_")}


def load_config(path, fixed=False, rps=None, output=None):
    path = Path(path).resolve()
    raw = path.read_bytes()
    cfg = json.loads(raw)
    if not isinstance(cfg, dict):
        raise ConfigError("schtest.json 顶层必须为对象")
    cfg["program"] = str((path.parent / Path(cfg["program"]).expanduser()).resolve())
    program = Path(cfg["program"])
    if not program.is_file() or not os.access(program, os.X_OK):
        raise ConfigError(f"schbench 程序不存在或不可执行: {program}；请先执行 ./build-schbench")
    cfg["cpus"] = cpuset(cfg["cpus"])
    available = set(os.sched_getaffinity(0))
    if not set(cfg["cpus"]) <= available:
        raise ConfigError(f"配置 CPU 不在当前可用 CPU 中: {sorted(set(cfg['cpus']) - available)}")
    cfg["mode"] = "fixed_rps" if fixed else "best_effort"
    workload = cfg["workload"]
    if not isinstance(workload, dict):
        raise ConfigError("workload 必须为对象")
    defaults = {"message_threads": 1, "worker_threads": len(cfg["cpus"]), "runtime_s": 60,
                "cache_footprint_kb": 256, "operations": 5, "sleep_us": 100,
                "locking": True, "warmup_s": 0, "zero_interval_s": 0}
    if set(workload) - set(defaults):
        raise ConfigError(f"不支持的 workload 参数: {sorted(set(workload) - set(defaults))}")
    workload = {**defaults, **workload}
    for key in ("message_threads", "worker_threads", "runtime_s", "cache_footprint_kb", "operations", "sleep_us"):
        value = workload[key]
        if type(value) is not int or value < (0 if key == "sleep_us" else 1) or value > 2**31 - 2:
            raise ConfigError(f"workload.{key} 必须为有效整数")
    if type(workload["locking"]) is not bool:
        raise ConfigError("workload.locking 必须为布尔值")
    if any(type(workload[key]) is not int or workload[key] != 0 for key in ("warmup_s", "zero_interval_s")):
        raise ConfigError("当前场景固定 warmup_s=0、zero_interval_s=0，保留整次累计统计")
    cfg["workload"] = workload
    if rps is not None:
        if not fixed:
            raise ConfigError("--rps 只用于 -R 固定请求率模式")
        cfg["rps"] = rps
    cfg.setdefault("rps", None)
    if cfg["rps"] is not None and (type(cfg["rps"]) is not int or not 1 <= cfg["rps"] <= 2**31 - 1):
        raise ConfigError("rps 必须为正整数或 null")
    if fixed and (cfg["rps"] is None or cfg["rps"] < workload["message_threads"]):
        raise ConfigError("固定请求率模式须先配置有效 rps 或传入 --rps；目标至少为 message_threads")
    if fixed and cfg["rps"] % workload["message_threads"]:
        raise ConfigError("原生 schbench 按消息线程整除 RPS；目标须能被 message_threads 整除")
    finite(cfg.setdefault("rps_tolerance", 0.05), "rps_tolerance")
    if cfg["rps_tolerance"] >= 1:
        raise ConfigError("rps_tolerance 须小于 1，例如 0.05 表示 ±5%")
    for key in ("request_p99_limit_us", "wakeup_p99_limit_us"):
        if cfg.setdefault(key, None) is not None:
            finite(cfg[key], key, positive=True)
    for key, default in (("cooldown_s", 30), ("sample_interval_s", 1)):
        finite(cfg.setdefault(key, default), key, positive=(key == "sample_interval_s"))
    cfg["results_dir"] = str((Path(output).expanduser().resolve() if output is not None
                               else (path.parent / Path(cfg.get("results_dir", "bench/results")).expanduser()).resolve()))
    result = Path(cfg["results_dir"])
    # Output roots must never contain code, the executable, or the live config.
    for protected in (PROJECT, PROJECT.parent / "Compile", program, path, PROJECT / "bench/src", PROJECT / "bench/build"):
        if protected.is_relative_to(result):
            raise ConfigError(f"results_dir 不得包含代码、源码、程序或配置: {result}")
    for protected in (PROJECT / "schbench_runner", PROJECT / "tests", PROJECT / "bench/src",
                      PROJECT / "bench/build", PROJECT.parent / "Compile"):
        if result.is_relative_to(protected):
            raise ConfigError(f"results_dir 不得写入代码、源码、构建或 Compile 目录: {result}")
    cfg.setdefault("temperature_sensor", None)
    cfg.setdefault("cgroup_parent", None)
    if cfg["temperature_sensor"]:
        cfg["temperature_sensor"] = str((path.parent / Path(cfg["temperature_sensor"]).expanduser()).resolve())
    if cfg["cgroup_parent"]:
        cfg["cgroup_parent"] = str(Path(cfg["cgroup_parent"]).expanduser().resolve())
    cfg["_config_path"] = str(path)
    cfg["_config_sha256"] = hashlib.sha256(raw).hexdigest()
    cfg["_program_sha256"] = digest(program)
    if cfg.get("provenance"):
        provenance = (path.parent / Path(cfg["provenance"]).expanduser()).resolve()
        cfg["provenance"] = str(provenance)
        data = json.loads(provenance.read_text())
        if data.get("binary_sha256") != cfg["_program_sha256"]:
            raise ConfigError("程序摘要与来源记录不一致；请重新 ./build-schbench 或更新程序来源")
        cfg["_provenance"] = data
        cfg["_provenance_sha256"] = digest(provenance)
    info = command_output([cfg["program"], "--help"])
    text = info.get("output", "")
    if "schbench" not in text.lower() or any(flag not in text for flag in ("-m", "-t", "-r", "-F", "-n", "-s", "-w", "-z", "-i")):
        raise ConfigError("程序 --help 未确认所需 schbench 参数（包括 -w 0、-z 0）；不猜测版本兼容性")
    if fixed and "-R" not in text:
        raise ConfigError("当前 schbench 未声明支持 -R")
    if not workload["locking"] and "-L" not in text:
        raise ConfigError("当前 schbench 未声明支持 -L")
    cfg["_native_help"] = info
    cfg["_native_json"] = "-j" in text and "--json" in text
    return cfg


def native_command(cfg, attempt):
    w = cfg["workload"]
    command = [cfg["program"], "-m", str(w["message_threads"]), "-t", str(w["worker_threads"]),
               "-r", str(w["runtime_s"]), "-F", str(w["cache_footprint_kb"]),
               "-n", str(w["operations"]), "-s", str(w["sleep_us"]), "-w", "0", "-z", "0",
               "-i", str(w["runtime_s"] + 1)]
    if not w["locking"]:
        command.append("-L")
    if cfg["mode"] == "fixed_rps":
        command.extend(["-R", str(cfg["rps"])])
    if cfg["_native_json"]:
        command.extend(["-j", str(attempt / "native.json")])
    return command


def save_inputs(cfg, directory, initial):
    write_json(directory / "schtest.json", effective(cfg))
    raw = Path(cfg["_config_path"]).read_bytes()
    if hashlib.sha256(raw).hexdigest() != cfg["_config_sha256"]:
        raise ConfigError("schtest.json 在读取后变化")
    (directory / "schtest.input.json").write_bytes(raw)
    machine = machine_info()
    sensor = sensor_path(cfg["temperature_sensor"])
    if sensor:
        label = read(sensor.with_name(sensor.name.replace("_input", "_label")))
        driver = read(sensor.parent / "name")
    else:
        label = driver = None
    snapshot = {"created_utc": datetime.now(timezone.utc).isoformat(), "parameters": effective(cfg),
                "settings_sha256": cfg["_config_sha256"],
                "effective_settings_sha256": digest(directory / "schtest.json"),
                "program_sha256": cfg["_program_sha256"],
                "program_help": cfg["_native_help"], "native_json_supported": cfg["_native_json"],
                "program_provenance": cfg.get("_provenance"),
                "version_note": "以来源提交和二进制 SHA256 标识版本；原生 JSON 的 version 是内核 release",
                "machine": machine, "online_cpus": read("/sys/devices/system/cpu/online"),
                "scheduler": initial, "environment": build_environment(cfg),
                "temperature_sensor": {"path": str(sensor) if sensor else None, "driver": driver,
                                       "label": label, "unit": "°C"}}
    write_json(directory / "snapshot.json", snapshot)
    return snapshot, sensor


def verify_inputs(cfg, directory, snapshot):
    paths = [(cfg["_config_path"], cfg["_config_sha256"]),
             (directory / "schtest.input.json", cfg["_config_sha256"]),
             (directory / "schtest.json", snapshot["effective_settings_sha256"]),
             (cfg["program"], snapshot["program_sha256"])]
    if cfg.get("_provenance_sha256"):
        paths.append((cfg["provenance"], cfg["_provenance_sha256"]))
    for path, expected in paths:
        if digest(path) != expected:
            raise ConfigError(f"输入与固定快照不匹配: {path}")
    if sorted(os.sched_getaffinity(0)) != snapshot["machine"]["available_cpus"]:
        raise ConfigError("可用 CPU affinity 发生变化")
    if read("/sys/devices/system/cpu/online") != snapshot["online_cpus"]:
        raise ConfigError("在线 CPU 发生变化")


def append_error(row, message):
    row["valid"] = False
    row["error"] = "; ".join(dict.fromkeys(filter(None, [row["error"], message])))


def run_attempt(cfg, directory, snapshot, sensor, scheduler, initial, policy, run, automatic, interrupts):
    folder = policy if policy in POLICIES else "current"
    attempt = directory / f"run-{run:03d}" / folder
    attempt.mkdir(parents=True)
    (attempt / "schbench.log").touch()
    with (attempt / "samples.csv").open("w", newline="") as stream:
        csv.writer(stream).writerow(SAMPLE_FIELDS)
    row = dict.fromkeys(FIELDS)
    row.update(policy=policy, run=run, valid=False, error="")
    details = {"command": native_command(cfg, attempt), "mode": cfg["mode"],
               "cpu_affinity": cfg["cpus"], "native_json_supported": cfg["_native_json"]}
    cancelled, fatal = False, None
    attempt_snapshot = {"parameters_snapshot": "../../snapshot.json", "scheduler_name": policy,
                        "scheduler": None, **details}
    write_json(attempt / "snapshot.json", attempt_snapshot)
    try:
        verify = lambda: verify_inputs(cfg, directory, snapshot)
        verify()
        expected = scheduler.select(policy, attempt / "scheduler.log") if automatic else initial
        details["expected_scheduler"] = expected
        # Strategy baseline is frozen after startup stabilization; no rebasing after cooldown.
        write_json(attempt / "snapshot.json", {**attempt_snapshot, "scheduler": expected})
        time.sleep(cfg["cooldown_s"])
        interrupts.check()
        verify()
        scheduler.verify(expected)
        outcome, notes, cancelled = measure(
            details["command"], attempt, snapshot["environment"], attempt / "schbench.log",
            attempt / "samples.csv", scheduler, expected, sensor, snapshot["machine"]["power"],
            cfg["sample_interval_s"], cfg["cgroup_parent"], interrupts,
            verify_parameters=verify, cpu_affinity=cfg["cpus"], workload_label="schbench")
        row.update(outcome)
        details.update(notes)
        try:
            values, evidence = parse_results(attempt / "schbench.log",
                attempt / "native.json" if cfg["_native_json"] else None,
                runtime=cfg["workload"]["runtime_s"])
            row.update(values)
            details["metric_sources"] = evidence
        except (OSError, ValueError, TypeError, KeyError) as exc:
            append_error(row, f"必需指标解析失败: {exc}")
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
            cancelled = cancelled or interrupts.requested
            if cancelled:
                append_error(row, "用户中断")
            details["constraints"] = assess(row, cfg)
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
    batch = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ") + f"-{os.getpid()}"
    directory = Path(cfg["results_dir"]) / batch
    directory.mkdir(parents=True)
    from .cleanup import register_root
    register_root(Path(cfg["results_dir"]))
    scheduler = (BatchScheduler(specs, directory, interrupts, timeout=cfg.get("scheduler_timeout_s", 30),
        settle=cfg.get("scheduler_settle_s", 5), workload_name="schtest") if rounds is not None else observer)
    metadata = {"schema_version": 1, "workload": "schbench", "mode": cfg["mode"],
                "scope": "randomized_blocks" if rounds is not None else "single_current_scheduler",
                "config": effective(cfg), "rounds": rounds or 1, "seed": seed if rounds is not None else None,
                "orders": orders, "initial_scheduler": initial, "attempts": [], "status": "running",
                "started_utc": datetime.now(timezone.utc).isoformat(),
                "metrics": {"wall_s": "门控 schbench exec 前 SIGCONT 到 wait4 返回的单调时钟秒；不含冷却、切换和收尾",
                            "cpu_time_s": "独立 cgroup 全进程树累积 CPU 秒；不可用留空",
                            "peak_memory_bytes": "独立 cgroup 峰值字节，包含文件缓存和内核内存，不含 swap",
                            "temperature": "固定 CPU 传感器，°C；峰值仅为正式运行窗口内采样最大值",
                            "avg_rps": "原生最终 average rps，请求/秒；不使用 RPS p50",
                            "request_p99_us": "原生整次累计请求延迟 p99，µs",
                            "wakeup_p99_us": "原生整次累计唤醒延迟 p99，µs"}}
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
                metadata["scheduler_commands"] = specs
                metadata["scheduler_controller"] = scheduler.info
            write_json(directory / "metadata.json", metadata)
            for run, order in enumerate(orders, 1):
                for policy in order:
                    row, stopped, issue, details, relative = run_attempt(
                        cfg, directory, snapshot, sensor, scheduler, initial,
                        policy, run, rounds is not None, interrupts)
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
                    cancelled = stopped or interrupts.requested
                    fatal = issue or fatal
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
                                fatal_error=fatal, final_scheduler=scheduler.snapshot(),
                                finished_utc=datetime.now(timezone.utc).isoformat(),
                                analysis=summarize(rows, assessments, cfg["mode"]))
                write_json(directory / "metadata.json", metadata)
    return 130 if cancelled else 1 if fatal or any(not row["valid"] for row in rows) else 0


def parser():
    p = argparse.ArgumentParser(description="当前调度器单测或随机四策略 schbench 测试", allow_abbrev=False)
    p.add_argument("-R", action="store_true", dest="fixed", help="固定请求率模式；目标来自配置或 --rps")
    p.add_argument("rounds", nargs="?", help="正整数轮数；省略时只测试当前调度器一次")
    p.add_argument("--rps", type=int, help="覆盖固定请求率目标（请求/秒）")
    p.add_argument("--output", type=Path, help="覆盖结果根目录，相对于当前工作目录")
    return p


def main(argv=None):
    lock = None
    interrupts = Interrupts()
    try:
        args = parser().parse_args(argv)
        if args.rounds is not None and (not re.fullmatch(r"[0-9]+", args.rounds) or int(args.rounds) < 1):
            raise ConfigError("轮数 n 必须为正整数")
        if os.getuid() == 0:
            raise ConfigError("请以普通用户运行 schtest；只有多轮监督进程提权")
        lock = acquire_lock()
        cfg = load_config(PROJECT / "schtest.json", args.fixed, args.rps, args.output)
        interrupts.install()
        return experiment(cfg, interrupts, rounds=int(args.rounds) if args.rounds is not None else None)
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"schtest 未能开始: {exc}", file=sys.stderr)
        return 2
    finally:
        interrupts.restore()
        if lock is not None:
            os.close(lock)
