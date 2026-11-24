"""Separate technical failures, request-p99 validity and foreground throughput constraints."""
import csv
import json
import math
from pathlib import Path
import re

from compilebench_runner.control import POLICIES
from compilebench_runner.system import power_error
from schbench_runner.results import FIELDS as FRONT_FIELDS, distribution, number, parse_results as parse_front

FIELDS = [*FRONT_FIELDS[:-1], "back_events_s", "error"]
VALIDITY_REVISION = "request-p99-validity-v1"
TIMER_SOURCES = {
    "front": "schbench 24e32b8: message 线程创建后 sleep_for_runtime 开始计时，结束设 stopping，join 后打印整次分布；average rps=累计请求数/配置 runtime；精确边界未外部观测",
    "back": "sysbench 1.0.20: 全部线程就绪的 barrier 回调启动计时，join workers 后停止；CPU speed 使用原生 events / checkpoint interval；warmup=0，未启用 checkpoint",
}
TIMING_VALIDATION = {
    "revision": "bounded_approximate_v2",
    "front_runtime_poll_s": 1.0,
    "front_source": "固定 schbench 24e32b8 的 sleep_for_runtime：每轮检查时长，未结束时 sleep(1)",
    "front_duration_reference": "配置 runtime，不是原生实测统计时长",
    "rule": "前台进程超额时间预算=固定版本一次轮询周期+配置的非轮询额外预算；后台仍核对原生实测时长",
    "limitation": "原生统计绝对边界未观测；这是有界近似同步核验，不证明精确共同统计窗口",
}


def parse_back(path):
    text = Path(path).read_text(errors="replace")
    if re.search(r"(?im)^.*(?:FATAL|\bERROR\b|failed|forced shutdown|unfinished transactions).*$", text):
        raise ValueError("sysbench 日志含失败/强制终止信息")
    fields = {}
    for name, pattern in (("events_s", r"^\s*events per second:\s*(\S+)\s*$"),
                          ("native_duration_s", r"^\s*total time:\s*([\d.eE+-]+)s\s*$"),
                          ("total_events", r"^\s*total number of events:\s*(\d+)\s*$")):
        matches = re.findall(pattern, text, re.M | re.I)
        if len(matches) != 1:
            raise ValueError(f"sysbench 最终 {name} 必须恰好一个；不平均周期 eps")
        fields[name] = int(matches[0]) if name == "total_events" else number(float(matches[0]), name)
    if fields["native_duration_s"] <= 0:
        raise ValueError("sysbench 原生测量时长必须为正数")
    return fields


def validity_rule(cfg):
    limit = cfg.get("latency_limit_us")
    return {"revision": VALIDITY_REVISION, "metric": "request_p99_us", "limit_us": limit,
            "applicable": cfg["case"] != "back" and limit is not None,
            "rule": "原有执行/核验通过，且 request_p99_us <= latency_limit_us（默认 30000 µs）"}


def latency_validity(row, cfg):
    info = validity_rule(cfg)
    value = row.get("request_p99_us")
    valid_value = type(value) in (int, float) and math.isfinite(value) and value >= 0
    info["actual_us"] = value if valid_value else None
    if not info["applicable"]:
        return {**info, "met": None, "reason": "back 无前台；或 front 预实验未配置延迟上限"}
    if not valid_value:
        return {**info, "met": False, "reason": "请求 p99 缺失或非法，无法判定延迟是否达标"}
    met = value <= info["limit_us"]
    return {**info, "met": met, "reason": None if met else
            f"请求 p99 超限: {value:g} µs ({value/1000:g} ms) > {info['limit_us']:g} µs ({info['limit_us']/1000:g} ms)"}


def finalize_validity(row, details, cfg):
    """Do not mistake an operational failure for a pure latency failure, or vice versa."""
    technical_valid = bool(row["valid"])
    technical_error = row.get("error", "")
    latency = latency_validity(row, cfg)
    reasons = []
    if not technical_valid:
        reasons.append("technical_error")
    if latency["met"] is False:
        reasons.append("p99_exceeded" if latency["actual_us"] is not None else "p99_unavailable")
    details.update(technical_valid=technical_valid, technical_error=technical_error,
                   latency_validity=latency, invalid_reasons=reasons)
    messages = []
    if not technical_valid:
        messages.append("测试执行/核验错误: " + (technical_error or "未通过原有技术检查"))
    if latency["met"] is False:
        messages.append(latency["reason"])
    row["valid"] = technical_valid and latency["met"] is not False
    row["error"] = "; ".join(messages)


