"""Kernel compile experiment orchestration. No installation targets are allowed."""
import csv
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
import random
import re
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time

from .policy import PolicyError, Scheduler, policy_name
from .interrupts import Interrupts
from .preflight import check_environment
from .system import command_output, digest, machine_info, read, sensor_path, write_json
from .workload import SAMPLE_FIELDS, measure, terminate_group


class ConfigError(ValueError):
    pass


def resolve_command(value, name):
    if not isinstance(value, list) or not value or not all(isinstance(x, str) and x for x in value):
        raise ConfigError(f"{name} 必须是非空字符串数组")
    executable = shutil.which(value[0])
    if not executable:
        raise ConfigError(f"缺少工具 {value[0]}；请安装对应软件包并检查 PATH")
    return [str(Path(executable).resolve()), *value[1:]]


def load_config(path):
    path = path.resolve()
    raw = path.read_bytes()
    cfg = json.loads(raw)
    if not isinstance(cfg, dict):
        raise ConfigError("配置顶层必须是 JSON 对象")
    for key in ("source", "kernel_config", "results_dir"):
        cfg[key] = str((path.parent / Path(cfg[key]).expanduser()).resolve())
    cfg.setdefault("build_root", str(Path(cfg["results_dir"]).parent / "compiletest-builds"))
    cfg["build_root"] = str((path.parent / Path(cfg["build_root"]).expanduser()).resolve())
    cfg.setdefault("jobs", len(os.sched_getaffinity(0)))
    if type(cfg["jobs"]) is not int or cfg["jobs"] < 1:
        raise ConfigError("jobs 必须为正整数")
    for key, default in (("cooldown_s", 30), ("sample_interval_s", 1)):
        cfg.setdefault(key, default)
        if type(cfg[key]) not in (int, float) or not math.isfinite(cfg[key]) or cfg[key] < 0:
            raise ConfigError(f"{key} 必须为有限非负数")
    if cfg["sample_interval_s"] <= 0:
        raise ConfigError("采样间隔必须大于 0")
    targets = cfg.setdefault("targets", ["vmlinux"])
    allowed = {"all", "vmlinux", "bzImage", "Image", "Image.gz", "zImage", "uImage", "modules", "dtbs"}
    if not isinstance(targets, list) or not targets or any(t not in allowed for t in targets):
        raise ConfigError(f"仅允许构建目标: {', '.join(sorted(allowed))}；禁止安装目标")
    cfg["make"] = resolve_command(cfg.get("make", ["make"]), "make")
    if len(cfg["make"]) != 1:
        raise ConfigError("make 只接受可执行文件；目标和并行度使用专用配置字段")
    cfg["compiler"] = resolve_command(cfg.get("compiler", ["gcc"]), "compiler")
    if Path(cfg["compiler"][0]).name in {"ccache", "sccache", "distcc", "icecc"}:
        raise ConfigError("compiler 必须直接调用本地编译器，不使用缓存或分布式包装器")
    cfg.setdefault("required_tools", ["ld", "as", "flex", "bison", "perl", "bc", "openssl"])
    if not isinstance(cfg["required_tools"], list) or any(not isinstance(tool, str) for tool in cfg["required_tools"]):
        raise ConfigError("required_tools 必须为工具名字符串数组")
    for tool in cfg["required_tools"]:
        resolve_command([tool], "required_tools")
    source, kernel_config = Path(cfg["source"]), Path(cfg["kernel_config"])
    if not (source / "Makefile").is_file() or not kernel_config.is_file():
        raise ConfigError("source 须为含 Makefile 的内核源码，kernel_config 须为可读固定配置")
    if (source / ".config").exists() or (source / "include/config").exists() or (source / "include/generated").exists():
        raise ConfigError("源码树存在原地构建配置/产物；请使用独立干净源码副本，不会自动执行 mrproper")
    for key in ("results_dir", "build_root"):
        if Path(cfg[key]).is_relative_to(source):
            raise ConfigError(f"{key} 必须位于源码树之外")
    if cfg["build_root"] == cfg["results_dir"]:
        raise ConfigError("build_root 和 results_dir 必须不同")
    config_text = kernel_config.read_text() if kernel_config.is_file() else ""
    if "CONFIG_DEBUG_INFO_BTF=y" in config_text:
        resolve_command(["pahole"], "CONFIG_DEBUG_INFO_BTF")
    if cfg.get("temperature_sensor"):
        cfg["temperature_sensor"] = str((path.parent / cfg["temperature_sensor"]).resolve())
    if cfg.get("cgroup_parent"):
        cfg["cgroup_parent"] = str(Path(cfg["cgroup_parent"]).resolve())
    cfg["_config_path"] = str(path)
    cfg["_config_sha256"] = hashlib.sha256(raw).hexdigest()
    return cfg


