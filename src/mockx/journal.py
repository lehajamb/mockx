"""One typed QuestDB journal of completed native exchange commands."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Mapping, Sequence

from .effects import BookCheckpoint, CommandEffect, CommandEffects
from .metrics import (
    BufferedMetricsWriter,
    MetricRecord,
    PersistenceAddOutcome,
    datetime_to_ns,
)
from .questdb_writer import QUESTDB_SCHEMAS


TABLE = "exchange_events"
_TYPES = dict(QUESTDB_SCHEMAS[0].columns)
_IDENTITY = {"timestamp", "run_id", "event_sequence", "event_timestamp_ns"}


class JournalAdmissionError(RuntimeError):
    """The validated command could not be retained by the bounded writer."""

    def __init__(
        self, outcome: PersistenceAddOutcome, *, run_id: str, event_sequence: int
    ) -> None:
        self.outcome = outcome
        self.run_id = run_id
        self.event_sequence = event_sequence
        self.code = (
            "PERSISTENCE_BACKPRESSURE_TIMEOUT"
            if outcome == PersistenceAddOutcome.TIMED_OUT
            else "METRICS_INCOMPLETE"
        )
        super().__init__(
            f"Native journal admission {outcome.value} for {run_id} at event {event_sequence}"
        )


class JournalFinalizationError(RuntimeError):
    """A terminal marker was admitted but its acknowledgment is uncertain."""

    code = "JOURNAL_END_UNCONFIRMED"

    def __init__(self, run_id: str, event_sequence: int, error: str | None) -> None:
        self.run_id = run_id
        self.event_sequence = event_sequence
        super().__init__(
            f"Native terminal marker acknowledgment is unconfirmed: {error}"
        )


@dataclass(slots=True)
class _RunScope:
    label: str
    sequence: int = 0
    command_sequence: int = 0
    checkpoint_sequence: int = 0
    failed: bool = False


def _scalar(value: object) -> object:
    return value.value if isinstance(value, Enum) else value


def _payload_fields(payload: object) -> dict[str, object]:
    """Select typed native values, excluding formatting and mutable objects."""
    if isinstance(payload, Mapping):
        source = dict(payload)
    else:
        source = {
            name: getattr(payload, name) for name in _TYPES if hasattr(payload, name)
        }
    fields = {
        name: _scalar(value)
        for name, value in source.items()
        if name in _TYPES and name not in _IDENTITY and value is not None
    }
    aliases = {
        "order_price_ticks": "price_ticks",
        "id": "trade_id",
        "executed_qty_lots": "qty_lots",
        "execution_price_ticks": "price_ticks",
        "reject_reason": "reason_code",
        "metric": "metric_name",
        "report_exec_id": "exec_id",
        "cancel_cl_ord_id": "cl_ord_id",
        "replace_cl_ord_id": "cl_ord_id",
        "stage": "metric_name",
        "reason": "reason_code",
        "report_id": "exec_id",
        "reject_id": "exec_id",
    }
    for original, canonical in aliases.items():
        value = (
            source.get(original)
            if isinstance(payload, Mapping)
            else getattr(payload, original, None)
        )
        if value is not None:
            fields[canonical] = _scalar(value)
    submitted = (
        source.get("submitted_at")
        if isinstance(payload, Mapping)
        else getattr(payload, "submitted_at", None)
    )
    if isinstance(submitted, datetime):
        fields["submitted_at_ns"] = datetime_to_ns(submitted)
    return fields


def encode_effect(effect: CommandEffect) -> tuple[str, dict[str, object]]:
    payload = effect.payload
    fields = _payload_fields(payload)
    names = {
        "DisplayedOrderAdded": "BOOK_ADDED",
        "DisplayedOrderExecuted": "BOOK_EXECUTED",
        "DisplayedOrderDeleted": "BOOK_DELETED",
        "DisplayedOrderReduced": "BOOK_REDUCED",
        "Trade": "TRADE",
        "SelfTradePrevention": "SELF_TRADE_PREVENTED",
        "CancelReject": "CANCEL_REJECTED",
    }
    if type(payload).__name__ == "ExecutionReport":
        kind = (
            "REPORT_DELIVERED"
            if effect.kind.lower() in {"report_delivered", "delivery"}
            else "REPORT_CREATED"
        )
        fields["report_created_at_ns"] = datetime_to_ns(payload.timestamp)
    else:
        kind = names.get(type(payload).__name__, effect.kind.upper())
    if kind == "CANCEL_REJECTED" and getattr(payload, "response_to", "1") == "2":
        kind = "REPLACE_REJECTED"
    if kind == "BOOK_ADDED":
        fields.setdefault("logical_order_id", fields["order_id"])
        fields.setdefault("liquidity_category", "market")
    if type(payload).__name__ == "LatencyMeasurement":
        # CPU observations stay operational; simulated measurements are exact ns.
        if getattr(payload, "unit", None) != "ms":
            raise ValueError("CPU samples are not durable journal effects")
        fields["duration_ns"] = round(payload.value * 1_000_000)
        kind = "LATENCY"
    return kind, fields


class ExchangeJournal:
    """Owner-loop sequencing and grouped admission, independent of transport."""

    def __init__(self, writer: BufferedMetricsWriter, venue_id: str) -> None:
        if not venue_id:
            raise ValueError("Native journal requires a venue identity")
        self.writer = writer
        self.venue_id = venue_id
        self._runs: dict[str, _RunScope] = {}
        self.active_run_id: str | None = None

    @property
    def run_id(self) -> str | None:
        return self.active_run_id

    @property
    def sequence(self) -> int:
        return self._runs[self.active_run_id].sequence if self.active_run_id else 0

    def _record(
        self, run_id: str, kind: str, timestamp: datetime, fields: Mapping[str, object]
    ) -> MetricRecord:
        scope = self._runs[run_id]
        scope.sequence += 1
        symbols: dict[str, object] = {"venue_id": self.venue_id, "kind": kind}
        columns: dict[str, object] = {}
        for name, value in fields.items():
            if value is None:
                continue
            if name not in _TYPES or name in _IDENTITY:
                raise ValueError(f"Unknown native journal field {name}")
            value = _scalar(value)
            if _TYPES[name] == "SYMBOL":
                symbols[name] = value
            elif _TYPES[name] == "LONG":
                if (
                    not isinstance(value, int)
                    or isinstance(value, bool)
                    or not -(1 << 63) < value < (1 << 63)
                ):
                    raise ValueError(f"Native journal {name} must fit a QuestDB LONG")
                columns[name] = value
            else:
                columns[name] = str(value)
        return MetricRecord.create(
            TABLE,
            symbols,
            columns,
            run_id=run_id,
            event_sequence=scope.sequence,
            event_timestamp_ns=datetime_to_ns(timestamp),
        )

    async def _admit(self, records: Sequence[MetricRecord]) -> None:
        try:
            outcome = await self.writer.add_group(records)
        except Exception as exc:
            first = records[0]
            self._runs[first.run_id].failed = True
            raise JournalAdmissionError(
                PersistenceAddOutcome.FAILED,
                run_id=first.run_id,
                event_sequence=first.event_sequence,
            ) from exc
        if outcome not in {
            PersistenceAddOutcome.ACCEPTED,
            PersistenceAddOutcome.ACCEPTED_FLUSH_REQUIRED,
        }:
            first = records[0]
            self._runs[first.run_id].failed = True
            raise JournalAdmissionError(
                outcome, run_id=first.run_id, event_sequence=first.event_sequence
            )

    async def start_run(
        self,
        run_id: str,
        label: str,
        instruments,
        timestamp: datetime,
        metadata: Mapping[str, object] | None = None,
    ) -> int:
        if not run_id or run_id in self._runs or self.active_run_id is not None:
            raise ValueError("Native journal run must have a fresh identity")
        self._runs[run_id] = _RunScope(label)
        self.active_run_id = run_id
        effects = [
            CommandEffect(
                "RUN_STARTED",
                {
                    "run_label": label,
                    "metadata_json": json.dumps(
                        metadata or {}, separators=(",", ":"), default=str
                    ),
                },
            )
        ]
        for item in instruments:
            instrument = instruments.require(item) if isinstance(item, str) else item
            effects.append(
                CommandEffect(
                    "INSTRUMENT",
                    {
                        "symbol": instrument.symbol,
                        "tick_size": str(instrument.tick_size),
                        "lot_size": str(instrument.lot_size),
                        "price_display_precision": instrument.price_display_precision,
                        "quantity_display_precision": instrument.quantity_display_precision,
                    },
                )
            )
        return await self.admit_command(
            CommandEffects("run_start", timestamp, tuple(effects), run_id=run_id)
        )

    async def admit_command(self, effects: CommandEffects) -> int:
        run_id = effects.run_id or self.active_run_id
        if run_id is None or run_id not in self._runs:
            raise ValueError("Journal command requires a known originating run")
        scope = self._runs[run_id]
        scope.command_sequence += 1
        base = {
            "command_type": effects.command_type,
            "command_sequence": scope.command_sequence,
            "ingress_sequence": effects.ingress_sequence,
            "order_id": effects.order_id,
        }
        records: list[MetricRecord] = []
        for ordinal, effect in enumerate(effects.effects):
            kind, fields = encode_effect(effect)
            records.append(
                self._record(
                    run_id,
                    kind,
                    effects.timestamp,
                    {**base, **fields, "ordinal": ordinal},
                )
            )
        records.append(
            self._record(
                run_id,
                "COMMAND_COMPLETE",
                effects.timestamp,
                {**base, "ordinal": len(records), "event_count": len(records)},
            )
        )
        await self._admit(tuple(records))
        return records[-1].event_sequence

    async def checkpoint(self, snapshot: BookCheckpoint, reason: str = "manual") -> int:
        scope = self._runs[snapshot.run_id]
        scope.checkpoint_sequence += 1
        cut = scope.sequence
        base = {
            "checkpoint_id": scope.checkpoint_sequence,
            "cut_sequence": cut,
            "next_itch_sequence": snapshot.next_itch_sequence,
            "order_count": len(snapshot.orders),
            "text": reason,
        }
        chunk: list[MetricRecord] = []
        chunk_bytes = 0

        async def add(record: MetricRecord) -> None:
            nonlocal chunk, chunk_bytes
            if chunk and (
                len(chunk) >= self.writer.flush_batch_size
                or chunk_bytes + record.encoded_size_bytes
                > self.writer.flush_batch_bytes
            ):
                await self._admit(tuple(chunk))
                chunk, chunk_bytes = [], 0
            chunk.append(record)
            chunk_bytes += record.encoded_size_bytes

        await add(
            self._record(
                snapshot.run_id, "CHECKPOINT_STARTED", snapshot.timestamp, base
            )
        )
        for position, order in enumerate(snapshot.orders):
            await add(
                self._record(
                    snapshot.run_id,
                    "CHECKPOINT_ORDER",
                    snapshot.timestamp,
                    {
                        **base,
                        "order_id": order.order_id,
                        "logical_order_id": order.logical_order_id or order.order_id,
                        "symbol": order.symbol,
                        "side": _scalar(order.side),
                        "price_ticks": order.price_ticks,
                        "qty_lots": order.qty_lots,
                        "liquidity_category": _scalar(order.liquidity_category),
                        "fifo_position": position,
                    },
                )
            )
        await add(
            self._record(
                snapshot.run_id, "CHECKPOINT_COMPLETE", snapshot.timestamp, base
            )
        )
        await self._admit(tuple(chunk))
        return scope.sequence

    async def end_run(
        self, timestamp: datetime, *, failed: bool = False, reason: str | None = None
    ):
        if self.active_run_id is None:
            raise ValueError("No active journal run")
        prefix = await self.writer.flush()
        if not prefix.successful:
            raise RuntimeError(
                f"Native journal prefix acknowledgment failed: {prefix.error}"
            )
        await self.admit_command(
            CommandEffects(
                "run_end",
                timestamp,
                (
                    CommandEffect(
                        "RUN_FAILED" if failed else "RUN_ENDED", {"text": reason}
                    ),
                ),
            )
        )
        result = await self.writer.flush()
        if not result.successful:
            run_id = self.active_run_id
            self._runs[run_id].failed = True
            raise JournalFinalizationError(
                run_id, self._runs[run_id].sequence, result.error
            )
        return result

    def detach_run(self) -> None:
        """Keep original scopes for report delivery that outlives a run reset."""
        self.active_run_id = None
