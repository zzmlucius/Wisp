"""Session, preflight and queue control for Finegraintest."""
import contextlib
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time

from compilebench_runner.cli import scheduler_specs
from compilebench_runner.control import BatchScheduler, POLICIES
from compilebench_runner.metrics import BuildCgroup
from compilebench_runner.policy import Scheduler
from compilebench_runner.system import digest, machine_info, power_error, power_state, sensor_path, temperature
from mixbench_runner import cli as mixed
from mixbench_runner.configuration import commands

BENCH = Path(__file__).resolve().parent
ROUNDS_PER_BATCH = 3
MIN_FREE_BYTES = 1024**3


def stamp():
    return datetime.now(timezone.utc).isoformat()


def write_status(path, status):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(status, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(path)


class Authorization:
    """All sudo children share this Python parent, including refreshes."""
    def __init__(self, log):
        self.log = log
        self.stop = threading.Event()
        self.failure = None
        self.thread = None

    def validate(self):
        subprocess.run(['sudo', '-n', '-v'], stdin=subprocess.DEVNULL,
                       stdout=self.log, stderr=self.log, check=True, timeout=15)

    def check(self):
        if self.failure:
            raise RuntimeError('sudo 非交互刷新失败，停止后续批次: ' + self.failure)
        self.validate()

    def refresh(self):
        while not self.stop.wait(30):
            try:
                self.validate()
            except Exception as exc:
                self.failure = str(exc)
                return

    def __enter__(self):
        # The ONLY interactive command in the entire queue.
        subprocess.run(['sudo', '-v'], check=True)
        self.validate()
        self.thread = threading.Thread(target=self.refresh, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self.thread.join(timeout=16)


@contextlib.contextmanager
def batch_log(path):
    # Also capture stderr from noninteractive sudo and other subprocesses.
    sys.stdout.flush()
    sys.stderr.flush()
    saved = [os.dup(1), os.dup(2)]
    with path.open('a', buffering=1) as stream:
        try:
            os.dup2(stream.fileno(), 1)
            os.dup2(stream.fileno(), 2)
            yield
        finally:
            sys.stdout.flush()
            sys.stderr.flush()
            for fd, previous in zip((1, 2), saved):
                os.dup2(previous, fd)
                os.close(previous)


def batches(root):
    return {p.parent for p in root.glob('*/metadata.json')}


def can_continue(code, metadata):
    # valid=False (including request-p99 excess) must not discard later RPS groups.
    return (code in (0, 1) and not metadata.get('fatal_error')
            and metadata.get('status') in ('completed', 'failed')
            and len(metadata.get('attempts', [])) == 12
            and metadata.get('final_scheduler', {}).get('state') == 'disabled'
            and metadata.get('scheduler_controller', {}).get('status') == 'closed'
            and metadata.get('scheduler_controller', {}).get('exit_code') == 0)


class DesktopGuard:
    """Own GNOME idle/suspend cookie and logind inhibitor FD in this process."""
    def __init__(self, label='Finegraintest.py'):
        self.label = label
        self.session = None
        self.cookie = None
        self.fd = None
        self.info = {'status': 'not_acquired', 'gnome_flags': 12, 'logind_what': 'sleep:idle',
                     'scope': 'entire queue, including cooldown and authentication'}

    def call(self, method, parameters, reply):
        return self.session.call_sync('org.gnome.SessionManager', '/org/gnome/SessionManager',
            'org.gnome.SessionManager', method, parameters, self.GLib.VariantType.new(reply),
            self.Gio.DBusCallFlags.NONE, 5000, None)

    def __enter__(self):
        try:
            import gi
            gi.require_version('Gio', '2.0')
            from gi.repository import Gio, GLib
            self.Gio, self.GLib = Gio, GLib
            self.session = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            self.cookie, = self.call('Inhibit', GLib.Variant('(susu)',
                (self.label, 0, 'Wisp mixed benchmark queue', 12)), '(u)').unpack()
            system = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
            reply, descriptors = system.call_with_unix_fd_list_sync('org.freedesktop.login1',
                '/org/freedesktop/login1', 'org.freedesktop.login1.Manager', 'Inhibit',
                GLib.Variant('(ssss)', ('sleep:idle', self.label, 'Wisp mixed benchmark queue', 'block')),
                GLib.VariantType.new('(h)'), Gio.DBusCallFlags.NONE, 5000, None, None)
            index, = reply.unpack()
            self.fd = descriptors.get(index)
            self.info['status'] = 'held'
            self.check()
            return self
        except BaseException:
            self.close()
            raise

    def check(self):
        if self.fd is None or self.cookie is None:
            raise RuntimeError('整批防熄屏/休眠锁未持有')
        os.fstat(self.fd)
        for flag in (4, 8):
            active, = self.call('IsInhibited', self.GLib.Variant('(u)', (flag,)), '(b)').unpack()
            if not active:
                raise RuntimeError('GNOME 防熄屏/休眠锁失效')

    def close(self):
        try:
            if self.cookie is not None:
                self.call('Uninhibit', self.GLib.Variant('(u)', (self.cookie,)), '()')
        finally:
            self.cookie = None
            if self.fd is not None:
                os.close(self.fd)
                self.fd = None
            self.info['status'] = 'released'

    def __exit__(self, *exc):
        self.close()


def preflight(jobs, directory, *, label='Finegraintest.py', batch_rest_s, extra_files=()):
    if not shutil.which('sudo') or not shutil.which('systemd-inhibit'):
        raise RuntimeError('需要 sudo 和 systemd-inhibit')
    initial = Scheduler().initial()
    if initial['state'] != 'disabled':
        raise RuntimeError(f'请先停止外部 scx 调度器，再运行 {label}')
    Scheduler().verify(initial, os.getpid())
    attempts_per_batch = ROUNDS_PER_BATCH * len(POLICIES)
    workload_seconds = sum(j['config']['runtime_s'] + j['config']['cooldown_s'] for j in jobs)
    report = {'status': 'checking', 'queue': label, 'machine': machine_info(), 'native_commands': {},
              'rounds_per_scheduler': ROUNDS_PER_BATCH, 'batch_count': len(jobs), 'attempt_count': len(jobs) * attempts_per_batch,
              'cooldown_s': jobs[0]['config']['cooldown_s'], 'batch_rest_s': batch_rest_s,
              'minimum_time_s': attempts_per_batch * workload_seconds + (len(jobs) - 1) * batch_rest_s,
              'frozen_files': {}}
    frozen = report['frozen_files']
    for program in (Path(__file__).resolve(), *extra_files):
        frozen[str(program)] = digest(program)
    for package in ('Compile/compilebench_runner', 'Schbench/schbench_runner', 'Mixbench/mixbench_runner'):
        for file in (BENCH / package).glob('*.py'):
            frozen[str(file)] = digest(file)
    for job in jobs:
        cfg = job['config']
        report['native_commands'][job['name']] = commands(cfg, directory / 'results/BATCH/run-NNN/POLICY')
        frozen[cfg['_config_path']] = cfg['_config_sha256']
        for side in ('front', 'back'):
            frozen[cfg[side]['program']] = cfg['_program_sha256'][side]
        frozen[cfg['front']['provenance']] = digest(cfg['front']['provenance'])
        for item in scheduler_specs(cfg).values():
            frozen[item['command'][0]] = digest(item['command'][0])
    report['free_bytes'] = shutil.disk_usage(directory).free
    if report['free_bytes'] < MIN_FREE_BYTES:
        raise RuntimeError('结果盘可用空间不足 1 GiB')
    sensor = sensor_path(jobs[0]['config']['temperature_sensor'])
    value = temperature(sensor)
    if sensor is None or value is None:
        raise RuntimeError('无法读取 CPU 温度传感器，不能核验本批冷却条件')
    report['sensor'] = str(sensor)
    report['temperature_c'] = value
    group = BuildCgroup(jobs[0]['config']['cgroup_parent'])
    try:
        if group.path is None:
            raise RuntimeError(group.reason)
        child = group.path / 'finegrain-preflight-child'
        child.mkdir()
        try:
            if not os.access(child / 'cgroup.procs', os.W_OK):
                raise RuntimeError('子 cgroup 不可写')
        finally:
            child.rmdir()
        report['cgroup'] = {'status': 'available', 'notes': group.notes}
    finally:
        group.close()
    report['status'] = 'ready'
    write_status(directory / 'preflight.json', report)
    return report


def verify_queue(report, directory, guard):
    guard.check()
    for name, expected in report['frozen_files'].items():
        if digest(name) != expected:
            raise RuntimeError('本队列冻结输入发生变化: ' + name)
    if sorted(os.sched_getaffinity(0)) != report['machine']['available_cpus']:
        raise RuntimeError('队列进程可用 CPU 范围变化')
    issue = power_error(report['machine']['power'], power_state(), report['queue'] + ' 队列')
    if issue:
        raise RuntimeError(issue)
    if shutil.disk_usage(directory).free < MIN_FREE_BYTES:
        raise RuntimeError('结果盘可用空间不足 1 GiB')


def scheduler_preflight(cfg, directory, interrupts):
    directory.mkdir()
    controller = BatchScheduler(scheduler_specs(cfg), directory, interrupts,
        timeout=cfg['scheduler_timeout_s'], settle=cfg['scheduler_settle_s'], workload_name='mixtest')
    selected = []
    try:
        controller.open()
        for policy in POLICIES[1:]:
            controller.select(policy, directory / (policy + '.log'))
            selected.append(policy)
            issue = controller.stop()
            if issue:
                raise RuntimeError(issue)
    finally:
        controller.close()
        write_status(directory / 'status.json', {'selected': selected, 'controller': controller.info})


def run_queue(jobs, directory, authorization, guard, report, interrupts):
    batch_rest_s = report['batch_rest_s']
    status = {'status': 'running', 'started_utc': stamp(), 'inhibition': guard.info,
              'jobs': [{'name': j['name'], 'command': j['command'], 'config_file': j['config_file'],
                        'status': 'pending'} for j in jobs]}
    path = directory / 'status.json'
    write_status(path, status)
    terminal = os.dup(1)
    try:
        for index, (job, record) in enumerate(zip(jobs, status['jobs'])):
            if index:
                status['phase'] = 'batch_cooldown'
                status['batch_rest_s'] = batch_rest_s
                write_status(path, status)
                deadline = time.monotonic() + batch_rest_s
                while time.monotonic() < deadline:
                    interrupts.check()
                    time.sleep(min(1, max(0, deadline - time.monotonic())))
            interrupts.check()
            authorization.check()
            verify_queue(report, directory, guard)
            record.update(status='running', started_utc=stamp(), log='logs/' + job['name'] + '.log')
            status['phase'] = 'testing'
            write_status(path, status)
            before = batches(Path(job['config']['results_dir']))
            code = None
            def show_round(run, rounds):
                message = (f"批次 {index + 1}/{len(jobs)} {job['name']} · "
                           f"第 {run}/{rounds} 轮完成（总轮次 {index * rounds + run}/{len(jobs) * rounds}）\n")
                try:
                    os.write(terminal, message.encode())
                except OSError:
                    pass  # An unavailable terminal must not interrupt the experiment.

            try:
                with batch_log(directory / record['log']):
                    code = mixed.experiment(job['config'], interrupts=interrupts, rounds=ROUNDS_PER_BATCH, on_round_complete=show_round)
            finally:
                created = sorted(batches(Path(job['config']['results_dir'])) - before)
                record.update(exit_code=code, finished_utc=stamp(),
                              result_directories=[str(p) for p in created], status='failed')
                write_status(path, status)
            if code == 130:
                record['status'] = 'interrupted'
                raise KeyboardInterrupt
            metadata = json.loads((created[0] / 'metadata.json').read_text()) if len(created) == 1 else {}
            if not can_continue(code, metadata):
                raise RuntimeError(job['name'] + ' 未完整完成或调度器收尾异常；见本批日志')
            record['status'] = 'completed' if code == 0 else 'completed_with_invalid_results'
            write_status(path, status)
        status['status'] = 'completed' if all(r['exit_code'] == 0 for r in status['jobs']) else 'completed_with_invalid_results'
        return 0 if status['status'] == 'completed' else 1
    except KeyboardInterrupt:
        status.update(status='interrupted', error='用户中断')
        return 130
    except Exception as exc:
        status.update(status='failed', error=str(exc))
        print(str(exc), file=sys.stderr)
        return 2
    finally:
        os.close(terminal)
        status['finished_utc'] = stamp()
        write_status(path, status)