def build_environment(cfg):
    # Do not inherit MAKEFLAGS, KBUILD_OUTPUT, CC or compiler cache wrappers.
    environment = {key: os.environ[key] for key in ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TMPDIR")
                   if key in os.environ}
    environment.update(CCACHE_DISABLE="1", SCCACHE_DISABLE="1", RUSTC_WRAPPER="",
                       KBUILD_BUILD_USER="compiletest", KBUILD_BUILD_HOST="compiletest",
                       KBUILD_BUILD_TIMESTAMP="Thu Jan 1 00:00:00 UTC 1970", KBUILD_BUILD_VERSION="1")
    return environment


def make_command(cfg, build, targets, jobs=None):
    return [*cfg["make"], "-C", cfg["source"], f"O={build}",
            f"-j{jobs or cfg['jobs']}", f"CC={shlex.join(cfg['compiler'])}",
            f"HOSTCC={shlex.join(cfg['compiler'])}", *targets]


def manifest(source):
    paths = []
    for directory, dirs, files in os.walk(source, followlinks=False):
        for name in [*dirs, *files]:
            path = Path(directory, name)
            if path.is_symlink():
                if not path.resolve(strict=True).is_relative_to(source):
                    raise ConfigError(f"源码符号链接指向树外，无法固定输入: {path}")
                paths.append(path.relative_to(source).as_posix())
        dirs[:] = sorted(d for d in dirs if d != ".git" and not Path(directory, d).is_symlink())
        for filename in sorted(files):
            path = Path(directory, filename)
            if path.is_symlink():
                continue
            if path.is_file():
                paths.append(path.relative_to(source).as_posix())
    return sorted(paths)


def preread(source, paths):
    checksum = hashlib.sha256()
    size = 0
    for relative in paths:
        path = source / relative
        checksum.update(relative.encode() + b"\0")
        if path.is_symlink():
            checksum.update(b"symlink\0" + os.readlink(path).encode() + b"\0")
            if path.is_dir():
                continue  # Internal directory targets are traversed at their real location.
        checksum.update(bytes.fromhex(digest(path)))
        size += path.stat().st_size
    return {"sha256": checksum.hexdigest(), "bytes": size, "files": len(paths)}


def run_preparation(command, log_path, environment, interrupts=None):
    interrupts = interrupts or Interrupts()
    with open(log_path, "ab") as log:
        with interrupts.defer():
            process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                                       env=environment, start_new_session=True)
        try:
            interrupts.check()
            code = process.wait()
        finally:
            with interrupts.defer():
                if process.poll() is None:
                    terminate_group(process.pid)
                    process.wait()
        if code != 0:
            raise RuntimeError(f"配置准备失败，退出码 {code}；见 {log_path}")


