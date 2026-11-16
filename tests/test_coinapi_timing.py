"""Temporary Windows timer requests are always paired and never assumed."""

import asyncio
from types import SimpleNamespace

import pytest

from examples.coinapi_replay import __main__ as launcher
from examples.coinapi_replay import timing


def install_timer(monkeypatch, *, begin_result=0, platform="win32"):
    calls = []

    def begin(period):
        calls.append(("begin", period))
        return begin_result

    def end(period):
        calls.append(("end", period))
        return 0

    def load(name):
        calls.append(("load", name))
        return SimpleNamespace(timeBeginPeriod=begin, timeEndPeriod=end)

    monkeypatch.setattr(timing, "sys", SimpleNamespace(platform=platform))
    monkeypatch.setattr(timing.ctypes, "WinDLL", load, raising=False)
    return calls


@pytest.mark.parametrize("raises", [False, True])
def test_accepted_timer_request_is_released_even_on_exception(monkeypatch, raises):
    calls = install_timer(monkeypatch)
    try:
        with timing.replay_timer_resolution() as status:
            assert status == {
                "resolution_request_ms": 1,
                "resolution_request_status": "accepted",
            }
            assert calls == [("load", "winmm"), ("begin", 1)]
            if raises:
                raise ValueError("stop replay")
    except ValueError:
        assert raises
    assert calls == [("load", "winmm"), ("begin", 1), ("end", 1)]


def test_failed_request_is_reported_without_unmatched_cleanup(monkeypatch, caplog):
    calls = install_timer(monkeypatch, begin_result=97)
    with timing.replay_timer_resolution() as status:
        assert status["resolution_request_status"] == "rejected"
        assert status["resolution_request_return_code"] == 97
    assert calls == [("load", "winmm"), ("begin", 1)]
    assert "rejected" in caplog.text


def test_other_platforms_do_not_load_windows_libraries(monkeypatch):
    calls = install_timer(monkeypatch, platform="linux")
    with timing.replay_timer_resolution() as status:
        assert status["resolution_request_ms"] is None
        assert status["resolution_request_status"] == "not_applicable"
    assert calls == []


def test_missing_timer_library_is_reported_without_blocking_replay(monkeypatch, caplog):
    install_timer(monkeypatch)

    def unavailable(_name):
        raise OSError("library unavailable")

    monkeypatch.setattr(timing.ctypes, "WinDLL", unavailable)
    with timing.replay_timer_resolution() as status:
        assert status["resolution_request_status"] == "unavailable"
    assert "unavailable" in caplog.text


@pytest.mark.asyncio
async def test_public_async_launch_releases_request_when_cancelled(monkeypatch):
    calls = install_timer(monkeypatch)
    entered = asyncio.Event()

    async def run(_args, status):
        assert status["resolution_request_status"] == "accepted"
        entered.set()
        await asyncio.Future()

    monkeypatch.setattr(launcher, "_launch", run)
    task = asyncio.create_task(launcher.launch(object()))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls == [("load", "winmm"), ("begin", 1), ("end", 1)]
