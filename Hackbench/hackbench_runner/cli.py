"""Fixed-work hackbench tests with the existing sched_ext supervisor and sampler."""
import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import random
import re
import shutil
import subprocess
import sys
import time

from compilebench.cli import ConfigError, acquire_lock, build_environment, scheduler_specs
from compilebench.control import BatchScheduler, ControlError, POLICIES
from compilebench.interrupts import Interrupts
from compilebench.policy import PolicyError, Scheduler, policy_name
from compilebench.system import command_output, digest, machine_info, read, sensor_path, write_json
from compilebench.workload import SAMPLE_FIELDS, measure
from schbench_runner.cli import cpuset, finite, effective, append_error

from .preflight import check_limits
from .results import FIELDS, parse_results, summarize

PROJECT = Path(__file__).resolve().parents[1]


def positive(value, name):
    if type(value) is not int or not 1 <= value <= 2**31 - 1:
        raise ConfigError(f"{name} 必须为 1–2147483647 的整数")
    return value


def load_config(path, groups=None, loops=None, output=None):
    path = Path(path).resolve()
    raw = path.read_bytes()
    cfg = json.loads(raw)
    if not isinstance(cfg, dict):
        raise ConfigError("hacktest.json 顶层必须为对象")
    program = cfg["program"]
    if not isinstance(program, str) or not program:
        raise ConfigError("program 必须为 hackbench 路径，不接受额外参数或 shell 命令")
    executable = shutil.which(program) if "/" not in program else path.parent / Path(program).expanduser()
    if not executable or not Path(executable).is_file() or not os.access(executable, os.X_OK):
        raise ConfigError(f"hackbench 不存在或不可执行: {program}；Fedora 软件包为 realtime-tests")
    cfg["program"] = str(Path(executable).resolve())
    cfg["cpus"] = cpuset(cfg["cpus"])
    if not set(cfg["cpus"]) <= os.sched_getaffinity(0):
        raise ConfigError("配置 CPU 超出当前可用 CPU 范围")
    w = cfg.get("workload", {})
    defaults = {"mode": "process", "communication": "socket", "groups": 4,
                "fds": 20, "loops": 10000, "datasize": 100}
    if not isinstance(w, dict) or set(w) - set(defaults):
        raise ConfigError("workload 仅支持 mode/communication/groups/fds/loops/datasize；不接受 FIFO 或其他原生参数")
    w = {**defaults, **w}
    if w["mode"] not in {"process", "thread"} or w["communication"] not in {"socket", "pipe"}:
        raise ConfigError("mode 只能为 process/thread；communication 只能为 socket/pipe")
    if groups is not None:
        w["groups"] = groups
    if loops is not None:
        w["loops"] = loops
    for key in ("groups", "fds", "loops", "datasize"):
        positive(w[key], key)
    if 2 * w["groups"] * w["fds"] > 2**31 - 1 or w["fds"] * w["loops"] > 2**32 - 1:
        raise ConfigError("worker 数或每接收者消息数超过原生整数范围")
    cfg["workload"] = w
    for key, value in (("cooldown_s", 30), ("sample_interval_s", 1), ("timeout_s", 300)):
        finite(cfg.setdefault(key, value), key, positive=(key != "cooldown_s"))
    cfg["results_dir"] = str(Path(output).expanduser().resolve() if output is not None else
                             (path.parent / Path(cfg.get("results_dir", "bench/results")).expanduser()).resolve())
    result = Path(cfg["results_dir"])
    protected = [PROJECT / "hackbench_runner", PROJECT / "tests", PROJECT / "bench/src",
                 PROJECT / "bench/build", PROJECT.parent / "Compile", PROJECT.parent / "Schbench"]
    if any(p.resolve().is_relative_to(result) or result.is_relative_to(p.resolve()) for p in protected):
        raise ConfigError("结果目录不得与代码、程序或其他测试目录重叠")
    if any(p.is_relative_to(result) for p in (PROJECT, path, Path(cfg["program"]))):
        raise ConfigError("结果目录不得包含项目、配置或程序")
    for key in ("temperature_sensor", "cgroup_parent"):
        cfg.setdefault(key, None)
        if cfg[key]:
            cfg[key] = str((path.parent / Path(cfg[key]).expanduser()).resolve())
    info = command_output([cfg["program"], "--help"])
    help_text = info.get("output", "")
    version = re.search(r"hackbench\s+V\s+([\d.]+)", help_text)
    if not version or any(flag not in help_text for flag in ("--process", "--threads", "--pipe", "--groups", "--fds", "--loops", "--datasize")):
        raise ConfigError("--help 未确认所需 hackbench 参数和版本；不猜测兼容性")
    cfg.update(_config_path=str(path), _config_sha256=hashlib.sha256(raw).hexdigest(),
               _program_sha256=digest(cfg["program"]), _native_help=info, _native_version=version[1])
    rpm = shutil.which("rpm")
    cfg["_package"] = command_output([rpm, "-qf", "--qf", "%{NAME} %{VERSION}-%{RELEASE} %{ARCH}\n", cfg["program"]]) if rpm else {"reason": "rpm 不可用；以二进制摘要和 --help 版本标识"}
    cfg["_preflight"] = check_limits(cfg)
    return cfg


