"""One authorization for a complete randomized batch; compilation stays unprivileged."""
import json
import os
from pathlib import Path
import select
import shutil
import subprocess
import sys
import threading
import time

from .policy import PolicyError, Scheduler

POLICIES = ("default", "scx_lavd", "scx_bpfland", "scx_rusty")


class ControlError(PolicyError):
    pass


class BatchScheduler(Scheduler):
    def __init__(self, specs, directory, interrupts, timeout=30, settle=5, prefix=None, inhibit=True,
                 workload_name="compiletest"):
        super().__init__()
        self.specs, self.directory, self.interrupts = specs, Path(directory), interrupts
        self.timeout, self.settle = timeout, settle
        self.prefix = ["sudo", "-n"] if prefix is None else prefix
        self.inhibit = inhibit
        self.workload_name = workload_name
        self.process = self.log = None
        self.active = False
        self.offset = 0
        self.attempt_log = None
        self.buffer = b""
        self.serial = 0
        self.lock = threading.Lock()
        self.info = {"status": "not_started", "startup_transitions": []}

    def open(self):
        if self.process is not None or self.info["status"] != "not_started":
            raise ControlError("监督进程已初始化；拒绝重复授权")
        if self.initial()["state"] != "disabled":
            raise PolicyError("发现已有外部 scx；多轮模式不会停止它，请先自行停止")
        commands = [self.specs[name]["command"] for name in POLICIES[1:]]
        worker = str(Path(__file__).with_name("scheduler_worker.py").resolve())
        command = [sys.executable, "-I", worker, json.dumps(commands)]
        if self.inhibit:
            executable = shutil.which("systemd-inhibit")
            if not executable:
                raise ControlError("多轮测试需要 systemd-inhibit 防止自动挂起；Fedora 请安装 systemd")
            guard = [executable, "--no-ask-password", "--what=idle:sleep", "--mode=block",
                     f"--who={self.workload_name}",
                     "--why=" + {"compiletest": "kernel compilation benchmark in progress",
                                  "schtest": "schbench benchmark in progress"}.get(
                                      self.workload_name, "hackbench benchmark in progress"), "--"]
            command = [*guard, *command]
            self.info["sleep_inhibition"] = {"what": "idle:sleep", "mode": "block",
                                             "status": "requested", "command": guard}
        command = [*self.prefix, *command]
        self.info.update(command=command, log=str(self.directory / "scheduler-controller.log"))
        # Both calls have the same Python parent and controlling terminal.
        # After this worker starts, no more sudo invocations are made.
        if self.prefix:
            subprocess.run([self.prefix[0], "-v"], check=True)
        self.log = (self.directory / "scheduler-controller.log").open("ab", buffering=0)
        with self.interrupts.defer():
            self.process = subprocess.Popen(command, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=self.log)
        ready = self._receive(time.monotonic() + 15)
        if not ready.get("ready"):
            raise ControlError("监督进程初始化失败")
        self.info.update(status="ready", supervisor=ready)
        if self.inhibit:
            # systemd-inhibit starts the worker only after acquiring the lock.
            self.info["sleep_inhibition"]["status"] = "held"
        self.interrupts.check()

    def _receive(self, deadline):
        while b"\n" not in self.buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([self.process.stdout], [], [], max(0, remaining))[0]:
                raise ControlError("调度器监督进程响应超时；见 scheduler-controller.log")
            data = os.read(self.process.stdout.fileno(), 65536)
            if not data:
                raise ControlError("调度器监督进程退出；批次内不会重新调用 sudo")
            self.buffer += data
        line, self.buffer = self.buffer.split(b"\n", 1)
        return json.loads(line)

    def call(self, action, **parameters):
        with self.lock:
            if self.process is None or self.process.poll() is not None:
                raise ControlError("已授权监督进程不可用；不会中途重新提权")
            self.serial += 1
            try:
                self.process.stdin.write((json.dumps({"id": self.serial, "action": action, **parameters}) + "\n").encode())
                self.process.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise ControlError("监督进程控制管道已关闭") from exc
            deadline = time.monotonic() + 15
            while True:
                reply = self._receive(deadline)
                if reply.get("id") == self.serial:
                    break
            if not reply.get("ok"):
                raise ControlError(f"监督进程 {action} 失败: {reply.get('error')}")
            return reply

    def verify(self, expected, pid=None):
        super().verify(expected, pid)
        if self.process is not None and self.info["status"] != "closed":
            status = self.call("status")
            if status["running"] != self.active:
                raise PolicyError(f"调度器进程状态变化，退出码 {status['exit_code']}")

    def select(self, policy, logfile):
        if self.initial()["state"] != "disabled" or self.active:
            raise PolicyError("切换前发现活动 scx；拒绝覆盖或停止外部调度器")
        if policy != "default":
            self.attempt_log = Path(logfile)
            self.attempt_log.touch()
            self.offset = self.log.tell()
            with self.interrupts.defer():
                self.active = True
                self.call("start", index=POLICIES[1:].index(policy))
        deadline = time.monotonic() + self.timeout
        candidate, stable_since = None, None
        history = []
        while time.monotonic() < deadline:
            self.interrupts.check()
            status = self.call("status")
            if status["running"] != self.active:
                raise PolicyError(f"调度器加载失败，退出码 {status['exit_code']}；见 scheduler.log")
            state = self.snapshot()
            if not history or state != history[-1]["state"]:
                history.append({"monotonic_s": time.monotonic(), "state": state})
                self.info["startup_transitions"].append({"policy": policy, **history[-1]})
            wanted = (state["state"] == "disabled" if policy == "default" else
                      state["state"] == "enabled" and state["ops"] == self.specs[policy]["ops"] and state["switch_all"] == "1")
            if state["state"] == "enabled" and (policy == "default" or state["ops"] != self.specs[policy]["ops"]):
                raise PolicyError(f"调度器名称不匹配: {state['ops']!r}")
            if not wanted or candidate != state:
                candidate, stable_since = state if wanted else None, time.monotonic() if wanted else None
            if wanted and time.monotonic() - stable_since >= self.settle:
                self.verify(state, os.getpid())
                return state
            time.sleep(0.05)
        raise PolicyError("调度器未在时限内稳定；见 scheduler.log 和启动状态记录")

    def stop(self):
        with self.interrupts.defer():
            unexpected = None
            try:
                if self.interrupts.requested:
                    self._close_worker()
                elif self.active:
                    try:
                        reply = self.call("stop")
                        if reply["unexpected_exit"]:
                            unexpected = f"调度器提前退出，退出码 {reply['exit_code']}"
                    except ControlError:
                        # Terminal Ctrl+C also reaches sudo and the worker.
                        # Wait until its own scheduler has been cleaned up.
                        if not self.interrupts.requested:
                            raise
                        self._close_worker()
                deadline = time.monotonic() + 5
                while True:
                    state = self.snapshot()
                    if state["state"] == "disabled" and state["ops"] is None and state["switch_all"] == "0":
                        break
                    if time.monotonic() >= deadline:
                        raise ControlError("停止后未恢复 disabled；不会终止其他调度器")
                    time.sleep(0.05)
                self.active = False
            finally:
                if self.attempt_log is not None:
                    with (self.directory / "scheduler-controller.log").open("rb") as source:
                        source.seek(self.offset)
                        self.attempt_log.write_bytes(source.read())
                    self.attempt_log = None
            return unexpected

    def _close_worker(self):
        if self.process is None or self.info["status"] == "closed":
            return
        try:
            self.process.stdin.close()  # EOF cleans up even if the caller failed.
        except BrokenPipeError:
            pass
        code = self.process.wait(timeout=15)
        self.info.update(status="closed", exit_code=code)
        if self.inhibit:
            guard = self.info["sleep_inhibition"]
            guard["status"] = "released" if guard["status"] == "held" else "not_acquired"
        self.process.stdout.close()
        if code:
            raise ControlError(f"监督进程退出码 {code}；见 scheduler-controller.log")

    def close(self):
        with self.interrupts.defer():
            try:
                self._close_worker()
            finally:
                if self.log is not None:
                    self.log.close()
                    self.log = None
            if self.process is not None and self.snapshot()["state"] != "disabled":
                raise ControlError("监督进程退出后 sched_ext 未恢复 disabled")
