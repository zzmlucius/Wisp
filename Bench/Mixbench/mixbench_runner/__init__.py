"""Mixed native workloads using the existing Wisp measurement harness."""
from pathlib import Path
import sys

BENCH = Path(__file__).resolve().parents[2]
for directory, package in (("Compile", "compilebench_runner"), ("Schbench", "schbench_runner")):
    if not (BENCH / directory / package).is_dir():
        raise RuntimeError(f"缺少同级 {directory}/{package}；请保留 Bench 下的同级目录布局")
    sys.path.insert(0, str(BENCH / directory))
