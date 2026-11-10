"""Unprivileged two-child gate; all descendants stay in the outer owned session."""
import json
import os
from pathlib import Path
import select
import signal
import sys
import time

# This file is exec'd directly by the shared gated measurement function.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mixbench_runner
from compilebench_runner.interrupts import Interrupts
from compilebench_runner.metrics import BuildCgroup
from compilebench_runner.policy import Scheduler
from compilebench_runner.system import read, temperature, write_json


def subgroup_layout(sides):
    groups, manager, parent = {}, None, None
    info = {"scope": "共同父 cgroup 汇总，不相加两侧内存峰值"}
    try:
        entry = next(line[3:] for line in Path("/proc/self/cgroup").read_text().splitlines() if line.startswith("0::"))
        parent = Path("/sys/fs/cgroup") / entry.lstrip("/")
        # Do not touch an ambient/external group when the shared sampler has no delegation.
        if not parent.name.startswith("compiletest-") or parent.stat().st_uid != os.getuid():
            raise OSError("共同独立 cgroup 不可用")
        manager = parent / "coordinator"
        manager.mkdir()
        (manager / "cgroup.procs").write_text(str(os.getpid()))
        available = (read(parent / "cgroup.controllers") or "").split()
        needed = [name for name in ("cpu", "memory") if name in available]
        if needed:
            (parent / "cgroup.subtree_control").write_text(" ".join("+" + name for name in needed))
        for side in sides:
            groups[side] = BuildCgroup(parent)
            if not groups[side].path:
                info.setdefault("missing", {})[side] = groups[side].reason
        info["parent"] = str(parent)
        info["children"] = {name: str(group.path) if group.path else None for name, group in groups.items()}
    except (OSError, StopIteration) as exc:
        info["missing"] = str(exc)
    # The outer BuildCgroup owns and removes the entire unique subtree afterwards.
    return groups, info


