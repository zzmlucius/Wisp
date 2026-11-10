"""Mixed native workloads using the existing Wisp measurement harness."""
from pathlib import Path
import sys

WISP = Path(__file__).resolve().parents[2]
for directory, package in (("Compile", "compilebench_runner"), ("Schbench", "schbench_runner")):
    if not (WISP / directory / package).is_dir():
        raise RuntimeError(f"缺少同级 {directory}/{package}；请保留 Wisp 布局")
    sys.path.insert(0, str(WISP / directory))
