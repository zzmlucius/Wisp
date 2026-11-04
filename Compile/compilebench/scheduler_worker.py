"""Privileged worker: only run commands registered before the batch starts."""
import json
import os
import select
import signal
import subprocess
import sys


def stop_child(child):
    if child is None or child.poll() is not None:
        return
    for sig, timeout in ((signal.SIGINT, 5), (signal.SIGTERM, 2), (signal.SIGKILL, 2)):
        try:
            os.killpg(child.pid, sig)
        except ProcessLookupError:
            break
        try:
            child.wait(timeout=timeout)
            return
        except subprocess.TimeoutExpired:
            pass
    child.wait()


def serve(commands):
    if not isinstance(commands, list) or not commands or any(
            not isinstance(command, list) or not command or
            any(not isinstance(arg, str) or not arg for arg in command) for command in commands):
        raise ValueError("invalid registered commands")
    child = None
    stopping = False
    buffer = b""

    def cancel(signum, frame):
        nonlocal stopping
        stopping = True

    def send(message):
        print(json.dumps(message), flush=True)

    signal.signal(signal.SIGINT, cancel)
    signal.signal(signal.SIGTERM, cancel)
    try:
        send({"ready": True, "pid": os.getpid(), "euid": os.geteuid()})
        while not stopping:
            if b"\n" not in buffer:
                if not select.select([sys.stdin], [], [], 0.2)[0]:
                    continue
                data = os.read(sys.stdin.fileno(), 65536)
                if not data:
                    break
                buffer += data
                if len(buffer) > 1024 * 1024:
                    raise ValueError("oversized request")
                if b"\n" not in buffer:
                    continue
            line, buffer = buffer.split(b"\n", 1)
            request = json.loads(line)
            reply = {"id": request["id"], "ok": True}
            try:
                action = request.get("action")
                if action == "start":
                    index = request.get("index")
                    if type(index) is not int or not 0 <= index < len(commands):
                        raise ValueError("unregistered command")
                    if child is not None:
                        raise ValueError("previous scheduler has not been stopped")
                    child = subprocess.Popen(commands[index], stdin=subprocess.DEVNULL,
                        stdout=sys.stderr, stderr=sys.stderr, start_new_session=True)
                    reply["pid"] = child.pid
                elif action == "status":
                    code = child.poll() if child else None
                    reply.update(running=child is not None and code is None, exit_code=code)
                elif action == "stop":
                    unexpected = child is not None and child.poll() is not None
                    stop_child(child)
                    reply.update(unexpected_exit=unexpected, exit_code=child.returncode if child else None)
                    child = None
                else:
                    raise ValueError("unknown action")
            except Exception as exc:
                reply.update(ok=False, error=str(exc))
            send(reply)
    finally:
        stop_child(child)


if __name__ == "__main__":
    serve(json.loads(sys.argv[1]))