def launch(spec, path, interrupts, evidence):
    children, pipes = {}, {}
    groups, group_info = subgroup_layout(spec["commands"])
    evidence.update(children=children, cgroups=group_info, side_samples=[])
    exit_status = 1
    try:
        for side, command in spec["commands"].items():
            read_fd, write_fd = os.pipe()  # CLOEXEC; child writes immediately before native exec.
            with interrupts.defer():
                pid = os.fork()
                if pid:
                    children[side] = {"pid": pid, "command": command, "exec_monotonic_s": None,
                        "exit_monotonic_s": None, "exit_code": None, "reaped": False}
                    pipes[side] = read_fd
            if pid == 0:
                try:
                    for fd in pipes.values():
                        os.close(fd)
                    os.close(read_fd)
                    signal.signal(signal.SIGINT, signal.SIG_DFL)
                    signal.signal(signal.SIGTERM, signal.SIG_DFL)
                    with (path.parent / f"{side}.log").open("ab", buffering=0) as log:
                        os.dup2(log.fileno(), 1)
                        os.dup2(log.fileno(), 2)
                    os.sched_setaffinity(0, spec["cpus"])
                    os.kill(os.getpid(), signal.SIGSTOP)
                    os.write(write_fd, f"{time.monotonic():.9f}\n".encode())
                    environment = os.environ.copy()
                    # Trace only the frontend and never inherit stale output paths from the terminal.
                    environment.pop("WISP_WAKEUP_TRACE_DIR", None)
                    environment.pop("WISP_WAKEUP_CAPACITY", None)
                    if side == "front":
                        environment.update(spec.get("front_environment", {}))
                    os.execvpe(command[0], command, environment)
                except BaseException as exc:
                    os.write(2, f"native exec failed: {exc}\n".encode())
                    os._exit(127)
            os.close(write_fd)
            interrupts.check()
            _, status = os.waitpid(pid, os.WUNTRACED)
            if not os.WIFSTOPPED(status):
                children[side].update(reaped=True, exit_code=os.waitstatus_to_exitcode(status), exit_monotonic_s=time.monotonic())
                raise RuntimeError(f"{side} 未进入共同启动门控")
            if side in groups:
                groups[side].attach(pid)
            Scheduler().verify(spec["expected_scheduler"], pid)
            children[side].update(affinity=sorted(os.sched_getaffinity(pid)), nice=os.getpriority(os.PRIO_PROCESS, pid),
                                  scheduling_policy=os.sched_getscheduler(pid))
            write_json(path, evidence)
        with interrupts.defer():
            Scheduler().verify(spec["expected_scheduler"])
            evidence["temp_start_c"] = temperature(spec.get("sensor"))
            evidence["released_monotonic_s"] = time.monotonic()
            for child in children.values():
                os.kill(child["pid"], signal.SIGCONT)
        buffers = {side: b"" for side in pipes}
        # Consume timestamps without blocking on a stopped/failed child indefinitely.
        deadline = time.monotonic() + 5
        while pipes:
            interrupts.check()
            if time.monotonic() >= deadline:
                raise RuntimeError("负载 exec 时间戳超时")
            readable, _, _ = select.select(list(pipes.values()), [], [], 0.05)
            for side, fd in list(pipes.items()):
                if fd not in readable:
                    continue
                data = os.read(fd, 4096)
                buffers[side] += data
                if not data or b"\n" in buffers[side]:
                    os.close(fd)
                    del pipes[side]
                    if not buffers[side]:
                        raise RuntimeError(f"{side} 缺少 exec 时间戳")
                    children[side]["exec_monotonic_s"] = float(buffers[side].strip())
        write_json(path, evidence)
        next_sample = 0
        while any(not child["reaped"] for child in children.values()):
            interrupts.check()
            for side, child in children.items():
                if child["reaped"]:
                    continue
                pid, status = os.waitpid(child["pid"], os.WNOHANG)
                if pid:
                    child.update(exit_monotonic_s=time.monotonic(), exit_code=os.waitstatus_to_exitcode(status), reaped=True)
                    write_json(path, evidence)
                    if child["exit_code"] != 0:
                        raise RuntimeError(f"{side} 失败，退出码 {child['exit_code']}")
                elif os.sched_getaffinity(child["pid"]) != set(spec["cpus"]) or os.getpriority(os.PRIO_PROCESS, child["pid"]) != 0:
                    raise RuntimeError(f"{side} affinity 或 nice 变化")
            if time.monotonic() >= next_sample:
                evidence["side_samples"].append({"monotonic_s": time.monotonic(),
                    **{side: group.values() for side, group in groups.items()}})
                next_sample = time.monotonic() + spec["sample_interval_s"]
            time.sleep(0.01)
        exit_status = 0
    finally:
        # Shared outer session/cgroup cleanup also covers descendants if this worker is killed.
        with interrupts.defer():
            alive = [child for child in children.values() if not child["reaped"]]
            for child in alive:
                try:
                    os.kill(child["pid"], signal.SIGTERM)
                    os.kill(child["pid"], signal.SIGCONT)
                except ProcessLookupError:
                    pass
            if alive:
                time.sleep(0.2)
            for child in alive:
                try:
                    os.kill(child["pid"], signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    _, status = os.waitpid(child["pid"], 0)
                    child.update(reaped=True, exit_code=os.waitstatus_to_exitcode(status), exit_monotonic_s=time.monotonic(), terminated_by_coordinator=True)
                except ChildProcessError:
                    child["status_missing"] = True
            for fd in pipes.values():
                os.close(fd)
            for side, group in groups.items():
                children.get(side, {}).update(resources=group.values(), missing_metrics=group.notes or ({"all": group.reason} if group.reason else {}))
                if group.pids():
                    group.kill_remaining()
            evidence["status"] = "completed" if exit_status == 0 else "failed"
            write_json(path, evidence)
    return exit_status


def main():
    path = Path(sys.argv[2]).resolve()
    spec = json.loads(Path(sys.argv[1]).read_text())
    evidence = {"clock": "CLOCK_MONOTONIC，单机跨进程共享", "timestamp_scope":
        "子进程在 execvpe 紧前写入；退出为 waitpid 观测，轮询误差通常不超过 10ms，调度延迟可能更大"}
    interrupts = Interrupts()
    interrupts.install()
    try:
        return launch(spec, path, interrupts, evidence)
    except BaseException as exc:
        evidence.update(error="用户中断/收尾信号" if isinstance(exc, KeyboardInterrupt) else str(exc), status="failed")
        write_json(path, evidence)
        return 130 if isinstance(exc, KeyboardInterrupt) else 1


if __name__ == "__main__":
    raise SystemExit(main())
