"""Bounded wakeup tracing and explicitly versioned running-phase percentiles."""
import csv
import hashlib
import json
import math
from pathlib import Path
import shutil
import subprocess

from compilebench_runner.system import command_output, digest, write_json

COMMIT = "24e32b8ef67cbafb0761aed68747d90a291767ae"
SOURCE_SHA256 = "18720ce0a70b115f70fcae309ef9a70c98b005f6a7b0cdcf7a4fbf298b14795b"
REVISION = "bounded-wakeup-events-v1"
CAPACITY = 4096
FILTER_REVISION = "running-wakeup-v1"
TRACE_FIELDS = ("worker_id", "sequence", "resume_realtime_us", "origin_realtime_us", "latency_us", "stopping", "last_post_kind")


def metric_policy(observed):
    return {"scope": "running_only" if observed else "native_all",
            "revision": FILTER_REVISION if observed else "native",
            "rule": "stopping == 0" if observed else "原生全部样本",
            "percentiles": "nearest-rank raw microseconds" if observed else "原生直方图近似百分位"}


def apply_running_wakeup(row, details, summary):
    """Never replace an unavailable filtered percentile with an unfiltered one."""
    native = row.get("wakeup_p99_us")
    filtering = {**metric_policy(True), "native_wakeup_p99_us": native,
                 "retained_samples": summary.get("groups", {}).get("running", {}).get("count"),
                 "excluded_samples": summary.get("groups", {}).get("stopping", {}).get("count"),
                 "native_source": details.get("front_metric_sources", {}).get("wakeup_p99_us")}
    details["wakeup_filter"] = filtering
    row["wakeup_p99_us"] = None
    group = summary.get("groups", {}).get("running", {})
    if summary.get("status") != "complete" or summary.get("capture_complete") is not True:
        filtering.update(status="unavailable", reason="逐条唤醒观测缺失或不完整，不能计算运行阶段 p99")
        return filtering["reason"]
    if not group.get("count") or group.get("p99_us") is None:
        filtering.update(status="unavailable", reason="没有 stopping=0 的唤醒样本，不能计算运行阶段 p99")
        return filtering["reason"]
    row["wakeup_p99_us"] = group["p99_us"]
    filtering.update(status="applied", running_wakeup_p99_us=group["p99_us"], source="front-wakeup-running.csv")
    details.setdefault("front_metric_sources", {})["wakeup_p99_us"] = "完整 front-wakeup.csv 中 stopping=0 样本的 nearest-rank p99；µs"
    return None


def instrument_source(source):
    def replace(old, new):
        nonlocal source
        if source.count(old) != 1:
            raise ValueError("schbench 观测插入点不唯一或缺失；拒绝猜测源码布局")
        source = source.replace(old, new)

    replace("\tunsigned long *data;\n};", "\tunsigned long *data;\n\tstruct wisp_trace_buffer *wisp_trace;\n};\n\n#include \"wakeup_observer.h\"")
    replace("\t\tfpost(&list->futex);", "\t\twisp_note_post(list, WISP_POST_REQUEST);\n\t\tfpost(&list->futex);")
    replace("\tif (delta > 0)\n\t\tadd_lat(&td->wakeup_stats, delta);",
            "\tif (delta > 0) {\n\t\tadd_lat(&td->wakeup_stats, delta);\n"
            "\t\twisp_record_wakeup(td, (unsigned int)delta, &now, stopping);\n\t}")
    replace("\t\t\tmemcpy(&worker->wake_time, &now, sizeof(now));\n\t\t\tfpost(&worker->futex);",
            "\t\t\tmemcpy(&worker->wake_time, &now, sizeof(now));\n"
            "\t\t\twisp_note_post(worker, WISP_POST_REQUEST);\n\t\t\tfpost(&worker->futex);")
    replace("\t\t\tfor (i = 0; i < worker_threads; i++)\n\t\t\t\tfpost(&worker_threads_mem[i].futex);",
            "\t\t\tfor (i = 0; i < worker_threads; i++) {\n"
            "\t\t\t\twisp_note_post(&worker_threads_mem[i], WISP_POST_SHUTDOWN);\n"
            "\t\t\t\tfpost(&worker_threads_mem[i].futex);\n\t\t\t}")
    replace("\t\tworker_threads_mem[i].msg_thread = td;",
            "\t\tif (wisp_buffers)\n\t\t\tworker_threads_mem[i].wisp_trace =\n"
            "\t\t\t\t&wisp_buffers[td->index * worker_threads + i];\n\t\tworker_threads_mem[i].msg_thread = td;")
    replace("\tfor (i = 0; i < worker_threads; i++) {\n\t\tfpost(&worker_threads_mem[i].futex);\n\t\tpthread_join",
            "\tfor (i = 0; i < worker_threads; i++) {\n"
            "\t\twisp_note_post(&worker_threads_mem[i], WISP_POST_SHUTDOWN);\n"
            "\t\tfpost(&worker_threads_mem[i].futex);\n\t\tpthread_join")
    replace("\tmessage_threads_mem = calloc(message_threads * worker_threads + message_threads,",
            "\twisp_trace_init();\n\tmessage_threads_mem = calloc(message_threads * worker_threads + message_threads,")
    replace("\tfree(message_threads_mem);", "\twisp_trace_dump(wakeup_stats.nr_samples);\n\tfree(message_threads_mem);")
    return source