def assess(row, cfg):
    if cfg["case"] == "back":
        return {"met": None, "evaluable": False, "reason": "back case 无前台约束", "checks": {}}
    complete = all(row.get(key) is not None for key in ("avg_rps", "request_p99_us", "wakeup_p99_us"))
    checks = {}
    if complete:
        if cfg["rps"] is not None:
            fraction = row["avg_rps"] / cfg["rps"]
            checks["throughput"] = {"target_rps": cfg["rps"], "tolerance": cfg["rps_tolerance"],
                "actual_fraction": fraction, "met": abs(fraction - 1) <= cfg["rps_tolerance"] + 1e-12}
        if cfg["latency_limit_us"] is not None:
            checks["request_p99"] = {"limit_us": cfg["latency_limit_us"], "met": row["request_p99_us"] <= cfg["latency_limit_us"]}
    evaluable = complete and cfg["rps"] is not None and cfg["latency_limit_us"] is not None
    return {"checks": checks, "evaluable": evaluable,
        "met": all(check["met"] for check in checks.values()) if evaluable else None,
        "reason": None if evaluable else "前台预实验：未配置 RPS/延迟上限或缺少前台指标"}


def synchronization(evidence, cfg, back=None):
    children = evidence.get("children", {})
    expected = ("front", "back") if cfg["case"] == "mixed" else (cfg["case"],)
    errors, durations = [], {}
    for side in expected:
        child = children.get(side, {})
        start, end = child.get("exec_monotonic_s"), child.get("exit_monotonic_s")
        if child.get("exit_code") != 0 or not child.get("reaped") or child.get("terminated_by_coordinator"):
            errors.append(f"{side} 未正常自然退出")
        if any(type(t) not in (int, float) or not math.isfinite(t) for t in (start, end)) or end <= start:
            errors.append(f"{side} 缺少有效 exec/退出边界")
            continue
        durations[side] = end - start
    info = {"process_durations_s": durations, "native_timing": TIMER_SOURCES,
        "validation": dict(TIMING_VALIDATION), "warnings": [],
        "interpretation": "近似同步稳态区间；进程重叠不等于原生统计窗口一致，原生绝对起止未直接观测",
        "native_absolute_boundaries_observed": False, "tolerances": cfg["synchronization"]}
    if len(durations) != len(expected):
        return info, errors
    if cfg["case"] == "mixed":
        a, b = children["front"], children["back"]
        skew = abs(a["exec_monotonic_s"] - b["exec_monotonic_s"])
        overlap = max(0, min(a["exit_monotonic_s"], b["exit_monotonic_s"]) - max(a["exec_monotonic_s"], b["exec_monotonic_s"]))
        ratio = overlap / min(durations.values())
        info.update(start_skew_s=skew, process_overlap_s=overlap, process_overlap_ratio=ratio)
        if skew > cfg["synchronization"]["max_start_skew_s"]:
            errors.append("前后台启动错位超过容差")
        if ratio < cfg["synchronization"]["min_overlap_ratio"]:
            errors.append("前后台进程重叠比例不足")
    info["native_duration_checks"] = {}
    for side in expected:
        reference = cfg["runtime_s"] if side == "front" else (back or {}).get("native_duration_s")
        if reference is None:
            errors.append(f"{side} 原生时长不可核对")
            continue
        excess = durations[side] - reference
        extra_budget = cfg["synchronization"]["max_native_overhead_s"]
        poll_budget = TIMING_VALIDATION["front_runtime_poll_s"] if side == "front" else 0
        check = {"duration_reference": "configured_runtime" if side == "front" else "native_elapsed",
            "configured_duration_s" if side == "front" else "native_duration_s": reference,
            "process_excess_s": excess, "runtime_polling_allowance_s": poll_budget,
            "additional_allowance_s": extra_budget, "max_process_excess_s": poll_budget + extra_budget,
            "absolute_start_s": None, "absolute_end_s": None}
        info["native_duration_checks"][side] = check
        if excess < -0.01 or excess > poll_budget + extra_budget:
            if side == "front":
                errors.append(f"front 进程超出固定版本计时预算：相对配置时长额外 {excess:.6f}s，"
                    f"预算 {poll_budget + extra_budget:.6f}s（轮询 {poll_budget}s + 非轮询额外预算 {extra_budget}s）；"
                    "统计边界未观测，不能确认近似同步")
            else:
                errors.append("back 进程时间与原生统计时长不符，不能确认持续共置")
        elif side == "front" and excess > extra_budget:
            info["warnings"].append(f"front 额外 {excess:.6f}s，处于固定版本轮询预算内；"
                "未将配置时长当成实测统计时长，也未证明额外时间全部是退出收尾")
        if side == "back" and abs(reference - cfg["runtime_s"]) > extra_budget:
            errors.append("back 原生测量时长偏离固定时长")
    return info, errors


