"""Run the C collector in the existing coordinator, outside both load groups."""
from collections import Counter
import csv
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import time

from compilebench_runner.system import write_json

SOURCE = Path(__file__).resolve().parents[2] / "Collector"


def prepare(cfg):
    """Build before the first attempt and freeze the actual executable/object."""
    options = cfg.get("collector", {})
    bpf_object = options.get("bpf_object")
    have_object = bool(bpf_object and Path(bpf_object).is_file())
    command = ["make", "-s", "-C", str(SOURCE), "all"]
    mode = "core"
    if have_object:
        optional = subprocess.run([*command, "WITH_BPF=1"], capture_output=True, text=True, timeout=60)
        if optional.returncode == 0:
            mode = "libbpf"
        else:
            options = {**options, "probe_build_error": optional.stderr.strip() or optional.stdout.strip()}
    if mode == "core":
        result = subprocess.run([*command, "WITH_BPF=0"], capture_output=True, text=True, timeout=60)
        if result.returncode:
            raise RuntimeError(f"无法构建 Wisp 采集器: {result.stderr.strip() or result.stdout.strip()}")
    binary = SOURCE / "bench/build" / mode / "wisp-collector"
    files = [str(binary)]
    if have_object:
        files.append(bpf_object)
    return {**options, "binary": str(binary), "files": files}


def start(processes, spec, attempt, cgroups, interrupts, *, samples=None):
    options = spec["collector"]
    command = [options["binary"], "--output", str(attempt),
               "--cpus", ",".join(map(str, spec["cpus"])),
               "--interval", str(spec["sample_interval_s"])]
    if options.get("prefix"):
        command.extend(["--prefix", options["prefix"]])
    for side, flag in (("front", "--fg-cgroup"), ("back", "--bg-cgroup")):
        if cgroups.get(side):
            command.extend([flag, str(cgroups[side])])
    for option, flag in (("device", "--device"), ("bpf_object", "--bpf-object")):
        if options.get(option):
            command.extend([flag, options[option]])
    if options.get("runqueue_verified"):
        command.append("--runqueue-verified")
    authorized = options.get("authorized", False)
    if samples is not None:
        command.extend(["--samples", str(samples)])
    if authorized:
        command = ["sudo", "-n", "--", *command, "--sudo-attach"]
    with (attempt / "collector.log").open("wb") as log, interrupts.defer():
        if options.get("probe_build_error"):
            log.write((options["probe_build_error"] + "\n").encode())
            log.flush()
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=log,
                                   stdin=subprocess.PIPE if authorized else subprocess.DEVNULL)
        processes.append(process)
        process.collector_pidfd = None
    with process.stdout as pipe:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            interrupts.check()
            if select.select([pipe], [], [], 0.05)[0]:
                reply = pipe.readline().split()
                if authorized and len(reply) == 2 and reply[0] == b"ready" and reply[1].isdigit():
                    process.collector_pidfd = os.pidfd_open(int(reply[1]))
                    return
                if not authorized and reply == [b"ready"]:
                    return
                raise RuntimeError("采集器未完成首轮快照，见 collector.log")
    raise RuntimeError("采集器首轮快照超时，见 collector.log")


def stop(process):
    pidfd = process.collector_pidfd
    try:
        if process.stdin:
            process.stdin.close()
        if process.poll() is None:
            try:
                if pidfd is not None:
                    signal.pidfd_send_signal(pidfd, signal.SIGTERM)
                elif not process.stdin:
                    process.terminate()
            except ProcessLookupError:
                pass
        try:
            return process.wait(timeout=5 if process.stdin else 1)
        except subprocess.TimeoutExpired:
            if pidfd is not None:
                try:
                    signal.pidfd_send_signal(pidfd, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                return process.wait(timeout=5)
            process.terminate()
            return process.wait(timeout=5)
    finally:
        if pidfd is not None:
            os.close(pidfd)
            process.collector_pidfd = None


SCHED_COLUMNS = ("sched_wakeups_per_sec", "sched_wakeup_latency_p95_us",
                 "sched_migrations_per_sec", "context_switches_per_cpu_variance")


def verify_probe_output(meta):
    if meta["probes"]["scheduler"] != "attached" or meta["probes"]["runqueue"] != "unverified":
        raise ValueError(f"探针状态不符合要求: {meta['probes']}")
    missing = [name for name in SCHED_COLUMNS if not meta["indicator_status_counts"][name].get("ok")]
    if missing:
        raise ValueError("本次没有有效的调度事件值: " + ", ".join(missing))


def finalize(attempt, evidence, valid, error, prefix="workload", authorized=False):
    """Record exact observed process bounds and eligible per-source windows."""
    path = attempt / f"{prefix}-meta.json"
    summary = {"status": "failed" if error else "unavailable", "error": error}
    if not path.exists():
        return summary
    meta = json.loads(path.read_text())
    children = evidence.get("children", {})
    begin = end = None
    if set(children) == {"front", "back"}:
        starts = [child.get("exec_monotonic_ns") for child in children.values()]
        ends = [child.get("last_alive_ns") for child in children.values()]
        if None not in starts and None not in ends:
            begin, end = max(starts), min(ends)
    status_counts = {col["name"]: Counter() for col in meta["columns"]}
    eligible = []
    rows = 0
    with (attempt / f"{prefix}-status.jsonl").open() as stream, (attempt / f"{prefix}.csv").open(newline="") as data:
        reader = csv.DictReader(data)
        for lineno, line in enumerate(stream, 1):
            sample = json.loads(line)
            timestamp = sample["timestamp_monotonic_ns"]
            record = next(reader, None)
            if record is None or record["timestamp_monotonic_ns"] != str(timestamp):
                raise ValueError("workload.csv 与状态记录的采样时间不一致")
            if len(sample["indicators"]) != len(status_counts):
                raise ValueError("workload 样本指标数不符合 schema")
            rows += 1
            windows = []
            for (name, counts), (status, start_ns, end_ns) in zip(
                    status_counts.items(), sample["indicators"]):
                if authorized and name in SCHED_COLUMNS and status == "ok" and not record.get(name):
                    raise ValueError(f"调度指标 {name} 标为有效但 CSV 为空")
                counts[status] += 1
                if status == "ok":
                    windows.append((start_ns, end_ns))
            if (valid and not error and begin is not None and end is not None and
                    begin <= timestamp <= end and windows and
                    all(begin <= start <= finish <= end for start, finish in windows)):
                eligible.append(lineno)
        if next(reader, None) is not None:
            raise ValueError("workload.csv 中存在无状态记录的样本")
    summary = {"status": "failed" if error else "complete", "error": error,
               "schema_version": meta["schema_version"], "samples": rows,
               "training_samples": len(eligible), "metadata": path.name, "csv": f"{prefix}.csv"}
    meta.update(collection=summary, indicator_status_counts=status_counts,
                joint_process_window_ns={"start": begin, "end": end},
                joint_window_basis="later pre-exec marker to earlier last confirmed alive poll",
                training_requires_valid_attempt=True, training_sample_rows_1based=eligible)
    if authorized and not error and not evidence.get("collector_interrupted"):
        try:
            verify_probe_output(meta)
            if "scheduler probe descriptors closed" not in (attempt / "collector.log").read_text():
                raise ValueError("未确认授权采集器关闭探针描述符")
        except (OSError, ValueError) as exc:
            summary.update(status="failed", error=str(exc))
    write_json(path, meta)
    return summary