def ensure_observer(project):
    """Build a separate pinned-source variant before cooldown/measurement, without sudo."""
    project = Path(project).resolve()
    source = project.parent / "Schbench/bench/src" / ("schbench-" + COMMIT) / "schbench.c"
    header = project / "native/wakeup_observer.h"
    if not source.is_file():
        raise ValueError("缺少固定 schbench 源码；先在 Schbench 执行 ./build-schbench")
    if digest(source) != SOURCE_SHA256:
        raise ValueError("schbench.c 与固定提交摘要不符，拒绝构建观测版本")
    patched = instrument_source(source.read_text())
    identity = {"revision": REVISION, "upstream_source_sha256": SOURCE_SHA256,
                "header_sha256": digest(header), "instrumented_source_sha256": hashlib.sha256(patched.encode()).hexdigest()}
    build = project / "bench/build/wakeup-observer"
    binary, provenance = build / "schbench-observe", build / "provenance.json"
    if binary.is_file() and provenance.is_file():
        data = json.loads(provenance.read_text())
        if data.get("observation") == identity and data.get("binary_sha256") == digest(binary):
            return str(binary), str(provenance), identity, {str(header): digest(header)}
    compiler = shutil.which("gcc")
    if compiler is None:
        raise ValueError("构建唤醒观测版本需要 gcc")
    build.mkdir(parents=True, exist_ok=True)
    src = build / "schbench-observe.c"
    src.write_text(patched)
    temporary = build / "schbench-observe.new"
    command = [str(Path(compiler).resolve()), "-Wall", "-O2", "-g", "-W", "-D_LARGEFILE_SOURCE",
               "-D_FILE_OFFSET_BITS=64", "-I", str(header.parent), str(src), "-o", str(temporary), "-lpthread", "-lm"]
    with (build / "prepare.log").open("wb") as log:
        subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True)
    temporary.replace(binary)
    write_json(provenance, {"repository": "https://github.com/masoncl/schbench", "commit": COMMIT,
        "source_sha256": SOURCE_SHA256, "binary_sha256": digest(binary), "observation": identity,
        "command": command, "compiler": command_output([compiler, "--version"])})
    return str(binary), str(provenance), identity, {str(header): digest(header)}


def trace_environment(attempt, cfg):
    if not cfg.get("wakeup_observation", {}).get("enabled"):
        return {}
    return {"WISP_WAKEUP_TRACE_DIR": str(Path(attempt).resolve()), "WISP_WAKEUP_CAPACITY": str(CAPACITY)}


def distribution(values):
    values = sorted(values)
    return {"count": len(values), "p50_us": values[math.ceil(len(values) * .5) - 1] if values else None,
            "p99_us": values[math.ceil(len(values) * .99) - 1] if values else None,
            "max_us": values[-1] if values else None,
            "over_100ms": sum(value > 100000 for value in values)}


def summarize_trace(attempt):
    """Run outside measurement; retain raw events and export a complete running-only view."""
    attempt = Path(attempt)
    summary = {"schema_version": 1, "revision": REVISION,
        "percentiles": "nearest-rank raw microseconds; native histogram values are approximate and remain unchanged",
        "classification": "stopping is sampled at recording; last_post_kind is the last instrumented posting site, not a kernel wakeup cause",
        "clock": "existing gettimeofday; origin is reconstructed from native latency and resume time, not a kernel wake timestamp"}
    try:
        meta = json.loads((attempt / "front-wakeup.meta.json").read_text())
        if meta.get("schema_version") != 1:
            raise ValueError("未知 wakeup 观测格式")
        rows = []
        with (attempt / "front-wakeup.csv").open() as stream:
            for row in csv.DictReader(stream):
                values = {k: int(row[k]) for k in ("worker_id", "sequence", "resume_realtime_us", "origin_realtime_us", "latency_us", "stopping")}
                if values["stopping"] not in (0, 1) or min(values.values()) < 0 or row["last_post_kind"] not in {"request", "shutdown", "unknown"}:
                    raise ValueError("非法 wakeup 观测记录")
                if values["resume_realtime_us"] - values["origin_realtime_us"] != values["latency_us"]:
                    raise ValueError("wakeup 观测时间差不匹配")
                rows.append({**values, "last_post_kind": row["last_post_kind"]})
        complete = meta.get("capture_complete") is True and len(rows) == meta.get("recorded_samples") == meta.get("native_wakeup_samples") and meta.get("dropped_samples") == 0
        summary.update(status="complete" if complete else "incomplete", capture_complete=complete, capture=meta,
            groups={"all": distribution([r["latency_us"] for r in rows]),
                    "running": distribution([r["latency_us"] for r in rows if not r["stopping"]]),
                    "stopping": distribution([r["latency_us"] for r in rows if r["stopping"]])},
            by_last_post={kind: distribution([r["latency_us"] for r in rows if r["last_post_kind"] == kind])
                          for kind in ("request", "shutdown", "unknown")},
            slowest=sorted(rows, key=lambda r: r["latency_us"], reverse=True)[:20])
        summary["selection"] = {**metric_policy(True), "retained_samples": summary["groups"]["running"]["count"],
            "excluded_samples": summary["groups"]["stopping"]["count"],
            "p99_us": summary["groups"]["running"]["p99_us"] if complete else None,
            "status": "complete" if complete else "unavailable"}
        if complete:
            temporary = attempt / "front-wakeup-running.csv.tmp"
            with temporary.open("w", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=TRACE_FIELDS)
                writer.writeheader()
                writer.writerows(r for r in rows if r["stopping"] == 0)
            temporary.replace(attempt / "front-wakeup-running.csv")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        summary.update(status="unavailable", capture_complete=False, error=str(exc))
    write_json(attempt / "front-wakeup-summary.json", summary)
    return summary
