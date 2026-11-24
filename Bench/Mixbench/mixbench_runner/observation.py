"""One final wakeup slot per worker; filter inside the native histogram."""
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess

from compilebench_runner.system import command_output, digest, write_json

COMMIT = "24e32b8ef67cbafb0761aed68747d90a291767ae"
SOURCE_SHA256 = "18720ce0a70b115f70fcae309ef9a70c98b005f6a7b0cdcf7a4fbf298b14795b"
REVISION = "last-wakeup-slots-v1"
CAPACITY = 1
FILTER_REVISION = "last-worker-stopping-v1"


def metric_policy(observed):
    return {"scope": "last_worker_stopping_filtered" if observed else "native_all",
            "revision": FILTER_REVISION if observed else "native",
            "rule": "仅排除每个 worker 最后一条且 stopping=1 的唤醒样本" if observed else "原生全部样本",
            "percentiles": "原生直方图近似百分位"}


def instrument_source(source):
    def replace(old, new):
        nonlocal source
        if source.count(old) != 1:
            raise ValueError("schbench 末条样本筛选插入点不唯一或缺失")
        source = source.replace(old, new)
    replace("\tunsigned long *data;\n};", "\tunsigned long *data;\n"
            "\tunsigned int wisp_last_us;\n\tunsigned char wisp_last_stopping;\n"
            "\tunsigned char wisp_last_valid;\n};\n\n#include \"wakeup_observer.h\"")
    replace("\t\tadd_lat(&td->wakeup_stats, delta);",
            "\t\twisp_record_last_wakeup(td, (unsigned int)delta, stopping);")
    replace("\t\tpthread_join(worker_threads_mem[i].tid, NULL);",
            "\t\tpthread_join(worker_threads_mem[i].tid, NULL);\n"
            "\t\twisp_finish_last_wakeup(&worker_threads_mem[i]);")
    replace("\t\t\tmemset(&worker->wakeup_stats, 0, sizeof(worker->wakeup_stats));",
            "\t\t\tmemset(&worker->wakeup_stats, 0, sizeof(worker->wakeup_stats));\n"
            "\t\t\tworker->wisp_last_valid = 0;")
    replace("\tfree(message_threads_mem);",
            "\twisp_dump_last_wakeups(message_threads_mem);\n\tfree(message_threads_mem);")
    return source


def ensure_observer(project):
    """Build a separate pinned-source variant; preserve Schbench's binary."""
    project = Path(project).resolve()
    source = project.parent / "Schbench/bench/src" / ("schbench-" + COMMIT) / "schbench.c"
    header = project / "native/wakeup_observer.h"
    if not source.is_file() or digest(source) != SOURCE_SHA256:
        raise ValueError("缺少固定 schbench 源码或摘要不符；先在 Schbench 执行 ./build-schbench")
    patched = instrument_source(source.read_text())
    identity = {"revision": REVISION, "upstream_source_sha256": SOURCE_SHA256,
                "header_sha256": digest(header), "instrumented_source_sha256": hashlib.sha256(patched.encode()).hexdigest()}
    build = project / "bench/build/wakeup-last"
    binary, provenance = build / "schbench-last", build / "provenance.json"
    if binary.is_file() and provenance.is_file():
        data = json.loads(provenance.read_text())
        if data.get("observation") == identity and data.get("binary_sha256") == digest(binary):
            return str(binary), str(provenance), identity, {str(header): digest(header)}
    compiler = shutil.which("gcc")
    if compiler is None:
        raise ValueError("构建末条唤醒筛选版本需要 gcc")
    build.mkdir(parents=True, exist_ok=True)
    src = build / "schbench-last.c"
    src.write_text(patched)
    temporary = build / "schbench-last.new"
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
    return {"WISP_WAKEUP_TAIL_DIR": str(Path(attempt).resolve())} if cfg.get("wakeup_observation", {}).get("enabled") else {}


def summarize_trace(attempt):
    """Validate only final slots, not a full event trace."""
    attempt = Path(attempt)
    data = json.loads((attempt / "front-wakeup.meta.json").read_text())
    if data.get("schema_version") != 2 or data.get("revision") != FILTER_REVISION:
        raise ValueError("未知末条唤醒筛选记录格式")
    workers = data.get("workers")
    count = data.get("worker_count")
    if type(count) is not int or count <= 0 or not isinstance(workers, list) or len(workers) != count:
        raise ValueError("末条唤醒 worker 数量不匹配")
    retained = excluded = 0
    for i, worker in enumerate(workers):
        if worker.get("worker_id") != i or type(worker.get("has_sample")) is not bool:
            raise ValueError("末条唤醒 worker 标识非法")
        for key in ("last_latency_us", "stopping", "retained_samples", "excluded_samples"):
            if type(worker.get(key)) is not int or worker[key] < 0:
                raise ValueError("末条唤醒计数非法")
        if worker["stopping"] not in (0, 1) or worker["excluded_samples"] != int(worker["has_sample"] and worker["stopping"]):
            raise ValueError("末条唤醒删除规则不匹配")
        if worker["has_sample"] and worker["last_latency_us"] <= 0:
            raise ValueError("末条唤醒延迟非法")
        if not worker["has_sample"] and any(worker[k] for k in ("last_latency_us", "stopping", "retained_samples", "excluded_samples")):
            raise ValueError("无样本 worker 含有统计值")
        if worker["has_sample"] and not worker["stopping"] and worker["retained_samples"] < 1:
            raise ValueError("正常末条样本未保留")
        retained += worker["retained_samples"]
        excluded += worker["excluded_samples"]
    for key, value in (("retained_samples", retained), ("excluded_samples", excluded),
                       ("native_wakeup_samples", retained + excluded)):
        if type(data.get(key)) is not int or data[key] != value:
            raise ValueError("末条唤醒汇总计数不匹配")
    counts = re.findall(r"Wakeup Latencies percentiles.*?\((\d+) total samples\)",
                        (attempt / "front.log").read_text(errors="replace"))
    if len(counts) != 1 or int(counts[0]) != retained:
        raise ValueError("末条唤醒保留数与原生最终直方图不一致")
    return {**data, **metric_policy(True), "status": "complete", "capture_complete": True,
            "note": "仅保存各 worker 最后一个样本；不声称排除了全部 stopping=1 样本"}


def apply_running_wakeup(row, details, summary):
    """The native p99 is already filtered; only validate evidence here."""
    details["wakeup_filter"] = {**metric_policy(True), **summary}
    expected = details.get("parameters", {}).get("front", {})
    expected_count = expected.get("message_threads", 1) * expected.get("worker_threads", summary.get("worker_count", 0))
    if (summary.get("status") != "complete" or summary.get("worker_count") != expected_count
            or not summary.get("retained_samples") or row.get("wakeup_p99_us") is None):
        row["wakeup_p99_us"] = None
        details["wakeup_filter"].update(status="unavailable", reason="末条唤醒筛选证据缺失、不完整或没有保留样本")
        return details["wakeup_filter"]["reason"]
    details["wakeup_filter"].update(status="applied", filtered_wakeup_p99_us=row["wakeup_p99_us"])
    details.setdefault("front_metric_sources", {})["wakeup_p99_us"] = "末条 stopping 样本筛选后的原生直方图 p99；µs"
    return None
