"""Scoped timer precision for the owned replay processes on Windows."""

from contextlib import contextmanager
import ctypes
import logging
import sys


logger = logging.getLogger(__name__)


@contextmanager
def replay_timer_resolution():
    """Request 1ms waits while replay runs, matching every accepted request.

    Windows 10 2004 and later scope this request to the calling process.
    Windows may still coalesce waits; this is not a real-time guarantee.
    Other platforms keep their normal event-loop timers.
    """
    status = {
        "resolution_request_ms": None,
        "resolution_request_status": "not_applicable",
    }
    if sys.platform != "win32":
        yield status
        return

    status["resolution_request_ms"] = 1
    try:
        winmm = ctypes.WinDLL("winmm")
        begin, end = winmm.timeBeginPeriod, winmm.timeEndPeriod
        for function in (begin, end):
            function.argtypes = [ctypes.c_uint]
            function.restype = ctypes.c_uint
        result = begin(1)
    except OSError as exc:
        status["resolution_request_status"] = "unavailable"
        logger.warning("Replay timer precision request unavailable: %s", exc)
        yield status
        return

    if result != 0:
        status["resolution_request_status"] = "rejected"
        status["resolution_request_return_code"] = result
        logger.warning("Replay timer precision request rejected: %s", result)
        yield status
        return

    status["resolution_request_status"] = "accepted"
    try:
        yield status
    finally:
        result = end(1)
        if result != 0:
            logger.warning("Replay timer precision cleanup returned: %s", result)
