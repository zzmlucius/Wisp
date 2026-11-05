"""Remove selected records and their generated configs; preserve shared inputs."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import sys

from .cli import acquire_lock

PROJECT = Path(__file__).resolve().parents[1]
BATCH_ID = re.compile(r"[0-9]{8}T[0-9]{6}(?:\.[0-9]+)?Z-[0-9]+")


def record_directories(config_path):
    config_path = config_path.resolve()
    cfg = json.loads(config_path.read_text())

    def configured(key, default=None):
        return config_path.parent / Path(cfg.get(key, default)).expanduser()

    results = configured("results_dir")
    protected = [configured("source").resolve(), configured("kernel_config").resolve().parent,
                 configured("build_root", str(results.resolve().parent / "compiletest-builds")).resolve()]
    candidates = [results, results.resolve().parent / "validation", PROJECT / "bench/results",
                  PROJECT / "bench/validation", PROJECT / "results"]
    directories = []
    for candidate in candidates:
        if candidate.is_symlink():
            raise ValueError(f"记录目录不能是符号链接: {candidate}")
        directory = candidate.resolve()
        if (PROJECT.is_relative_to(directory) or Path.home().is_relative_to(directory)
                or config_path.is_relative_to(directory)):
            raise ValueError(f"拒绝清理包含项目、主目录或配置文件的目录: {directory}")
        for path in protected:
            if path.is_relative_to(directory) or directory.is_relative_to(path):
                raise ValueError(f"记录目录与源码、配置或构建目录重叠: {directory}")
        if directory.exists() and not directory.is_dir():
            raise ValueError(f"记录路径不是目录: {directory}")
        directories.append(directory)
    # Avoid processing nested or duplicate roots twice.
    return [directory for directory in sorted(set(directories))
            if not any(parent != directory and directory.is_relative_to(parent)
                       for parent in directories)]


def batch_metadata(directory):
    if directory.is_symlink() or not directory.is_dir():
        return {}
    path = directory / "metadata.json"
    if path.is_symlink():
        return {}
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def generated_paths(batch, metadata):
    paths = metadata.get("generated_configs", [])
    if not isinstance(paths, list):
        raise ValueError(f"generated_configs 必须为路径数组: {batch}")
    return [Path(os.path.abspath(batch / Path(path).expanduser())) for path in paths]


def generated_configurations(config_path, selected, retained, batch_ids=None):
    config_path = config_path.resolve()
    cfg = json.loads(config_path.read_text())
    configured = lambda name: (config_path.parent / Path(cfg[name]).expanduser()).resolve()
    kernel = configured("kernel_config")
    roots = {kernel.parent, (PROJECT / "bench/config").resolve()}
    protected = {kernel, kernel.with_suffix(".json"), config_path,
                 (PROJECT / "bench/config/kernel.config").resolve(),
                 (PROJECT / "bench/config/kernel.json").resolve()}
    source = configured("source")
    build = (config_path.parent / Path(cfg.get("build_root",
             str(configured("results_dir").parent / "compiletest-builds"))).expanduser()).resolve()
    # A retained experiment may still depend on a generated config.
    for entry in retained:
        metadata = batch_metadata(entry)
        protected.update(path.resolve() for path in generated_paths(entry, metadata))
        parameters = metadata.get("config")
        external_input = parameters.get("kernel_config") if isinstance(parameters, dict) else None
        if external_input:
            path = Path(external_input).expanduser()
            protected.add((path if path.is_absolute() else config_path.parent / path).resolve())
    ids = set(batch_ids or (entry.name for entry in selected))
    candidates = set()
    for root in roots:
        if not root.is_dir():
            continue
        for entry in root.iterdir():
            if entry.is_symlink():
                continue
            if not entry.is_dir() and entry.suffix not in {".config", ".json", ".log"}:
                continue
            stem = entry.name if entry.is_dir() else entry.stem
            names = {stem, stem.removeprefix("kernel-"), stem.removeprefix("config-")}
            if names & ids or (batch_ids is None and any(BATCH_ID.fullmatch(name) for name in names)):
                candidates.add(entry)
    for entry in selected:
        candidates.update(generated_paths(entry, batch_metadata(entry)))
    result = []
    for candidate in sorted(candidates):
        # Resolve for validation, but unlink the lexical entry, never its target.
        path = candidate.resolve()
        if not any(path != root and path.is_relative_to(root) for root in roots):
            raise ValueError(f"关联配置超出配置目录，拒绝删除: {candidate}")
        if path.is_relative_to(source) or source.is_relative_to(path) or path.is_relative_to(build) or build.is_relative_to(path):
            raise ValueError(f"关联配置与源码或构建目录重叠: {candidate}")
        if any(item == path or item.is_relative_to(path) for item in protected):
            continue
        if candidate.exists() or candidate.is_symlink():
            result.append(candidate)
    return [entry for entry in result if not any(parent != entry and entry.is_relative_to(parent)
                                                for parent in result if parent.is_dir() and not parent.is_symlink())]


def remove_entry(entry):
    if entry.is_symlink() or not entry.is_dir():
        entry.unlink()
    else:
        shutil.rmtree(entry)


def main(argv=None):
    parser = argparse.ArgumentParser(description="清除实验记录及其独立配置，保留共享配置、源码和构建产物")
    parser.add_argument("batches", nargs="*", metavar="批次ID", help="只清理指定批次；省略则清理全部")
    parser.add_argument("-c", "--config", type=Path, default=PROJECT / "compiletest.json")
    parser.add_argument("--dry-run", action="store_true", help="只列出清理范围，不删除")
    args = parser.parse_args(argv)
    lock = None
    try:
        if os.getuid() == 0:
            raise ValueError("请以普通用户运行 Clean，无需 sudo")
        lock = acquire_lock()  # Shared with compiletest; never erase an active batch.
        directories = record_directories(args.config)
        if any(not name or name in {".", ".."} or Path(name).name != name for name in args.batches):
            raise ValueError("批次ID只能是目录名，不能包含路径")
        entries = [entry for directory in directories if directory.exists() for entry in sorted(directory.iterdir())]
        selected = [entry for entry in entries if not args.batches or entry.name in args.batches]
        retained = [entry for entry in entries if entry not in selected]
        configurations = generated_configurations(args.config, selected, retained, args.batches or None)
        if args.batches:
            for name in args.batches:
                associated = generated_configurations(args.config, [], retained, [name])
                if not any(entry.name == name for entry in selected) and not associated:
                    raise ValueError(f"未找到批次或对应独立配置: {name}")
        # Validate the entire plan before removing either records or configs.
        for path in configurations:
            print(f"{'预览配置' if args.dry_run else '清理配置'}: {path}")
            if not args.dry_run:
                remove_entry(path)
        for directory in directories:
            children = [entry for entry in selected if entry.parent == directory]
            if not children:
                continue
            print(f"{'预览' if args.dry_run else '清理'}: {directory}（{len(children)} 项）")
            for entry in children:
                if not args.dry_run:
                    remove_entry(entry)
        print(f"{'将清除' if args.dry_run else '已清除'} {len(selected)} 项测试记录和 {len(configurations)} 项关联配置。共享配置、源码和构建产物保留。")
        return 0
    except KeyboardInterrupt:
        print("清理已中断", file=sys.stderr)
        return 130
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        print(f"清理失败: {exc}", file=sys.stderr)
        return 2
    finally:
        if lock is not None:
            os.close(lock)
