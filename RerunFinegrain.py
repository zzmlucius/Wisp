#!/usr/bin/env python3
"""Repeat the three unfinished Finegraintest attempts at the confirmed cutoff."""
import argparse
from contextlib import ExitStack
import csv
import ctypes
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import shutil
import sys

import Finegraintest as fine
from compilebench_runner.control import BatchScheduler
from compilebench_runner.policy import Scheduler
from compilebench_runner.system import digest, power_error, read as read_system
from mixbench_runner import cli as mixed, collector
from mixbench_runner.results import FIELDS, summarize


QUEUE = fine.ROOT / 'Mixbench/bench/results/finegraintest/20261120T184958.190631Z-5446'
BATCH = QUEUE / 'results/14-14-1000-1_20261121T031137.885264Z-5446'
CUTOFF = datetime.fromisoformat('2026-11-21T11:22:54+08:00')
TARGETS = ('run-003/scx_bpfland', 'run-003/scx_rusty', 'run-003/scx_lavd')
DONE = {'completed', 'completed_with_invalid_results'}


def read(path):
    return json.loads(path.read_text())


def plan(batch=BATCH, queue=QUEUE):
    if read(queue / 'status.json')['status'] not in DONE:
        raise RuntimeError('原队列尚未正常收尾')
    metadata = read(batch / 'metadata.json')
    if metadata.get('replacements'):
        raise RuntimeError('原批次已有补跑替换记录')
    actual = tuple(name for name in metadata['attempts']
                   if (batch / name / 'attempt.json').stat().st_mtime > CUTOFF.timestamp())
    if actual != TARGETS:
        raise RuntimeError(f'原记录与确认的截止范围不一致：{actual}')
    first = batch / actual[0]
    if (first / 'launch.json').stat().st_mtime > CUTOFF.timestamp():
        raise RuntimeError('第一项在截止时间之前尚未放行至测试协调器')
    return metadata


def check_environment(original, machine):
    for key in ('uname', 'available_cpus'):
        if original['machine'][key] != machine[key]:
            raise RuntimeError(f'当前系统条件与原批次不同：{key}')
    if read_system('/sys/devices/system/cpu/online') != original['online_cpus']:
        raise RuntimeError('在线 CPU 范围与原批次不同')
    issue = power_error(original['machine']['power'], machine['power'], '补跑')
    if issue:
        raise RuntimeError(issue)
    for path, sha in original['frozen_files'].items():
        if digest(path) != sha:
            raise RuntimeError(f'原批次的固定文件已变化：{path}')


def exchange_directories(a, b):
    """Atomically publish the replacement and retain the full original batch."""
    libc = ctypes.CDLL(None, use_errno=True)
    rename_exchange = libc.renameat2
    rename_exchange.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename_exchange.restype = ctypes.c_int
    if rename_exchange(-100, os.fsencode(a), -100, os.fsencode(b), 2):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(a), None, str(b))


