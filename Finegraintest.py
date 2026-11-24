#!/usr/bin/env python3
"""Run a matrix of Mixbench workloads with a separate collector output prefix."""
import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import fcntl
import importlib.machinery
import importlib.util
from itertools import product
import json
import os
import re
import shutil
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent
loader = importlib.machinery.SourceFileLoader('finegrain_midnight', str(ROOT / 'midnighttest'))
spec = importlib.util.spec_from_loader(loader.name, loader)
midnight = importlib.util.module_from_spec(spec)
loader.exec_module(midnight)
night = midnight.night

from compilebench_runner.cli import acquire_lock, scheduler_specs
from compilebench_runner.control import POLICIES
from compilebench_runner.interrupts import Interrupts
from compilebench_runner.system import power_state
from mixbench_runner import cli as mixed, collector
from mixbench_runner.cli import result_directory_name
from mixbench_runner.configuration import load_config
from schbench_runner.cli import effective


THREADS = (4, 8, 14)
RPS_VALUES = (250, 500, 750, 1000)
RUNTIME_S = 30
COOLDOWN_S = 30
BATCH_REST_S = 45
PREFIX = 'finegrain-workload'
BPF_OBJECT = ROOT / 'Collector/bench/build/scheduler.bpf.o'


def prepare_jobs(directory, power, rps_values=RPS_VALUES):
    base = load_config(ROOT / 'Mixbench/mixtest.json')
    base['collector'] = {**base.get('collector', {}), 'bpf_object': str(BPF_OBJECT),
                         'runqueue_verified': False}
    native_collect = {**collector.prepare(base), 'prefix': PREFIX, 'authorized': True}
    if str(BPF_OBJECT) not in native_collect['files'] or native_collect.get('probe_build_error'):
        raise RuntimeError('需要现有的调度探针和 libbpf 采集器，请先执行 make -C Collector bpf')
    settings = effective(base)
    settings.update(runtime_s=RUNTIME_S, cooldown_s=COOLDOWN_S,
                    schedulers=scheduler_specs(base), results_dir=str(directory / 'results'))
    inputs = directory / 'inputs'
    inputs.mkdir()
    (directory / 'logs').mkdir()
    jobs = []
    for front, back, rps in product(THREADS, THREADS, rps_values):
        settings['front']['worker_threads'] = front
        settings['back']['threads'] = back
        settings['rps'] = rps
        name = result_directory_name(settings, power, '').rstrip('_')
        path = inputs / (name + '.json')
        night.write_status(path, settings)
        cfg = load_config(path)
        cfg.update(_collector=native_collect, _collector_required=True)
        jobs.append({'name': name, 'config_file': str(path), 'config': cfg,
                     'command': midnight.commands(cfg, directory / 'results' / name / 'run-NNN' / 'POLICY')})
    return jobs


def check_collector(cfg, directory):
    options = cfg['_collector']
    output = directory / 'collector-preflight'
    output.mkdir()
    command = [options['binary'], '--output', str(output), '--prefix', PREFIX,
               '--cpus', ','.join(map(str, cfg['cpus'])), '--interval', '0.1', '--samples', '2']
    for key, flag in (('device', '--device'), ('bpf_object', '--bpf-object')):
        if options.get(key):
            command.extend([flag, options[key]])
    if options.get('runqueue_verified'):
        command.append('--runqueue-verified')
    with (output / 'collector.log').open('w') as log:
        subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=10)
    meta = json.loads((output / f'{PREFIX}-meta.json').read_text())
    status = [json.loads(line) for line in (output / f'{PREFIX}-status.jsonl').read_text().splitlines()]
    indices = {column['name']: column['id'] for column in meta['columns']}
    required = ('runnable_per_cpu', 'mem_available_kib', 'context_switches_per_sec')
    if len(meta['columns']) != 19 or len(status) != 2 or any(
            status[-1]['indicators'][indices[name]][0] != 'ok' for name in required):
        raise RuntimeError('采集器预检没有生成有效的基础向量，见 collector-preflight')
    return {'schema_version': meta['schema_version'], 'probes': meta['probes'],
            'diagnostics': str(output.relative_to(directory)), 'files': options['files']}