def native_command(cfg):
    w = cfg["workload"]
    cmd = [cfg["program"], "-P" if w["mode"] == "process" else "-T", "-g", str(w["groups"]),
           "-f", str(w["fds"]), "-l", str(w["loops"]), "-s", str(w["datasize"])]
    if w["communication"] == "pipe":
        cmd.append("-p")
    return cmd


def save_inputs(cfg, directory, initial):
    write_json(directory / "hacktest.json", effective(cfg))
    raw = Path(cfg["_config_path"]).read_bytes()
    if hashlib.sha256(raw).hexdigest() != cfg["_config_sha256"]:
        raise ConfigError("hacktest.json 在读取后变化")
    (directory / "hacktest.input.json").write_bytes(raw)
    sensor = sensor_path(cfg["temperature_sensor"])
    snapshot = {"created_utc": datetime.now(timezone.utc).isoformat(), "parameters": effective(cfg),
                "settings_sha256": cfg["_config_sha256"],
                "effective_settings_sha256": digest(directory / "hacktest.json"),
                "program_sha256": cfg["_program_sha256"], "program_help": cfg["_native_help"],
                "program_version": cfg["_native_version"], "package": cfg["_package"],
                "native_timer_scope_verified": cfg["_native_version"] == "2.11",
                "preflight": cfg["_preflight"], "machine": machine_info(),
                "online_cpus": read("/sys/devices/system/cpu/online"), "scheduler": initial,
                "environment": build_environment(cfg),
                "temperature_sensor": {"path": str(sensor) if sensor else None,
                    "driver": read(sensor.parent / "name") if sensor else None,
                    "label": read(sensor.with_name(sensor.name.replace("_input", "_label"))) if sensor else None,
                    "unit": "°C"}}
    write_json(directory / "snapshot.json", snapshot)
    return snapshot, sensor


def verify_inputs(cfg, directory, snapshot):
    for path, expected in [(cfg["_config_path"], cfg["_config_sha256"]),
                           (directory / "hacktest.input.json", cfg["_config_sha256"]),
                           (directory / "hacktest.json", snapshot["effective_settings_sha256"]),
                           (cfg["program"], snapshot["program_sha256"])]:
        if digest(path) != expected:
            raise ConfigError(f"固定输入发生变化: {path}")
    if sorted(os.sched_getaffinity(0)) != snapshot["machine"]["available_cpus"]:
        raise ConfigError("可用 CPU affinity 变化")
    if read("/sys/devices/system/cpu/online") != snapshot["online_cpus"]:
        raise ConfigError("在线 CPU 变化")


