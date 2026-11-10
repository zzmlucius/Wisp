"""Per-build cgroup accounting, optional when user delegation is unavailable."""
import os
from pathlib import Path
import uuid
import subprocess

from .system import read


def is_delegated(path):
    try:
        if os.getxattr(path, "user.delegate") == b"1":
            return True
    except OSError:
        pass
    # Older systemd versions do not publish the xattr. Ask the user manager
    # about this exact unit and require an exact ControlGroup path match.
    if path.suffix not in {".service", ".scope"}:
        return False
    try:
        result = subprocess.run(["systemctl", "--user", "show", "--property=Delegate",
                                 "--property=ControlGroup", "--", path.name],
                                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
        properties = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        return (result.returncode == 0 and properties.get("Delegate") == "yes"
                and properties.get("ControlGroup") == "/" + str(path.relative_to("/sys/fs/cgroup")))
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False


class BuildCgroup:
    def __init__(self, parent=None):
        self.path = None
        self.reason = None
        self.notes = {}
        try:
            if parent is None:
                entry = next(line[3:] for line in Path("/proc/self/cgroup").read_text().splitlines()
                             if line.startswith("0::"))
                parent = Path("/sys/fs/cgroup") / entry.lstrip("/")
                # systemd DelegateSubgroup=manager places the supervisor below an
                # empty delegated root. Only enable controllers inside that root.
                for candidate in (parent, parent.parent):
                    processes = read(candidate / "cgroup.procs")
                    if candidate.stat().st_uid == os.getuid() and processes == "" and is_delegated(candidate):
                        available = (read(candidate / "cgroup.controllers") or "").split()
                        needed = [name for name in ("cpu", "memory") if name in available]
                        if needed:
                            (candidate / "cgroup.subtree_control").write_text(" ".join("+" + name for name in needed))
                        parent = candidate
                        break
            parent = Path(parent).resolve()
            if not parent.is_relative_to(Path("/sys/fs/cgroup")):
                raise OSError("cgroup_parent 必须位于 /sys/fs/cgroup")
            path = parent / ("compiletest-" + uuid.uuid4().hex)
            path.mkdir()
            self.path = path
            for metric, filename in (("cpu_time_s", "cpu.stat"),
                                     ("peak_memory_bytes", "memory.peak")):
                if not (path / filename).exists():
                    self.notes[metric] = f"未启用对应 cgroup controller: {filename}"
        except (OSError, StopIteration) as exc:
            self.reason = f"独立 cgroup 不可用: {exc}"

    def attach(self, pid):
        if self.path:
            (self.path / "cgroup.procs").write_text(str(pid))

    def values(self):
        cpu = memory = peak = None
        if self.path:
            stat = dict(line.split() for line in (read(self.path / "cpu.stat") or "").splitlines())
            if "usage_usec" in stat:
                cpu = int(stat["usage_usec"]) / 1_000_000
            memory = read(self.path / "memory.current")
            peak = read(self.path / "memory.peak")
        return {"cpu_time_s": cpu,
                "memory_current_bytes": int(memory) if memory is not None else None,
                "peak_memory_bytes": int(peak) if peak is not None else None}

    def pids(self):
        if not self.path:
            return []
        return list({int(pid) for file in self.path.rglob("cgroup.procs")
                     for pid in (read(file) or "").split()})

    def close(self):
        if self.path:
            # Only our unique per-attempt subtree may contain workload subgroups.
            for directory in sorted((p for p in self.path.rglob("*") if p.is_dir()),
                                    key=lambda p: len(p.parts), reverse=True):
                directory.rmdir()
            self.path.rmdir()  # Never remove an external cgroup or a nonempty one.

    def kill_remaining(self):
        if self.path and self.pids():
            (self.path / "cgroup.kill").write_text("1")
