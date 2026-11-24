"""Parse fixed-work IPC completion time; retain evidence of native failures."""
from pathlib import Path
import math
import re

from compilebench_runner.cli import SUMMARY_FIELDS
from compilebench_runner.control import POLICIES
from schbench_runner.results import distribution

FIELDS = [*SUMMARY_FIELDS[:-1], "hackbench_s", "error"]


def parse_results(log, workload):
    text = Path(log).read_text(errors="replace")
    # rt-tests 2.11 can print Time and return zero after receiving a signal.
    # Its parent also does not propagate every worker exit code: stderr matters.
    errors = re.findall(r"(?im)^.*(?:\(error:|\bfailed\b|\bfailure\b|\bfatal\b|\bshort (?:read|write)\b|"
                        r"\bno measurements available\b|\bsignal \d+ caught\b|longjmp|"
                        r"children started\.\s*Expected|sending SIGTERM|signaling \d+ worker).*$", text)
    if errors:
        raise ValueError("原生 worker/通信失败: " + "; ".join(dict.fromkeys(errors)))
    times = re.findall(r"(?im)^\s*Time:\s*(\S+)\s*$", text)
    if len(times) != 1:
        raise ValueError("预期恰好一个原生 Time:；缺失或多次输出均不作为成功")
    try:
        value = float(times[0])
    except ValueError as exc:
        raise ValueError("原生 Time 不是秒数") from exc
    if not math.isfinite(value) or value < 0:
        raise ValueError("原生 Time 必须为有限非负秒数")
    headers = re.findall(r"Running in (process|threaded) mode with (\d+) groups using (\d+) file descriptors each \(== (\d+) tasks\)", text)
    payload = re.findall(r"Each sender will pass (\d+) messages of (\d+) bytes", text)
    expected = ("process" if workload["mode"] == "process" else "threaded",
                str(workload["groups"]), str(2 * workload["fds"]), str(2 * workload["groups"] * workload["fds"]))
    if headers != [expected] or payload != [(str(workload["loops"]), str(workload["datasize"]))]:
        raise ValueError("原生负载描述与固定参数不一致或缺失")
    return {"hackbench_s": value}, {
        "source": "hackbench.log 唯一 Time:，秒；保留原生毫秒显示精度，0.000 不伪造为更大值",
        "workload_echo_verified": True,
        "timer_scope_rt_tests_2_11": "workers 创建并全部就绪后、发出 GO 前 gettimeofday，至等待 workers 结束后 gettimeofday；不等于外层 wall_s"}


def summarize(rows):
    groups = {}
    for policy in dict.fromkeys([*POLICIES, *(r["policy"] for r in rows)]):
        attempts = [r for r in rows if r["policy"] == policy]
        good = [r for r in attempts if r["valid"]]
        pairs = []
        for row in good:
            for base in rows:
                if (policy != "default" and base["policy"] == "default" and base["run"] == row["run"]
                        and base["valid"] and base["hackbench_s"] > 0):
                    pairs.append({"run": row["run"], "improvement_pct":
                        (base["hackbench_s"] - row["hackbench_s"]) / base["hackbench_s"] * 100})
        groups[policy] = {"attempted": len(attempts), "valid": len(good),
                          "hackbench_s": distribution([r["hackbench_s"] for r in good]),
                          "paired": pairs,
                          "paired_improvement_pct": distribution([r["improvement_pct"] for r in pairs])}
    return {"primary_metric": "hackbench_s", "lower_is_better": True, "policies": groups,
            "interpretation": "只比较相同负载配置；同轮有效结果配对，正值更快。Time=0 时不计算改善率。采样点不是重复实验，3 轮仅作摸底。"}