def run_attempt(cfg, directory, snapshot, sensor, scheduler, initial, policy, run, automatic, interrupts):
    folder = policy if policy in POLICIES else "current"
    attempt = directory / f"run-{run:03d}" / folder
    attempt.mkdir(parents=True)
    (attempt / "hackbench.log").touch()
    with (attempt / "samples.csv").open("w", newline="") as stream:
        csv.writer(stream).writerow(SAMPLE_FIELDS)
    row = dict.fromkeys(FIELDS)
    row.update(policy=policy, run=run, valid=False, error="")
    details = {"command": native_command(cfg), "cpu_affinity": cfg["cpus"], "timeout_s": cfg["timeout_s"]}
    cancelled, fatal = False, None
    frozen = {"parameters_snapshot": "../../snapshot.json", "scheduler_name": policy, "scheduler": None, **details}
    write_json(attempt / "snapshot.json", frozen)
    try:
        verify = lambda: verify_inputs(cfg, directory, snapshot)
        verify()
        details["preflight"] = check_limits(cfg)
        expected = scheduler.select(policy, attempt / "scheduler.log") if automatic else initial
        details["expected_scheduler"] = expected
        write_json(attempt / "snapshot.json", {**frozen, "scheduler": expected})
        time.sleep(cfg["cooldown_s"])
        interrupts.check()
        verify()
        scheduler.verify(expected)
        outcome, notes, cancelled = measure(details["command"], attempt, snapshot["environment"],
            attempt / "hackbench.log", attempt / "samples.csv", scheduler, expected, sensor,
            snapshot["machine"]["power"], cfg["sample_interval_s"], cfg["cgroup_parent"], interrupts,
            verify_parameters=verify, cpu_affinity=cfg["cpus"], workload_label="hackbench", timeout_s=cfg["timeout_s"])
        row.update(outcome)
        details.update(notes)
        try:
            metrics, evidence = parse_results(attempt / "hackbench.log", cfg["workload"])
            row.update(metrics)
            details["metric_sources"] = evidence
        except (OSError, ValueError) as exc:
            append_error(row, f"原生结果核验失败: {exc}")
        details.setdefault("missing_metrics", {}).update(
            context_switches="现有共享采样器未采集上下文切换", cpu_migrations="未启用迁移计数",
            cpu_psi="现有共享采样器未采集 CPU PSI")
        if row["cpu_time_s"] is not None and row["wall_s"] and row["wall_s"] > 0:
            details["auxiliary"] = {"avg_cpu_utilization_pct": row["cpu_time_s"] / row["wall_s"] / len(cfg["cpus"]) * 100,
                                    "scope": "cgroup CPU 秒 / wall_s / 固定 CPU 数，辅助估算，不作为主要排名"}
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
        settle=cfg.get("scheduler_settle_s", 5), workload_name="hacktest") if rounds is not None else observer
    metadata = {"schema_version": 1, "workload": "hackbench", "config": effective(cfg),
                "scope": "randomized_blocks" if rounds is not None else "single_current_scheduler",
                "rounds": rounds or 1, "seed": seed if rounds is not None else None,
                "orders": orders, "initial_scheduler": initial, "attempts": [], "status": "running",
                "started_utc": datetime.now(timezone.utc).isoformat(),
                "metrics": {"hackbench_s": "原生 Time:，秒；固定工作量的主要指标，越小越好",
                            "wall_s": "门控 exec 前 SIGCONT 到 wait4 返回的单调时钟秒；超时/中断时为终止到退出的观测耗时",
                            "cpu_time_s": "独立 cgroup 全负载树 CPU 秒；不可用留空",
                            "peak_memory_bytes": "cgroup 峰值字节，包含缓存和内核内存，不含 swap；不可用留空",
                            "temperature": "固定 CPU 传感器，°C；峰值仅为正式运行窗口内采样最高值"}}
    rows, cancelled, fatal = [], False, None
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
                    fatal_error=fatal, final_scheduler=scheduler.snapshot(), analysis=summarize(rows),
                    finished_utc=datetime.now(timezone.utc).isoformat())
                write_json(directory / "metadata.json", metadata)
    return 130 if cancelled else 1 if fatal or any(not row["valid"] for row in rows) else 0


def parser():
    p = argparse.ArgumentParser(description="固定工作量 hackbench 当前策略单测或四策略随机轮", allow_abbrev=False)
    p.add_argument("rounds", nargs="?", help="正整数轮数；省略时仅测试当前调度器一次")
    p.add_argument("--groups", type=int, help="覆盖通信组数")
    p.add_argument("--loops", type=int, help="覆盖每发送者的循环数")
    p.add_argument("--output", type=Path, help="覆盖结果根目录；相对于调用时工作目录")
    return p


def main(argv=None):
    lock = None
    interrupts = Interrupts()
    try:
        args = parser().parse_args(argv)
        if args.rounds is not None and (not re.fullmatch(r"[0-9]+", args.rounds) or int(args.rounds) < 1):
            raise ConfigError("轮数必须为正整数")
        if os.getuid() == 0:
            raise ConfigError("请以普通用户运行 hacktest；只有多轮监督进程提权")
        lock = acquire_lock()
        cfg = load_config(PROJECT / "hacktest.json", args.groups, args.loops, args.output)
        interrupts.install()
        return experiment(cfg, interrupts, rounds=int(args.rounds) if args.rounds is not None else None)
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"hacktest 未能开始: {exc}", file=sys.stderr)
        return 2
    finally:
        interrupts.restore()
        if lock is not None:
            os.close(lock)
