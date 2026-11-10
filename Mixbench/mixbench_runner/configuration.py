"""Validate fixed mixed-workload inputs without calibration or system changes."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile

from compilebench_runner.cli import ConfigError
from compilebench_runner.system import command_output, digest
from schbench_runner.cli import cpuset, finite, load_config as load_front, native_command as front_command

FRONT_COMMIT = "24e32b8ef67cbafb0761aed68747d90a291767ae"
PROJECT = Path(__file__).resolve().parents[1]


def positive(value, key):
    if type(value) is not int or not 1 <= value <= 2**31 - 2:
        raise ConfigError(f"{key} 必须为有效正整数")
    return value


def active_sides(case):
    return ("front", "back") if case == "mixed" else (case,)


def executable(value, base, label):
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{label}.program 必须为单个程序路径")
    path = shutil.which(value) if "/" not in value else base / Path(value).expanduser()
    if not path or not Path(path).is_file() or not os.access(path, os.X_OK):
        hint = "；Fedora 可安装 sysbench 软件包" if label == "back" else "；先在 Schbench 执行 ./build-schbench"
        raise ConfigError(f"{label} 程序不存在或不可执行: {value}{hint}")
    return str(Path(path).resolve())


def protected_paths(cfg, config_path):
    project = Path(cfg["_project"])
    paths = [project / "mixbench_runner", project / "native", project / "tests", project / "bench/src", project / "bench/build"]
    paths += [project.parent / name for name in ("Compile", "Schbench", "Hackbench")]
    paths += [Path(cfg[side]["program"]) for side in ("front", "back") if Path(cfg[side]["program"]).is_absolute()]
    root = Path(cfg["results_dir"])
    if any(root.is_relative_to(p.resolve()) or p.resolve().is_relative_to(root) for p in paths):
        raise ConfigError("结果目录与代码、程序、其他测试或基线重叠")
    if any(p.resolve().is_relative_to(root) for p in (project, Path(config_path), Path.home())):
        raise ConfigError("结果目录不得包含项目、配置或主目录")
    if any(root.is_relative_to(Path(value)) for value in cfg.get("baselines", {}).values() if value):
        raise ConfigError("结果根目录不得写入已有基线批次内部")
    return paths


def load_config(path, case="mixed", rps=None, bg_threads=None, output=None, observe_wakeup=False):
    path = Path(path).resolve()
    raw = path.read_bytes()
    cfg = json.loads(raw)
    if not isinstance(cfg, dict):
        raise ConfigError("mixtest.json 顶层必须为对象")
    if case not in {"mixed", "front", "back"}:
        raise ConfigError("case 必须为 mixed/front/back")
    if observe_wakeup and case == "back":
        raise ConfigError("--observe-wakeup 需要前台；不能用于 --case back")
    # Observation is explicitly enabled by the CLI, never inherited silently from a saved config.
    cfg.pop("wakeup_observation", None)
    cfg.update(case=case, _project=str(PROJECT))
    cfg["cpus"] = cpuset(cfg["cpus"])
    if not set(cfg["cpus"]) <= os.sched_getaffinity(0):
        raise ConfigError("配置 CPU 不在当前可用 CPU 中")
    if os.getpriority(os.PRIO_PROCESS, 0) != 0 or os.sched_getscheduler(0) not in (os.SCHED_OTHER, 7):
        raise ConfigError("请在普通优先级（nice=0、SCHED_OTHER/SCHED_EXT）下运行；脚本不修改优先级")
    positive(cfg.setdefault("runtime_s", 60), "runtime_s")
    for key, value in (("cooldown_s", 30), ("sample_interval_s", 1), ("timeout_s", cfg["runtime_s"] + 60),
                       ("scheduler_timeout_s", 30), ("scheduler_settle_s", 5)):
        finite(cfg.setdefault(key, value), key, positive=key not in {"cooldown_s", "scheduler_settle_s"})
    if cfg["timeout_s"] <= cfg["runtime_s"]:
        raise ConfigError("timeout_s 须大于 runtime_s，超时只用于异常收尾")
    front, back = cfg.get("front", {}), cfg.get("back", {})
    if not isinstance(front, dict) or not isinstance(back, dict):
        raise ConfigError("front/back 须为配置对象")
    front_defaults = {"program": "../Schbench/bench/build/schbench", "provenance": "../Schbench/bench/build/provenance.json",
        "message_threads": 1, "worker_threads": 14, "cache_footprint_kb": 256, "operations": 5,
        "sleep_us": 100, "locking": True}
    back_defaults = {"program": "sysbench", "threads": 14, "cpu_max_prime": 20000}
    if set(front) - set(front_defaults) or set(back) - set(back_defaults):
        raise ConfigError("front/back 包含不支持的参数；不接受任意原生命令")
    front, back = {**front_defaults, **front}, {**back_defaults, **back}
    for key in ("message_threads", "worker_threads", "cache_footprint_kb", "operations"):
        positive(front[key], f"front.{key}")
    if type(front["sleep_us"]) is not int or not 0 <= front["sleep_us"] <= 2**31 - 2 or type(front["locking"]) is not bool:
        raise ConfigError("front.sleep_us 或 locking 无效")
    if bg_threads is not None:
        back["threads"] = bg_threads
    positive(back["threads"], "back.threads")
    positive(back["cpu_max_prime"], "back.cpu_max_prime")
    if rps is not None:
        cfg["rps"] = rps
    cfg.setdefault("rps", 1000)
    if cfg["rps"] is not None:
        positive(cfg["rps"], "rps")
        if cfg["rps"] % front["message_threads"]:
            raise ConfigError("目标 RPS 必须能被 message_threads 整除")
    if case == "mixed" and cfg["rps"] is None:
        raise ConfigError("mixed 须先通过独立预实验确定 rps，写入 mixtest.json 或传入 --rps；可先执行 --case front，不会自动调参")
    finite(cfg.setdefault("rps_tolerance", 0.02), "rps_tolerance")
    if cfg["rps_tolerance"] >= 1:
        raise ConfigError("rps_tolerance 须小于 1")
    if cfg.setdefault("latency_limit_us", 30000) is not None:
        finite(cfg["latency_limit_us"], "latency_limit_us", positive=True)
    if case == "mixed" and cfg["latency_limit_us"] is None:
        raise ConfigError("mixed 须先配置独立预实验确定的 latency_limit_us；可先执行 --case front")
    cfg["front_mode"] = "fixed_rps" if cfg["rps"] is not None else "best_effort"
    sync = cfg.setdefault("synchronization", {})
    if not isinstance(sync, dict) or set(sync) - {"max_start_skew_s", "min_overlap_ratio", "max_native_overhead_s"}:
        raise ConfigError("synchronization 参数无效")
    for key, value in (("max_start_skew_s", 0.1), ("min_overlap_ratio", 0.99), ("max_native_overhead_s", 0.5)):
        finite(sync.setdefault(key, value), key, positive=key != "max_start_skew_s")
    if sync["min_overlap_ratio"] > 1:
        raise ConfigError("min_overlap_ratio 须在 (0,1] 内")
    cfg["baselines"] = cfg.get("baselines", {"front": None, "back": None})
    if not isinstance(cfg["baselines"], dict) or set(cfg["baselines"]) - {"front", "back"}:
        raise ConfigError("baselines 仅支持 front/back 批次目录")
    for side, value in cfg["baselines"].items():
        if value is not None:
            cfg["baselines"][side] = str((path.parent / Path(value).expanduser()).resolve())
    cfg["front"], cfg["back"] = front, back
    cfg["_program_sha256"], cfg["_versions"] = {}, {}
    for side in active_sides(case):
        cfg[side]["program"] = executable(cfg[side]["program"], path.parent, side)
        cfg["_program_sha256"][side] = digest(cfg[side]["program"])
    # Resolve the unused side too when it exists; it is never a dependency of a single-side case.
    for side in {"front", "back"} - set(active_sides(case)):
        value = cfg[side]["program"]
        if "/" in value:
            cfg[side]["program"] = str((path.parent / Path(value).expanduser()).resolve())
    front["provenance"] = str((path.parent / Path(front["provenance"]).expanduser()).resolve())
    if observe_wakeup:
        from .observation import CAPACITY, FILTER_REVISION, ensure_observer
        program, provenance, identity, files = ensure_observer(PROJECT)
        front.update(program=program, provenance=provenance)
        cfg["_program_sha256"]["front"] = digest(program)
        cfg["_observation_files"] = files
        cfg["wakeup_observation"] = {"enabled": True, "capacity_per_worker": CAPACITY,
            **identity, "filter_revision": FILTER_REVISION, "filter_rule": "stopping == 0",
            "note": "汇总 wakeup_p99_us 仅统计运行阶段；原生全量值和原始样本另行保留"}
    if case != "back":
        front_cfg = {"program": front["program"], "provenance": front["provenance"], "cpus": cfg["cpus"],
            "workload": {key: value for key, value in front.items() if key not in {"program", "provenance"}},
            "rps": cfg["rps"], "results_dir": cfg.get("results_dir", "bench/results")}
        front_cfg["workload"].update(runtime_s=cfg["runtime_s"], warmup_s=0, zero_interval_s=0)
        # Reuse schtest validation and native argument construction, not its locked entry point.
        with tempfile.TemporaryDirectory(prefix="mixtest-front-check-") as temporary:
            check = Path(temporary) / "schtest.json"
            front_cfg["results_dir"] = str(Path(temporary) / "results")
            check.write_text(json.dumps(front_cfg))
            cfg["_front"] = load_front(check, fixed=cfg["front_mode"] == "fixed_rps")
        if cfg["_front"].get("_provenance", {}).get("commit") != FRONT_COMMIT:
            raise ConfigError("schbench 计时口径未核对：首版需要现有 Schbench/build-schbench 的固定提交")
        cfg["_versions"]["front"] = {"help": cfg["_front"]["_native_help"], "provenance": cfg["_front"]["_provenance"]}
    if case != "front":
        infos = {"version": command_output([back["program"], "--version"]),
                 "help": command_output([back["program"], "--help"]),
                 "cpu_help": command_output([back["program"], "cpu", "help"])}
        if any(info.get("exit_code") != 0 for info in infos.values()):
            raise ConfigError("无法核对 sysbench --version/--help/cpu help")
        match = re.search(r"sysbench\s+(\d+\.\d+\.\d+)(?![\d.])", infos["version"].get("output", ""))
        if not match or match[1] != "1.0.20":
            raise ConfigError("sysbench 计时口径未核对：首版支持 1.0.20；不猜测其他版本")
        if any(flag not in infos["help"]["output"] for flag in ("--threads", "--time", "--events", "--rate", "--report-interval")) or "--cpu-max-prime" not in infos["cpu_help"]["output"]:
            raise ConfigError("sysbench help 未确认所需参数")
        cfg["_back_warmup_time_supported"] = "--warmup-time" in infos["help"]["output"]
        infos["warmup"] = {"requested_s": 0, "option_supported": cfg["_back_warmup_time_supported"],
            "behavior": "显式 --warmup-time=0" if cfg["_back_warmup_time_supported"] else
                "本地 sysbench 1.0.20 未实现 warmup-time 选项；省略该选项，原生 CPU 测试直接计时，不增加预热"}
        cfg["_versions"]["back"] = infos
    cfg["results_dir"] = str(Path(output).expanduser().resolve() if output is not None else
        (path.parent / Path(cfg.get("results_dir", "bench/results")).expanduser()).resolve())
    for key in ("temperature_sensor", "cgroup_parent"):
        if cfg.setdefault(key, None):
            cfg[key] = str((path.parent / Path(cfg[key]).expanduser()).resolve())
    cfg.update(_config_path=str(path), _config_sha256=hashlib.sha256(raw).hexdigest())
    protected_paths(cfg, path)
    return cfg


def commands(cfg, attempt):
    result = {}
    if cfg["case"] != "back":
        result["front"] = front_command(cfg["_front"], attempt)
        if cfg["_front"]["_native_json"]:
            result["front"][-1] = str(attempt / "front-native.json")
    if cfg["case"] != "front":
        b = cfg["back"]
        result["back"] = [b["program"], "cpu", f"--threads={b['threads']}", f"--cpu-max-prime={b['cpu_max_prime']}",
            f"--time={cfg['runtime_s']}", "--events=0", "--rate=0"]
        if cfg["_back_warmup_time_supported"]:
            result["back"].append("--warmup-time=0")
        result["back"].extend(["--report-interval=1", "run"])
    return result
