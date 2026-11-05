"""Optional real cgroup integration check; run inside a delegated user service."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from compilebench_runner.policy import Scheduler
from compilebench_runner.system import power_state, write_json
from compilebench_runner.workload import measure


def main():
    directory = ROOT / "bench/validation" / ("cgroup-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ"))
    directory.mkdir(parents=True)
    child = ("import time; p=bytearray(32*1024*1024); "
             "p[::4096]=b'x'*(len(p)//4096); time.sleep(0.3)")
    program = f"import subprocess,sys; subprocess.run([sys.executable,'-c',{child!r}],check=True)"
    scheduler = Scheduler()
    result, notes, _ = measure([sys.executable, "-c", program], ROOT, dict(os.environ),
        directory / "workload.log", directory / "samples.csv", scheduler, scheduler.initial(),
        None, power_state(), interval=0.05)
    checks = {"workload_valid": result["valid"],
              "cpu_accounted": result["cpu_time_s"] is not None and result["cpu_time_s"] > 0,
              "child_memory_accounted": result["peak_memory_bytes"] is not None
                  and result["peak_memory_bytes"] >= 32 * 1024 * 1024}
    artifact = {"result": result, "notes": notes, "checks": checks,
                "purpose": "child touches 32 MiB; memory peak must cover the child, not just its Python parent"}
    write_json(directory / "result.json", artifact)
    print(json.dumps({"directory": str(directory), **artifact}, indent=2, ensure_ascii=False))
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
