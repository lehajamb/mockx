"""Child-process entry point; all exchange behavior remains in MockExchange."""

import argparse
import logging
import sys
import threading

from src.mockx.configuration import load_config
from src.mockx.main import MockExchange
from .timing import replay_timer_resolution


def watch_parent(exchange, stream, finished):
    """An EOF means the owning launcher disappeared; never orphan its server."""
    if stream.readline().strip() not in {"", "stop"}:
        return
    while not finished.is_set():
        if exchange.server is not None:
            exchange.stop()
            return
        # A stop received during construction must not vanish before Uvicorn
        # exists. This daemon stops waiting when startup fails or run exits.
        finished.wait(0.01)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--fix-config", required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    exchange = MockExchange(load_config(args.config, environ={}), args.fix_config)
    finished = threading.Event()
    threading.Thread(
        target=lambda: watch_parent(exchange, sys.stdin, finished), daemon=True
    ).start()
    try:
        with replay_timer_resolution():
            exchange.run()
    finally:
        finished.set()


if __name__ == "__main__":
    main()