def summarize(rows, assessments, case):
    primary = "request_p99_us" if case == "front" else "back_events_s"
    groups = {}
    for policy in dict.fromkeys([*POLICIES, *(r["policy"] for r in rows)]):
        indexed = [(i, row) for i, row in enumerate(rows) if row["policy"] == policy]
        good = [(i, row) for i, row in indexed if row["valid"]]
        eligible = [(i, row) for i, row in good if case == "back" or assessments[i]["met"] is True]
        pairs = []
        if case == "mixed":
            for i, row in eligible:
                for j, base in enumerate(rows):
                    if (policy != "default" and base["policy"] == "default" and base["run"] == row["run"]
                        and base["valid"] and assessments[j]["met"] is True and base[primary] > 0):
                        pairs.append({"run": row["run"], "improvement_pct": 100 * (row[primary] - base[primary]) / base[primary]})
        groups[policy] = {"attempted": len(indexed), "valid": len(good),
            "constraints_met": sum(assessments[i]["met"] is True for i, _ in good),
            "constraints_unmet": sum(assessments[i]["met"] is False for i, _ in good),
            "constraints_unknown": sum(assessments[i]["met"] is None for i, _ in good),
            "metrics_valid": {field: distribution([row[field] for _, row in good if row.get(field) is not None])
                for field in ("avg_rps", "request_p99_us", "wakeup_p99_us", "back_events_s")},
            "primary_eligible": distribution([row[primary] for _, row in eligible if row.get(primary) is not None]),
            "paired": pairs, "paired_improvement_pct": distribution([p["improvement_pct"] for p in pairs])}
    return {"case": case, "primary_metric": primary, "policies": groups,
        "interpretation": "mixed 先满足预设前台吞吐和请求延迟约束再比较后台吞吐；front 可用于预实验。仅同配置同轮有效且达标时配对；采样点不是重复。"}


def baseline_ratios(cfg, snapshot, row):
    result = {}
    if cfg["case"] != "mixed":
        return result
    for side, metric, name in (("front", "request_p99_us", "front_latency_amplification"),
                                ("back", "back_events_s", "back_throughput_retention")):
        path = cfg.get("baselines", {}).get(side)
        result[name] = {"ratio": None, "reason": "未指定同配置单独运行基线"}
        if not path or not row["valid"]:
            continue
        try:
            root = Path(path)
            data = json.loads((root / "metadata.json").read_text())
            base_cfg = data["config"]
            old = json.loads((root / "snapshot.json").read_text())
            same = (data.get("workload") == "mixbench" and data.get("status") == "completed" and base_cfg["case"] == side
                and all(base_cfg[key] == cfg[key] for key in ("cpus", "runtime_s", side))
                and (side == "back" or base_cfg["rps"] == cfg["rps"])
                and old["program_sha256"][side] == snapshot["program_sha256"][side]
                and old["machine"]["uname"][2] == snapshot["machine"]["uname"][2]
                and not power_error(old["machine"]["power"], snapshot["machine"]["power"], "基线"))
            if not same:
                raise ValueError("基线 case、配置、程序摘要或环境不匹配")
            with (root / "summary.csv").open() as stream:
                values = [float(r[metric]) for r in csv.DictReader(stream) if r["policy"] == row["policy"] and r["valid"] == "True"]
            base = distribution(values)["median"]
            if base is None or base <= 0 or not math.isfinite(base):
                raise ValueError("基线没有同策略有效且分母非零的结果")
            result[name] = {"ratio": row[metric] / base, "baseline_median": base, "batch": path,
                "policy": row["policy"], "count": len(values), "reason": None}
        except (OSError, ValueError, KeyError, TypeError) as exc:
            result[name]["reason"] = str(exc)
    return result
