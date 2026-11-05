"""schbench workload adapter using Wisp's existing measurement harness."""
from pathlib import Path
import sys

COMPILE = Path(__file__).resolve().parents[2] / "Compile"
if not (COMPILE / "compilebench").is_dir():
    raise RuntimeError("缺少同级 Compile/compilebench；请保留 Wisp 目录布局")
sys.path.insert(0, str(COMPILE))