def check_attachment(cfg, directory, interrupts):
    output = directory / 'collector-authorized-preflight'
    output.mkdir()
    spec = {'collector': cfg['_collector'], 'cpus': cfg['cpus'], 'sample_interval_s': 0.5}
    processes = []
    try:
        collector.start(processes, spec, output, {}, interrupts, samples=3)
        if processes[0].wait(timeout=10):
            raise RuntimeError('授权采集预检失败，见 collector.log')
    finally:
        for process in processes:
            collector.stop(process)
    meta = json.loads((output / f'{PREFIX}-meta.json').read_text())
    if meta['probes'] != {'scheduler': 'attached', 'runqueue': 'unverified'}:
        raise RuntimeError('调度探针预检失败，见 collector-authorized-preflight')
    if 'scheduler probe descriptors closed' not in (output / 'collector.log').read_text():
        raise RuntimeError('授权采集预检未确认探针文件描述符关闭')
    return meta['probes']


def check_one_workload(cfg, directory, interrupts):
    result_root = directory / 'ebpf-validation'
    code = mixed.experiment({**cfg, 'results_dir': str(result_root)}, interrupts)
    if code == 130:
        raise KeyboardInterrupt
    metadata, = result_root.glob('*/metadata.json')
    batch = json.loads(metadata.read_text())
    if len(batch.get('attempts', [])) != 1:
        raise RuntimeError('eBPF 短测未完成一次尝试')
    attempt = metadata.parent / batch['attempts'][0]
    details = json.loads((attempt / 'attempt.json').read_text())
    collection = details.get('workload_collection', {})
    if not details['technical_valid'] or collection.get('status') != 'complete':
        raise RuntimeError(f'eBPF 短测失败：{details.get("technical_error") or collection.get("error")}，见 {attempt}')
    log = (attempt / 'collector.log').read_text()
    ids = {int(value) for value in re.findall(r'^scheduler probe program id: (\d+)$', log, re.M)}
    if len(ids) != 5:
        raise RuntimeError(f'短测未记录完整的 5 个调度探针程序 ID，见 {attempt}')
    deadline = time.monotonic() + 3
    while True:
        interrupts.check()
        result = subprocess.run(['sudo', '-n', '--', 'bpftool', '-j', 'prog', 'show'],
                                capture_output=True, text=True, check=True, timeout=10)
        remaining = ids & {info['id'] for info in json.loads(result.stdout)}
        if not remaining:
            break
        if time.monotonic() >= deadline:
            raise RuntimeError(f'内核仍有本次调度探针程序 ID {sorted(remaining)}')
        time.sleep(0.1)
    print(f'已验证 4 项调度事件和本次 5 个程序 ID 已卸载，采集文件 {attempt / collection["csv"]}', flush=True)
    return 0


def restore_noninteractive(previous):
    if previous is None:
        os.environ.pop('WISP_SUDO_NONINTERACTIVE', None)
    else:
        os.environ['WISP_SUDO_NONINTERACTIVE'] = previous


