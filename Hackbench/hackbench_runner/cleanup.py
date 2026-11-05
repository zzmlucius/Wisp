"""Clean this workload's records only, using compiletest's shared mutex."""
import argparse
import json
import os
import shutil
from pathlib import Path
import sys

from compilebench_runner.cli import acquire_lock
from compilebench_runner.cleanup import remove_entry
from compilebench_runner.system import write_json

PROJECT = Path(__file__).resolve().parents[1]


def register_root(root):
    index = PROJECT / "bench/results-index.json"
    roots = json.loads(index.read_text()).get("roots", []) if index.exists() else []
    value = str(root.resolve())
    if value not in roots:
        roots.append(value)
        index.parent.mkdir(parents=True, exist_ok=True)
        write_json(index, {"roots": roots})


def roots(config_path):
    cfg = json.loads(config_path.read_text())
    configured = (config_path.parent / Path(cfg.get("results_dir", "bench/results")).expanduser())
    candidates = [configured, PROJECT / "bench/results", PROJECT / "bench/validation"]
    index = PROJECT / "bench/results-index.json"
    if index.exists():
        candidates.extend(Path(value) for value in json.loads(index.read_text())["roots"])
    protected = [PROJECT / "hackbench_runner", PROJECT / "tests", PROJECT / "bench/src",
                 PROJECT / "bench/build", PROJECT / "bench/downloads", PROJECT.parent / "Compile", PROJECT.parent / "Schbench"]
    executable = shutil.which(cfg["program"]) if "/" not in cfg["program"] else None
    program = Path(executable or config_path.parent / Path(cfg["program"]).expanduser()).resolve()
    result = []
    for candidate in candidates:
        if candidate.is_symlink():
            raise ValueError(f"拒绝清理符号链接目录: {candidate}")
        root = candidate.resolve()
        if PROJECT.is_relative_to(root) or config_path.is_relative_to(root) or program.is_relative_to(root) or Path.home().is_relative_to(root):
            raise ValueError(f"记录目录包含代码、配置、程序或主目录: {root}")
        for path in protected:
            path = path.resolve()
            if path.is_relative_to(root) or root.is_relative_to(path):
                raise ValueError(f"记录目录与保留目录重叠: {root}")
        if root.exists() and not root.is_dir():
            raise ValueError(f"记录路径不是目录: {root}")
        result.append(root)
    return sorted(set(result))


def plan(config_path, batches):
    if any(not name or name in {".", ".."} or Path(name).name != name for name in batches):
        raise ValueError("批次ID必须为完整目录名，不能包含路径")
    selected = []
    for root in roots(config_path):
        if not root.exists():
            continue
        for entry in sorted(root.iterdir()):
            if batches and entry.name not in batches:
                continue
            if entry.is_symlink() or not entry.is_dir():
                continue
            metadata = entry / "metadata.json"
            if metadata.is_symlink():
                continue
            try:
                info = json.loads(metadata.read_text())
            except (OSError, ValueError):
                continue
            if not isinstance(info, dict) or info.get("workload") != "hackbench":
                continue
            selected.append(entry)
    missing = set(batches) - {path.name for path in selected}
    if missing:
        raise ValueError(f"未找到本测试批次: {', '.join(sorted(missing))}")
    return [entry for entry in sorted(set(selected)) if not any(other != entry and entry.is_relative_to(other) for other in selected)]


def main(argv=None):
    parser = argparse.ArgumentParser(description="清除 hacktest 测试记录；保留程序、源码、配置与 Compile 数据")
    parser.add_argument("batches", nargs="*", metavar="批次ID")
    parser.add_argument("--dry-run", action="store_true", help="只预览，不删除")
    parser.add_argument("-c", "--config", type=Path, default=PROJECT / "hacktest.json")
    args = parser.parse_args(argv)
    lock = None
    try:
        if os.getuid() == 0:
            raise ValueError("请以普通用户运行 Clean，无需 sudo")
        lock = acquire_lock()
        selected = plan(args.config.resolve(), args.batches)
        for entry in selected:
            print(f"{'预览' if args.dry_run else '清理'}: {entry}")
        if not args.dry_run:
            for entry in selected:
                remove_entry(entry)
        print(f"{'将清除' if args.dry_run else '已清除'} {len(selected)} 项 hacktest 记录。源码、程序、配置与 Compile 数据保留。")
        return 0
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        print(f"清理失败: {exc}", file=sys.stderr)
        return 2
    finally:
        if lock is not None:
            os.close(lock)