def publish(work, source, replacement, metadata, *, batch=BATCH, targets=TARGETS):
    # This directory becomes the backup at the moment of the atomic exchange.
    backup = work / 'original-batch'
    shutil.copytree(batch, backup, symlinks=True)
    for relative in targets:
        target = backup / relative
        shutil.rmtree(target)
        shutil.copytree(source / relative, target, symlinks=True)
        snap = read(target / 'snapshot.json')
        snap['rerun_snapshot'] = str(source / 'snapshot.json')
        fine.night.write_status(target / 'snapshot.json', snap)
    with (batch / 'summary.csv').open(newline='') as f:
        old = {f"run-{int(row['run']):03d}/{row['policy']}": row for row in csv.DictReader(f)}
    if list(old) != metadata['attempts']:
        raise RuntimeError('原 summary.csv 与原批次尝试顺序不一致')
    details = [read(backup / name / 'attempt.json') for name in metadata['attempts']]
    with (backup / 'summary.csv').open('w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        for name, detail in zip(metadata['attempts'], details):
            writer.writerow(detail['result'] if name in targets else old[name])
        f.flush()
        os.fsync(f.fileno())
    rows = [detail['result'] for detail in details]
    metadata.update(
        status='completed' if all(row['valid'] for row in rows) else 'failed',
        analysis=summarize(rows, [detail['constraints'] for detail in details], metadata['config']['case']),
        replacements=[replacement],
    )
    fine.night.write_status(backup / 'metadata.json', metadata)
    exchange_directories(backup, batch)
    return backup


def main(argv=None):
    parser = argparse.ArgumentParser(description='补跑 11:22:54 时未完成的三个 Finegraintest 尝试')
    parser.add_argument('--plan', action='store_true', help='只列出目标，无需 sudo')
    args = parser.parse_args(argv)
    work = None
    try:
        if args.plan:
            plan()
            for target in TARGETS:
                print(BATCH / target)
            return 0
        if os.getuid() == 0:
            raise RuntimeError('请从普通用户的 Fedora 终端运行')
        interrupts = fine.Interrupts()
        with ExitStack() as stack:
            stack.callback(os.close, fine.acquire_lock())
            queue_lock = stack.enter_context((fine.ROOT / 'bench/overnight/queue.lock').open('a'))
            fcntl.flock(queue_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            metadata = plan()
            original = read(BATCH / 'snapshot.json')
            machine = fine.midnight.machine_info()
            check_environment(original, machine)
            initial = Scheduler().initial()
            if initial['state'] != 'disabled':
                raise RuntimeError('请先停止外部 scx 调度器')
            config_path = QUEUE / 'inputs/14-14-1000-1.json'
            if digest(config_path) != original['settings_sha256']:
                raise RuntimeError('原队列输入已变化')
            cfg = fine.load_config(config_path)
            if fine.effective(cfg) != metadata['config']:
                raise RuntimeError('重新解析的负载配置与原批次不同')
            options = collector.prepare(cfg)
            check_environment(original, machine)
            if set(options['files']) - set(original['frozen_files']) or options.get('probe_build_error'):
                raise RuntimeError('采集器与原批次不一致')
            cfg.update(_collector={**options, 'prefix': fine.PREFIX, 'authorized': True},
                       _collector_required=True)
            stamp = datetime.now(CUTOFF.tzinfo).strftime('%Y%m%dT%H%M%S.%f%z')
            work = QUEUE / 'reruns' / f'{stamp}-{os.getpid()}'
            work.mkdir(parents=True)
            stage = work / 'new-attempts'
            stage.mkdir()
            interrupts.install()
            stack.callback(interrupts.restore)
            guard = stack.enter_context(fine.midnight.DesktopGuard('RerunFinegrain'))
            snapshot, sensor = mixed.save_inputs(cfg, stage, initial, machine=machine)
            stack.callback(fine.restore_noninteractive, os.environ.get('WISP_SUDO_NONINTERACTIVE'))
            os.environ['WISP_SUDO_NONINTERACTIVE'] = '1'
            print(f'补跑记录 {work}。启动时可能要求输入一次 sudo 密码。', flush=True)
            log = stack.enter_context((work / 'authorization.log').open('a', buffering=1))
            authorization = stack.enter_context(fine.night.Authorization(log))
            fine.check_attachment(cfg, work, interrupts)
            controller = BatchScheduler(fine.scheduler_specs(cfg), stage, interrupts,
                timeout=cfg['scheduler_timeout_s'], settle=cfg['scheduler_settle_s'], workload_name='mixtest')
            replacement = {'cutoff': CUTOFF.isoformat(), 'attempts': list(TARGETS),
                           'source': str(stage), 'original_batch_backup': str(work / 'original-batch')}
            try:
                controller.open()
                for expected in TARGETS:
                    interrupts.check()
                    authorization.check()
                    guard.check()
                    check_environment(original, fine.midnight.machine_info())
                    _, policy = expected.split('/')
                    print(f'补跑 {expected}', flush=True)
                    row, cancelled, fatal, detail, actual = mixed.run_attempt(
                        cfg, stage, snapshot, sensor, controller, initial, policy, 3, True, interrupts)
                    if cancelled:
                        raise KeyboardInterrupt
                    if (actual != expected or fatal or not detail['technical_valid'] or
                            detail['workload_collection']['status'] != 'complete'):
                        raise RuntimeError(f'{expected} 补跑未通过执行或采集核验：{fatal or row["error"]}')
                    metrics = read(stage / expected / f'{fine.PREFIX}-meta.json')['indicator_status_counts']
                    for metric in ('fg_cpu_cores_used', 'bg_cpu_cores_used',
                                   'fg_cpu_psi_some_pct', 'bg_cpu_psi_some_pct'):
                        if not metrics[metric].get('ok'):
                            raise RuntimeError(f'{expected} 缺少 {metric}，请核对 cgroup 委派')
            finally:
                try:
                    controller.close()
                finally:
                    fine.night.write_status(stage / 'controller.json', controller.info)
            interrupts.check()
            plan()
            replacement['published_utc'] = fine.night.stamp()
            with interrupts.defer():
                backup = publish(work, stage, replacement, metadata)
                fine.night.write_status(work / 'status.json', {'status': 'replaced', **replacement})
            print(f'已替换三次记录。原批次备份 {backup}')
            return 0
    except (KeyboardInterrupt, Exception) as exc:
        cancelled = isinstance(exc, KeyboardInterrupt)
        error = '用户中断' if cancelled else str(exc)
        if work is not None and not (work / 'status.json').exists():
            fine.night.write_status(work / 'status.json',
                                   {'status': 'interrupted' if cancelled else 'failed', 'error': error})
        print(error, file=sys.stderr)
        return 130 if cancelled else 2


if __name__ == '__main__':
    raise SystemExit(main())
