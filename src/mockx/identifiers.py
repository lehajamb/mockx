"""Generation-aware public exchange identifier allocation."""

from __future__ import annotations


class RunIdentifierScope:
    """Allocate deterministic public IDs within one simulation generation."""

    def __init__(self, generation: int) -> None:
        if generation < 0:
            raise ValueError("Run generation cannot be negative")
        self.generation = generation
        self._next_order = 1
        self._next_exec = 1
        self._next_trade = 1

    def next_order_id(self) -> str:
        value = self._format("O", self._next_order)
        self._next_order += 1
        return value

    def next_exec_id(self) -> str:
        value = self._format("E", self._next_exec)
        self._next_exec += 1
        return value

    def next_trade_id(self) -> str:
        value = self._format("T", self._next_trade)
        self._next_trade += 1
        return value

    def _format(self, kind: str, counter: int) -> str:
        return f"G{self.generation:08d}-{kind}{counter:08d}"
