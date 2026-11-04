"""Real desktop-profile observation plus an in-memory EPP regression probe."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from compilebench.policy import Scheduler
from compilebench.system import power_state, write_json
from compilebench.workload import measure


def main():
    directory = ROOT / "bench/validation" / ("power-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ"))
    directory.mkdir(parents=True)
    baseline = power_state()
    replay = dict(baseline)
    keys = [key for key in baseline if key.endswith("/energy_performance_preference")]
    if keys:
        replay[keys[0]] = "balance_power" if baseline[keys[0]] != "balance_power" else "balance_performance"
    scheduler = Scheduler()
    result, notes, _ = measure([sys.executable, "-c", "import time;time.sleep(2)"], ROOT, dict(os.environ),
        directory / "workload.log", directory / "samples.csv", scheduler, scheduler.initial(), None,
        replay, interval=0.2)
    checks = {"desktop_profile_readable": baseline.get("desktop_profile") is not None,
              "workload_valid_despite_epp_replay": result["valid"],
              "epp_diagnostic_captured": bool(keys) and bool(notes["power"]["events"])}
    artifact = {"kind": "power_observation_check", "observed_start": baseline,
                "result": result, "notes": notes, "checks": checks,
                "purpose": "仅在内存中改写一项预期 EPP，验证其仅作诊断；不写 sysfs，不切换电源或调度器，不是正式成绩"}
    write_json(directory / "result.json", artifact)
    print(json.dumps({"directory": str(directory), "checks": checks,
                      "desktop_profile": baseline.get("desktop_profile"), "result": result}, indent=2, ensure_ascii=False))
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
