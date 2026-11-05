"""Read final whole-run schbench statistics and compare independent attempts."""
import json
import math
from pathlib import Path
import re
import statistics

FIELDS = ["policy", "run", "valid", "exit_code", "wall_s", "cpu_time_s",
          "peak_memory_bytes", "temp_start_c", "temp_peak_sampled_c",
          "avg_rps", "request_p99_us", "wakeup_p99_us", "error"]


def number(value, name):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} 必须为有限非负数")
    return value


def parse_results(log_path, native_path=None, runtime=None):
    text = Path(log_path).read_text(errors="replace")
    averages = list(re.finditer(r"(?im)^\s*average rps:\s*([0-9.eE+-]+)\s*$", text))
    if not averages:
        raise ValueError("无法解析最终 average rps；不会用 RPS p50 替代")
    final = averages[-1]
    values = {"avg_rps": number(float(final[1]), "average rps")}
    evidence = {"avg_rps": "schbench.log 最后一个 average rps（原生整次请求数 / 配置 runtime）"}
    before_average = text[:final.start()]
    for label, field in (("Request", "request_p99_us"), ("Wakeup", "wakeup_p99_us")):
        headers = list(re.finditer(
            rf"(?m)^{label} Latencies percentiles \(usec\) runtime (\d+) \(s\) \((\d+) total samples\)",
            before_average))
        if not headers:
            raise ValueError(f"缺少最终 {label} 延迟统计")
        header = headers[-1]
        if int(header[2]) <= 0 or (runtime is not None and int(header[1]) != runtime):
            raise ValueError(f"{label} 最终统计窗口或样本数不符合配置")
        block = before_average[header.end():]
        block = re.split(r"(?m)^\S.*percentiles", block, maxsplit=1)[0]
        match = re.search(r"(?m)^\s*\*?\s*99(?:\.0)?th:\s*([0-9.eE+-]+)", block)
        if match is None:
            raise ValueError(f"缺少最终 {label} p99；不平均窗口 p99")
        values[field] = number(float(match[1]), field)
        evidence[field] = f"schbench.log 最终 {label} 99.0th，µs，整次累计分布"
    if native_path is not None:
        obj = json.loads(Path(native_path).read_text())
        normal = obj.get("normal") if isinstance(obj, dict) else None
        if not isinstance(normal, dict):
            raise ValueError("不支持的 native.json 格式；预期 normal 对象")
        # Upstream uses typed groups: string metadata in normal, statistics in int.
        stats = obj.get("int", normal)
        if not isinstance(stats, dict):
            raise ValueError("不支持的 native.json 数值统计对象")
        for field, key in (("request_p99_us", "request_latency_pct99.0"),
                           ("wakeup_p99_us", "wakeup_latency_pct99.0")):
            value = number(stats.get(key), key)
            if value != values[field]:
                raise ValueError(f"native.json 与最终日志 {field} 不一致")
        if runtime is not None and stats.get("runtime") != runtime:
            raise ValueError("native.json runtime 与配置不一致")
        evidence["native_json"] = "保留全部原生百分位；version 字段是运行内核版本，不是 schbench 版本"
    return values, evidence


def assess(row, cfg):
    checks = {}
    if row["avg_rps"] is not None and cfg["mode"] == "fixed_rps":
        target = cfg["rps"]
        fraction = row["avg_rps"] / target
        checks["throughput"] = {"target_rps": target, "tolerance": cfg["rps_tolerance"],
                                "actual_fraction": fraction,
                                "met": abs(fraction - 1) <= cfg["rps_tolerance"] + 1e-12}
    for field, name in (("request_p99_us", "request_p99_limit_us"),
                        ("wakeup_p99_us", "wakeup_p99_limit_us")):
        if cfg[name] is not None and row[field] is not None:
            checks[field] = {"limit_us": cfg[name], "met": row[field] <= cfg[name]}
    known = all(row.get(key) is not None for key in ("avg_rps", "request_p99_us", "wakeup_p99_us"))
    return {"checks": checks, "evaluable": known,
            "met": known and all(check["met"] for check in checks.values())}


def distribution(values):
    return {"count": len(values), "median": statistics.median(values) if values else None,
            "min": min(values) if values else None, "max": max(values) if values else None}


def summarize(rows, assessments, mode):
    from compilebench_runner.control import POLICIES
    primary = "request_p99_us" if mode == "fixed_rps" else "avg_rps"
    policies = list(dict.fromkeys([*POLICIES, *(row["policy"] for row in rows)]))
    groups = {}
    for policy in policies:
        indexed = [(i, row) for i, row in enumerate(rows) if row["policy"] == policy]
        good = [(i, row) for i, row in indexed if row["valid"]]
        eligible = [(i, row) for i, row in good if assessments[i]["met"]]
        pairs = []
        for i, row in eligible:
            for j, base in enumerate(rows):
                if (base["policy"] == "default" and base["run"] == row["run"]
                        and base["valid"] and assessments[j]["met"] and policy != "default"
                        and base[primary] > 0):
                    change = (base[primary] - row[primary]) if mode == "fixed_rps" else (row[primary] - base[primary])
                    pairs.append({"run": row["run"], "improvement_pct": change / base[primary] * 100})
        groups[policy] = {"attempted": len(indexed), "valid": len(good),
                          "constraints_met": len(eligible),
                          "primary_valid": distribution([row[primary] for _, row in good]),
                          "primary_eligible": distribution([row[primary] for _, row in eligible]),
                          "paired": pairs,
                          "paired_improvement_pct": distribution([p["improvement_pct"] for p in pairs])}
    return {"mode": mode, "primary_metric": primary, "policies": groups,
            "interpretation": "先满足约束再比较主要指标；正的配对改善率更好。每次独立运行是一次重复，采样点不是重复；少量轮次允许尚不能区分。"}
