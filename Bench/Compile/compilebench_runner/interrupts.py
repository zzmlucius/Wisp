"""Defer cancellation through ownership transfers, cleanup and result persistence."""
from contextlib import contextmanager
import signal


class Interrupts:
    def __init__(self):
        self.requested = False
        self.depth = 0
        self.previous = {}

    def handle(self, signum, frame):
        self.requested = True
        if self.depth == 0:
            raise KeyboardInterrupt

    def install(self):
        for sig in (signal.SIGINT, signal.SIGTERM):
            self.previous[sig] = signal.signal(sig, self.handle)

    def restore(self):
        for sig, handler in self.previous.items():
            signal.signal(sig, handler)

    @contextmanager
    def defer(self):
        self.depth += 1
        try:
            yield
        finally:
            self.depth -= 1

    def check(self):
        if self.requested:
            raise KeyboardInterrupt
