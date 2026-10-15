"""The shipped FIX acceptor configuration must need no file persistence."""

from pathlib import Path
from types import SimpleNamespace

from src.mockx.gateway import FIXGateway
import src.mockx.gateway as gateway_module


def test_required_fix_acceptor_starts_with_memory_store_and_screen_log(monkeypatch):
    config = Path(__file__).resolve().parents[1] / "configs" / "mockx1.cfg"
    assert "FileLogPath" not in config.read_text(encoding="utf-8")
    factories = []

    class Settings:
        def __init__(self, path):
            assert Path(path) == config

        def get(self, *_args):
            return SimpleNamespace(getString=lambda key: {
                "SenderCompID": "MOCKX1", "TargetCompID": "CLIENT1", "SocketAcceptPort": "4001",
            }[key])

    def memory_store():
        factories.append("memory")
        return "memory-store"

    def screen_log(settings):
        assert isinstance(settings, Settings)
        factories.append("screen")
        return "screen-log"

    def file_log(_settings):
        raise RuntimeError("Configuration failed: FileLogPath not defined")

    class Acceptor:
        def __init__(self, application, store, settings, log):
            assert application.loop is not None
            assert (store, log) == ("memory-store", "screen-log")
            assert isinstance(settings, Settings)
            self.running = False

        def start(self):
            self.running = True

        def stop(self):
            self.running = False

    monkeypatch.setattr(gateway_module, "fix", SimpleNamespace(
        SessionSettings=Settings, MemoryStoreFactory=memory_store,
        ScreenLogFactory=screen_log, FileLogFactory=file_log, SocketAcceptor=Acceptor,
        SessionID=lambda *parts: parts,
    ))
    gateway = FIXGateway(
        str(config), None, None, None, application=SimpleNamespace(loop=object()),
        session_participants={"FIX.4.4:MOCKX1->CLIENT1": "participant-client"},
    )
    gateway.start()
    try:
        assert gateway.started and gateway.fix_acceptor.running
        assert gateway.accept_port == "4001"
        gateway.start()
        assert factories == ["memory", "screen"]
    finally:
        gateway.stop()
    assert not gateway.started and not gateway.fix_acceptor.running
