"""Run control: one total deadline and a cancel switch for the investigation in progress.

Every model call asks `time_left()` for its own timeout and calls `check()` first, so no run can outlive its
deadline by more than one network round trip, and Cancel takes effect at the next step.
"""
import threading
import time

_deadline = None
_cancel = threading.Event()


class RunStopped(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason  # "timed_out" or "cancelled"


def start(seconds):
    """Arm the deadline for a new run (None = no deadline) and clear any earlier cancel."""
    global _deadline
    _deadline = None if seconds is None else time.time() + float(seconds)
    _cancel.clear()


def cancel():
    _cancel.set()


def cancelled():
    return _cancel.is_set()


def time_left():
    return None if _deadline is None else max(0.0, _deadline - time.time())


def check(min_seconds=0.0):
    """Raise RunStopped if the run was cancelled or has (almost) no time left."""
    if _cancel.is_set():
        raise RunStopped("cancelled")
    left = time_left()
    if left is not None and left <= min_seconds:
        raise RunStopped("timed_out")