def acquire_lock():
    # Shared with Clean, so records cannot be erased during a test.
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp"))
    try:
        fd = os.open(runtime / f"compiletest-{os.getuid()}.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    except (PermissionError, OSError) as exc:
        if runtime == Path("/tmp") or exc.errno not in {13, 30}:
            raise
        fd = os.open(f"/tmp/compiletest-{os.getuid()}.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    if os.fstat(fd).st_uid != os.getuid():
        os.close(fd)
        raise ConfigError("锁文件属主不匹配")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        os.close(fd)
        raise ConfigError("已有同用户 compiletest 正在运行") from exc
    return fd


SUMMARY_FIELDS = ["policy", "run", "valid", "exit_code", "wall_s", "cpu_time_s",
                  "peak_memory_bytes", "temp_start_c", "temp_peak_sampled_c", "error"]


def scheduler_specs(cfg):
    from .control import POLICIES
    specs = {}
    for policy in POLICIES[1:]:
        spec = dict(cfg.get("schedulers", {}).get(policy, {}))
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", spec.get("ops", "")):
            raise ConfigError(f"schedulers.{policy} 需要填写完整、精确的 root/ops")
        spec["command"] = resolve_command(spec.get("command", [policy]), policy)
        specs[policy] = spec
    for key, default in (("scheduler_timeout_s", 30), ("scheduler_settle_s", 5)):
        value = cfg.get(key, default)
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ConfigError(f"{key} 必须为有限非负数")
    if cfg.get("scheduler_timeout_s", 30) <= cfg.get("scheduler_settle_s", 5):
        raise ConfigError("scheduler_timeout_s 必须大于 scheduler_settle_s")
    return specs


def save_inputs(cfg, result_dir, initial, rounds):
    config_path = Path(cfg["_config_path"])
    saved_settings = result_dir / "compiletest.json"
    saved_settings.write_bytes(config_path.read_bytes())
    if digest(saved_settings) != cfg["_config_sha256"]:
        raise ConfigError("compiletest.json 在读取参数后发生变化")
    saved_config = result_dir / "kernel.config"
    shutil.copyfile(cfg["kernel_config"], saved_config)
    source = Path(cfg["source"])
    paths = manifest(source)
    baseline = preread(source, paths)
    (result_dir / "source-manifest.txt").write_text("\n".join(paths) + "\n")
    sensor = sensor_path(cfg.get("temperature_sensor"))
    snapshot = {"created_utc": datetime.now(timezone.utc).isoformat(), "parameters": cfg,
                "settings_sha256": cfg["_config_sha256"], "kernel_config_sha256": digest(saved_config),
                "source": baseline, "machine": machine_info(),
                "scheduler_name": policy_name(initial) if rounds is None else None,
                "scheduler": initial, "build_environment": build_environment(cfg),
                "temperature_sensor": str(sensor) if sensor else None,
                "compiler": command_output([*cfg["compiler"], "--version"]),
                "make": command_output([*cfg["make"], "--version"]),
                "source_makefile": read(source / "Makefile")}
    write_json(result_dir / "snapshot.json", snapshot)
    return snapshot, paths, sensor


def verify_inputs(cfg, result_dir, snapshot, build=None):
    for path, expected_hash in ((Path(cfg["_config_path"]), snapshot["settings_sha256"]),
                                (result_dir / "compiletest.json", snapshot["settings_sha256"]),
                                (Path(cfg["kernel_config"]), snapshot["kernel_config_sha256"]),
                                (result_dir / "kernel.config", snapshot["kernel_config_sha256"])):
        if digest(path) != expected_hash:
            raise ConfigError(f"参数快照不匹配，文件被改变: {path}")
    if build and digest(build / ".config") != snapshot["kernel_config_sha256"]:
        raise ConfigError(f"参数快照不匹配，文件被改变: {build / '.config'}")
    if sorted(os.sched_getaffinity(0)) != snapshot["machine"]["available_cpus"]:
        raise ConfigError("参数快照不匹配，可用 CPU 发生变化")


def run_attempt(cfg, result_dir, build_batch, snapshot, paths, sensor, scheduler,
                initial, policy, run, automatic, interrupts):
    folder = policy if policy in {"default", "scx_lavd", "scx_bpfland", "scx_rusty"} else "current"
    attempt = result_dir / f"run-{run:03d}" / folder
    build = build_batch / f"run-{run:03d}-{folder}"
    attempt.mkdir(parents=True)
    build.mkdir()
    (attempt / "build.log").touch()
    with (attempt / "samples.csv").open("w", newline="") as stream:
        csv.writer(stream).writerow(SAMPLE_FIELDS)
    row = dict.fromkeys(SUMMARY_FIELDS)
    row.update(policy=policy, run=run, valid=False, error="")
    details = {"make_command": make_command(cfg, build, cfg["targets"]),
               "prepare_command": make_command(cfg, build, ["olddefconfig"], 1), "build_dir": str(build)}
    source, environment = Path(cfg["source"]), snapshot["build_environment"]
    interrupted, fatal = False, None
    try:
        verify_inputs(cfg, result_dir, snapshot)
        shutil.copyfile(result_dir / "kernel.config", build / ".config")
        run_preparation(details["prepare_command"], attempt / "prepare.log", environment, interrupts)
        if digest(build / ".config") != snapshot["kernel_config_sha256"]:
            raise ConfigError("olddefconfig 改变了固定 .config；请先整理冻结配置")
        expected = scheduler.select(policy, attempt / "scheduler.log") if automatic else initial
        details["expected_scheduler"] = expected
        attempt_snapshot = {"parameters_snapshot": "../../snapshot.json", "scheduler_name": policy,
                            "scheduler": expected, **details}
        write_json(attempt / "snapshot.json", attempt_snapshot)
        if not automatic:
            # Keep the established single-build snapshot fields and layout.
            write_json(result_dir / "snapshot.json", {**snapshot, **details})
        verify = lambda: verify_inputs(cfg, result_dir, snapshot, build)
        verify()
        if manifest(source) != paths or preread(source, paths) != snapshot["source"]:
            raise ConfigError("源码输入与参数快照不匹配")
        time.sleep(cfg["cooldown_s"])
        scheduler.verify(expected)
        outcome, notes, interrupted = measure(
            details["make_command"], source, environment, attempt / "build.log",
            attempt / "samples.csv", scheduler, expected, sensor, snapshot["machine"]["power"],
            cfg["sample_interval_s"], cfg.get("cgroup_parent"), interrupts,
            verify_parameters=verify)
        row.update(outcome)
        details.update(notes)
        verify()
        if manifest(source) != paths or preread(source, paths) != snapshot["source"]:
            raise ConfigError("构建结束时源码输入与参数快照不匹配")
        scheduler.verify(expected)
    except KeyboardInterrupt:
        interrupted = True
        row.update(valid=False, error=(row["error"] + "; 用户中断").strip("; "))
    except Exception as exc:
        row.update(valid=False, error=(row["error"] + "; " + str(exc)).strip("; "))
    finally:
        with interrupts.defer():
            if automatic:
                try:
                    issue = scheduler.stop()
                    if issue:
                        row.update(valid=False, error=(row["error"] + "; " + issue).strip("; "))
                    scheduler.verify(scheduler.initial())
                except Exception as exc:
                    fatal = str(exc)
                    row.update(valid=False, error=(row["error"] + "; " + fatal).strip("; "))
            interrupted = interrupted or interrupts.requested
            if interrupted:
                row["valid"] = False
                if "中断" not in row["error"]:
                    row["error"] = (row["error"] + "; 用户中断").strip("; ")
            details["result"] = row
            write_json(attempt / "attempt.json", details)
            if interrupts.requested and not interrupted:
                interrupted = True
                row.update(valid=False, error=(row["error"] + "; 用户中断（结果保存阶段）").strip("; "))
                write_json(attempt / "attempt.json", details)
    return row, interrupted, fatal, details, str(attempt.relative_to(result_dir))


def experiment(cfg, interrupts=None, rounds=None, seed=None):
    """Use one measurement path for current-policy and randomized-block modes."""
    from .control import BatchScheduler, POLICIES
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
    result_dir = Path(cfg["results_dir"]) / batch
    build_batch = Path(cfg["build_root"]) / batch
    result_dir.mkdir(parents=True)
    build_batch.mkdir(parents=True)
    scheduler = (BatchScheduler(specs, result_dir, interrupts,
                               timeout=cfg.get("scheduler_timeout_s", 30),
                               settle=cfg.get("scheduler_settle_s", 5)) if rounds is not None else observer)
    metadata = {"schema_version": 3, "mode": "randomized_blocks" if rounds is not None else "single_current_scheduler",
                "config": cfg, "rounds": rounds or 1, "seed": seed if rounds is not None else None,
                "orders": orders, "initial_scheduler": initial, "attempts": [], "status": "running",
                "started_utc": datetime.now(timezone.utc).isoformat(),
                "metrics": {"wall_s": "正式 make 的单调时钟耗时；准备、切换、稳定等待、冷却及收尾在计时外",
                            "cpu_time_s": "独立编译 cgroup 整个进程树 CPU 时间；不可用则留空",
                            "peak_memory_bytes": "独立编译 cgroup 峰值；含文件缓存和内核内存，不含 swap",
                            "temperature": "同一 CPU 温度传感器，摄氏度；正式构建窗口内采样最大值"}}
    if rounds is None:
        metadata["scheduler_name"] = policy_name(initial)
    rows, interrupted, fatal = [], False, None
    write_json(result_dir / "metadata.json", metadata)
    with (result_dir / "summary.csv").open("w", newline="") as summary:
        writer = csv.DictWriter(summary, fieldnames=SUMMARY_FIELDS)
        writer.writeheader()
        summary.flush()
        try:
            metadata["preflight"] = check_environment(cfg)
            if rounds is not None:
                scheduler.open()  # Every required privilege is acquired before any build.
                metadata["scheduler_commands"] = specs
                metadata["scheduler_controller"] = scheduler.info
            snapshot, paths, sensor = save_inputs(cfg, result_dir, initial, rounds)
            metadata["snapshot"] = "snapshot.json"
            write_json(result_dir / "metadata.json", metadata)
            for run, order in enumerate(orders, 1):
                for policy in order:
                    row, cancelled, issue, details, relative = run_attempt(
                        cfg, result_dir, build_batch, snapshot, paths, sensor, scheduler,
                        initial, policy, run, rounds is not None, interrupts)
                    with interrupts.defer():
                        writer.writerow(row)
                        summary.flush()
                        os.fsync(summary.fileno())
                        rows.append(row)
                        metadata["attempts"].append(relative)
                        if rounds is None:
                            metadata.update(result=row, build_dir=details["build_dir"], attempt=relative)
                        write_json(result_dir / "metadata.json", metadata)
                    interrupted = cancelled or interrupts.requested
                    fatal = issue or fatal
                    if interrupted or fatal:
                        break
                if interrupted or fatal:
                    break
        except KeyboardInterrupt:
            interrupted = True
        except Exception as exc:
            fatal = str(exc)
        finally:
            with interrupts.defer():
                if rounds is not None:
                    try:
                        scheduler.close()
                    except Exception as exc:
                        fatal = (fatal + "; " + str(exc)).strip("; ") if fatal else str(exc)
                    metadata["scheduler_controller"] = scheduler.info
                interrupted = interrupted or interrupts.requested
                metadata.update(status="interrupted" if interrupted else "failed" if fatal or any(not r["valid"] for r in rows) else "completed",
                                fatal_error=fatal, final_scheduler=scheduler.snapshot(),
                                finished_utc=datetime.now(timezone.utc).isoformat())
                write_json(result_dir / "metadata.json", metadata)
    return 130 if interrupted else 1 if fatal or any(not row["valid"] for row in rows) else 0


def main(argv=None):
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) > 1 or (arguments and (not re.fullmatch(r"[0-9]+", arguments[0]) or int(arguments[0]) < 1)):
        print("用法: ./compiletest 或 ./compiletest n（n 为正整数轮数）", file=sys.stderr)
        return 2
    rounds = int(arguments[0]) if arguments else None
    lock = None
    interrupts = Interrupts()
    try:
        if os.getuid() == 0:
            raise ConfigError("请以普通用户运行 compiletest")
        lock = acquire_lock()
        cfg = load_config(Path("compiletest.json"))
        interrupts.install()
        return experiment(cfg, interrupts, rounds=rounds)
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"测试未能开始: {exc}", file=sys.stderr)
        return 2
    finally:
        interrupts.restore()
        if lock is not None:
            os.close(lock)
