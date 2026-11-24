"""Hackbench adapter using the existing Wisp sched_ext measurement harness."""
from pathlib import Path
import sys

BENCH = Path(__file__).resolve().parents[2]
if not (BENCH / "Compile/compilebench_runner").is_dir() or not (BENCH / "Schbench/schbench_runner").is_dir():
    raise RuntimeError("需要同级 Compile/compilebench_runner 和 Schbench/schbench_runner；请保留 Bench 下的同级目录布局")
sys.path.insert(0, str(BENCH))
sys.path.insert(0, str(BENCH / "Schbench"))
sys.path.insert(0, str(BENCH / "Compile"))
