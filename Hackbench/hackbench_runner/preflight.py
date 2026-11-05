"""Read-only capacity checks. Never raise limits or modify cgroup settings."""
import os
from pathlib import Path
import resource

from compilebench.cli import ConfigError
from compilebench.system import read


def user_tasks():
    count = 0
    for p in Path("/proc").glob("[0-9]*"):
        try:
            values = dict(line.split(":", 1) for line in (p / "status").read_text().splitlines() if ":" in line)
            if int(values["Uid"].split()[0]) == os.getuid():
                count += int(values["Threads"])
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError:
            try:
                if p.stat().st_uid == os.getuid():
                    return None
            except FileNotFoundError:
                continue
        except (OSError, ValueError, KeyError):
            return None
    return count


def check_limits(cfg):
    w = cfg["workload"]
    workers = 2 * w["groups"] * w["fds"]
    # Four ready/wake descriptors plus stdio and a spare descriptor. In process
    # mode the parent closes group descriptors; threads share the entire table.
    needed_fds = 2 * w["fds"] * (w["groups"] if w["mode"] == "thread" else 1) + 8
    needed_tasks = workers + 8  # workload leader, sampler and short-lived framework helpers
    soft_fd, hard_fd = resource.getrlimit(resource.RLIMIT_NOFILE)
    soft_proc, hard_proc = resource.getrlimit(resource.RLIMIT_NPROC)
    used = user_tasks()
    result = {"worker_count": workers, "task_reserve": 8, "required_tasks_with_reserve": needed_tasks,
              "fd_conservative_bound_per_process": needed_fds,
              "rlimit_nofile": {"soft": soft_fd, "hard": hard_fd},
              "rlimit_nproc": {"soft": soft_proc, "hard": hard_proc, "visible_user_tasks": used},
              "cgroup_pids": [],
              "limitations": "瞬时只读检查不保证未来资源充足；/proc 在命名空间中可能只显示部分用户任务。cgroup 检查覆盖可见祖先；不更改限额，实际创建/通信失败仍判无效"}
    if soft_fd != resource.RLIM_INFINITY and needed_fds > soft_fd:
        raise ConfigError(f"文件描述符限额不足：每进程保守需要 {needed_fds}，RLIMIT_NOFILE={soft_fd}")
    if soft_proc != resource.RLIM_INFINITY:
        if used is None:
            raise ConfigError("无法核对当前用户线程数，不能确认 RLIMIT_NPROC 余量")
        if used + needed_tasks > soft_proc:
            raise ConfigError(f"进程/线程限额不足：当前 {used}，另需约 {needed_tasks}，RLIMIT_NPROC={soft_proc}")
    own = read("/proc/self/cgroup")
    if own is None or not any(line.startswith("0::") for line in own.splitlines()):
        raise ConfigError("无法核对当前 cgroup v2 pids 限额")
    relative = next(line[3:] for line in own.splitlines() if line.startswith("0::"))
    cgroot = Path("/sys/fs/cgroup")
    starts = [cgroot / relative.lstrip("/")]
    if cfg.get("cgroup_parent"):
        starts.append(Path(cfg["cgroup_parent"]).resolve())
    seen = set()
    for start in starts:
        if not start.is_relative_to(cgroot):
            raise ConfigError("cgroup_parent 必须位于 /sys/fs/cgroup")
        for path in [start, *start.parents]:
            if not path.is_relative_to(cgroot) or path in seen:
                continue
            seen.add(path)
            limit, current = read(path / "pids.max"), read(path / "pids.current")
            if limit is None:
                continue  # This ancestor has no pids controller/limit file.
            entry = {"path": str(path), "max": limit, "current": current}
            result["cgroup_pids"].append(entry)
            if limit != "max":
                if current is None:
                    raise ConfigError(f"无法读取 pids.current: {path}")
                if int(current) + needed_tasks > int(limit):
                    raise ConfigError(f"cgroup pids 余量不足：{path} 当前 {current}/{limit}，另需约 {needed_tasks}")
    return result
