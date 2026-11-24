"""Observe the user's current scheduler without controlling its lifecycle."""
from pathlib import Path
import re

from .system import read


class PolicyError(RuntimeError):
    pass


def policy_name(state):
    if state["state"] == "disabled":
        return "default"
    ops = state["ops"]
    for name in ("lavd", "bpfland", "rusty"):
        if ops == name or ops.startswith(name + "_"):
            return "scx_" + name
    return ops  # Unknown schedulers retain the exact kernel-provided name.


class Scheduler:
    def __init__(self, root="/sys/kernel/sched_ext"):
        self.root = Path(root)

    def snapshot(self):
        return {"state": read(self.root / "state"), "ops": read(self.root / "root/ops"),
                "enable_seq": read(self.root / "enable_seq"),
                "switch_all": read(self.root / "switch_all")}

    def initial(self):
        state = self.snapshot()
        if state["state"] not in {"enabled", "disabled"} or state["enable_seq"] is None:
            raise PolicyError("sched_ext 状态不可读或正在切换，请先完成调度器切换再测试")
        if state["state"] == "enabled" and (not state["ops"] or state["switch_all"] != "1"):
            raise PolicyError("当前 scx 的 root/ops 不可读或 switch_all 不为 1；不支持 partial 模式")
        return state

    def verify(self, expected, pid=None):
        actual = self.snapshot()
        for field in ("state", "ops", "enable_seq", "switch_all"):
            if actual[field] != expected[field]:
                raise PolicyError(f"策略变化: {field}={actual[field]!r}, 预期 {expected[field]!r}")
        if pid is not None:
            text = read(f"/proc/{pid}/sched") or ""
            match = re.search(r"^ext.enabled\s*:\s*(\d+)", text, re.M)
            if match is None:
                raise PolicyError("无法读取编译任务 ext.enabled；需要 CONFIG_SCHED_DEBUG")
            want = int(expected["state"] == "enabled")
            if int(match[1]) != want:
                raise PolicyError(f"编译任务 ext.enabled={match[1]}，预期 {want}")