def main(argv=None, *, rps_values=RPS_VALUES, entrypoint=Path(__file__), round_progress=False):
    label = entrypoint.name
    parser = argparse.ArgumentParser(description='运行细粒度 Mixbench 参数矩阵')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--check', action='store_true',
                        help='生成矩阵并预检采集器、防熄屏及系统资源')
    mode.add_argument('--check-ebpf', action='store_true',
                      help='授权后用默认调度器实跑一次，验证 CSV 调度事件与探针收尾')
    args = parser.parse_args(argv)
    if os.getuid() == 0:
        print(f'请以普通用户运行 {label}', file=sys.stderr)
        return 2
    if args.check_ebpf and not shutil.which('bpftool'):
        print('--check-ebpf 需要 bpftool 来核对探针是否卸载', file=sys.stderr)
        return 2
    directory, guard = None, None
    interrupts = Interrupts()
    try:
        with ExitStack() as stack:
            lock = acquire_lock()
            stack.callback(os.close, lock)
            queue_root = ROOT / 'bench/overnight'
            queue_root.mkdir(parents=True, exist_ok=True)
            queue_lock = stack.enter_context((queue_root / 'queue.lock').open('a'))
            fcntl.flock(queue_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            interrupts.install()
            stack.callback(interrupts.restore)
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
            directory = ROOT / 'Mixbench/bench/results' / entrypoint.stem.lower() / f'{stamp}-{os.getpid()}'
            directory.mkdir(parents=True)
            print(f'队列与结果目录 {directory}', flush=True)

            guard = stack.enter_context(midnight.DesktopGuard(label))
            jobs = prepare_jobs(directory, power_state(), rps_values)
            cfg = jobs[0]['config']
            report = midnight.preflight(jobs, directory, label=label, batch_rest_s=BATCH_REST_S, extra_files=(
                Path(__file__).resolve(), entrypoint.resolve(), ROOT / 'Mixbench/mixtest.json', *cfg['_collector']['files']))
            report.update(inhibition=dict(guard.info), collector=check_collector(cfg, directory),
                          matrix={'front_workers': THREADS, 'back_threads': THREADS, 'rps': rps_values,
                                  'policies': list(POLICIES), 'front_runtime_s': RUNTIME_S,
                                  'back_runtime_s': RUNTIME_S})
            midnight.verify_queue(report, directory, guard)
            night.write_status(directory / 'preflight.json', report)
            if args.check:
                night.write_status(directory / 'status.json', {'status': 'checked', 'jobs': [
                    {'name': j['name'], 'config_file': j['config_file']} for j in jobs]})
                print(f"预检通过，共 {report['batch_count']} 批、{report['attempt_count']} 次。", flush=True)
                return 0

            stack.callback(restore_noninteractive, os.environ.get('WISP_SUDO_NONINTERACTIVE'))
            os.environ['WISP_SUDO_NONINTERACTIVE'] = '1'
            if args.check_ebpf:
                print('将用默认调度器实跑一次。启动时可能要求一次 sudo 密码。', flush=True)
            else:
                print(f"{report['batch_count']} 批、{report['attempt_count']} 次，预计至少 "
                      f"{report['minimum_time_s'] / 3600:.1f} 小时。启动时可能要求一次 sudo 密码。", flush=True)
            log = stack.enter_context((directory / 'authorization.log').open('a', buffering=1))
            authorization = stack.enter_context(night.Authorization(log))
            midnight.verify_queue(report, directory, guard)
            report['collector']['authorized_probes'] = check_attachment(cfg, directory, interrupts)
            night.write_status(directory / 'preflight.json', report)
            if args.check_ebpf:
                code = check_one_workload(cfg, directory, interrupts)
                night.write_status(directory / 'status.json', {'status': 'ebpf_verified'})
                return code
            midnight.scheduler_preflight(cfg, directory / 'scheduler-preflight', interrupts)
            authorization.check()
            midnight.verify_queue(report, directory, guard)
            return midnight.run_queue(jobs, directory, authorization, guard, report, interrupts,
                                      round_progress=round_progress)
    except (KeyboardInterrupt, Exception) as exc:
        cancelled = isinstance(exc, KeyboardInterrupt)
        error = '用户中断' if cancelled else str(exc)
        if directory is not None and not (directory / 'status.json').exists():
            night.write_status(directory / 'status.json', {'status': 'interrupted' if cancelled else 'failed',
                         'phase': 'preflight_or_authorization', 'error': error})
        if not cancelled:
            print(f'{label} 未能完成：{error}', file=sys.stderr)
        return 130 if cancelled else 2
    finally:
        if directory is not None and guard is not None:
            status = directory / 'status.json'
            if status.exists():
                try:
                    data = json.loads(status.read_text())
                    data['inhibition'] = dict(guard.info)
                    night.write_status(status, data)
                except (OSError, ValueError) as exc:
                    print(f'保存防熄屏收尾状态失败：{exc}', file=sys.stderr)


if __name__ == '__main__':
    raise SystemExit(main())
