"""Hackbench adapter using the existing Wisp sched_ext measurement harness."""
from pathlib import Path
import sys

WISP = Path(__file__).resolve().parents[2]
if not (WISP / "Compile/compilebench_runner").is_dir() or not (WISP / "Schbench/schbench_runner").is_dir():
    raise RuntimeError("需要同级 Compile/compilebench_runner 和 Schbench/schbench_runner；请保留 Wisp 布局")
sys.path.insert(0, str(WISP))
sys.path.insert(0, str(WISP / "Schbench"))
sys.path.insert(0, str(WISP / "Compile"))
