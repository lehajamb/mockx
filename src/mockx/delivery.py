"""Process-lifetime reliable application delivery for outbound FIX reports."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import math
import threading
from typing import Callable, Deque, Dict, Iterable, Optional

from .models import (
    CancelReject,
    ExecutionReport,
    FixDeliveryDueCommand,
    FixDeliveryMessageType,
    FixDeliveryQueueSnapshot,
    FixDeliveryState,
    OutboundFixDelivery,
    SessionIdentity,
)


class FixDeliverySerializationError(RuntimeError):
    """A deterministic payload-to-FIX serialization failure."""


@dataclass(frozen=True, slots=True)
class _DeliveredTombstone:
    run_generation: int
    destination_session_id: str
    message_type: FixDeliveryMessageType


class OutboundFixDeliveryQueue:
    """Thread-safe owner of immutable delivery-state snapshots."""

    def __init__(
        self,
        *,
        retry_interval_ms: int = 1_000,
        max_attempts_per_cycle: int = 3,
        terminal_retention_records: int = 10_000,
        wall_clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        for name, value in (
            ("retry_interval_ms", retry_interval_ms),
            ("max_attempts_per_cycle", max_attempts_per_cycle),
            ("terminal_retention_records", terminal_retention_records),
        ):
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or value <= 0
            ):
                raise ValueError(f"{name} must be a positive integer")
        self.retry_interval_ms = retry_interval_ms
        self.max_attempts_per_cycle = max_attempts_per_cycle
        self.terminal_retention_records = terminal_retention_records
        self._wall_clock = wall_clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self._records: Dict[str, OutboundFixDelivery] = {}
        self._scheduled_due: Dict[str, FixDeliveryDueCommand] = {}
        self._next_schedule_version = 1
        self._record_order: list[str] = []
        self._domain_index: Dict[str, str] = {}
        self._session_queues: Dict[str, Deque[str]] = defaultdict(deque)
        self._next_session_sequence: Dict[str, int] = defaultdict(int)
        self._delivered_record_ids: Deque[str] = deque()
        self._delivered_tombstones: Dict[str, _DeliveredTombstone] = {}
        self._enqueued = 0
        self._attempts = 0
        self._retries = 0
        self._failures = 0
        self._delivered = 0
        self._permanently_failed = 0

    @property
    def wall_time(self) -> datetime:
        value = self._wall_clock()
        if value.tzinfo is None:
            raise ValueError("Delivery wall clock must be timezone-aware")
        return value

    def enqueue(
        self,
        *,
        delivery_id: str,
        destination: SessionIdentity,
        payload: ExecutionReport | CancelReject,
        simulated_due_timestamp: datetime,
    ) -> tuple[Optional[OutboundFixDelivery], bool]:
        """Insert one logical payload, deduplicated by its domain identity."""
        if isinstance(payload, ExecutionReport):
            message_type = FixDeliveryMessageType.EXECUTION_REPORT
            domain_id = payload.exec_id
        elif isinstance(payload, CancelReject):
            message_type = FixDeliveryMessageType.ORDER_CANCEL_REJECT
            domain_id = payload.reject_id
        else:
            raise TypeError(f"Unsupported FIX delivery payload: {type(payload)}")

        with self._lock:
            tombstone = self._delivered_tombstones.get(domain_id)
            if tombstone is not None:
                if (
                    tombstone.destination_session_id
                    != destination.session_id
                    or tombstone.message_type != message_type
                    or tombstone.run_generation != payload.run_generation
                ):
                    raise ValueError(
                        f"Conflicting payload for delivery domain ID {domain_id}"
                    )
                return None, False
            existing_id = self._domain_index.get(domain_id)
            if existing_id is not None:
                existing = self._records[existing_id]
                if (
                    existing.payload != payload
                    or existing.destination.session_id
                    != destination.session_id
                ):
                    raise ValueError(
                        f"Conflicting payload for delivery domain ID {domain_id}"
                    )
                return existing, False
            if delivery_id in self._records:
                raise ValueError(f"Duplicate FIX delivery ID {delivery_id}")

            session_id = destination.session_id
            session_sequence = self._next_session_sequence[session_id]
            self._next_session_sequence[session_id] = session_sequence + 1
            record = OutboundFixDelivery(
                delivery_id=delivery_id,
                session_sequence=session_sequence,
                run_generation=payload.run_generation,
                destination=destination,
                message_type=message_type,
                payload=payload,
                domain_id=domain_id,
                simulated_due_timestamp=simulated_due_timestamp,
                enqueued_at=self.wall_time,
            )
            self._records[delivery_id] = record
            self._record_order.append(delivery_id)
            self._domain_index[domain_id] = delivery_id
            self._session_queues[session_id].append(delivery_id)
            self._enqueued += 1
            self._arm_due_locked(delivery_id, simulated_due_timestamp)
            return record, True

    def begin_generation(self, active_generation: int) -> None:
        """Discard completed-generation tombstones after lifecycle reset."""
        with self._lock:
            self._delivered_tombstones = {
                domain_id: tombstone
                for domain_id, tombstone in self._delivered_tombstones.items()
                if tombstone.run_generation == active_generation
            }

    def get(self, delivery_id: str) -> OutboundFixDelivery:
        with self._lock:
            return self._records[delivery_id]

    def snapshot_records(self) -> tuple[OutboundFixDelivery, ...]:
        with self._lock:
            return tuple(
                self._records[delivery_id]
                for delivery_id in self._record_order
            )

    def _arm_due_locked(
        self, delivery_id: str, due_timestamp: datetime,
    ) -> FixDeliveryDueCommand:
        """Replace a deadline while holding the queue lock; versions never reset."""
        command = FixDeliveryDueCommand(
            delivery_id=delivery_id,
            schedule_version=self._next_schedule_version,
            due_timestamp=due_timestamp,
        )
        self._next_schedule_version += 1
        self._scheduled_due[delivery_id] = command
        return command

    def current_due_command(
        self, delivery_id: str,
    ) -> Optional[FixDeliveryDueCommand]:
        with self._lock:
            return self._scheduled_due.get(delivery_id)

    def mark_due(
        self,
        command: FixDeliveryDueCommand,
        *,
        simulated_timestamp: datetime,
    ) -> Optional[OutboundFixDelivery]:
        """Consume a current, elapsed deadline; obsolete events are harmless."""
        if not isinstance(command, FixDeliveryDueCommand):
            raise TypeError("FIX delivery due events require FixDeliveryDueCommand")
        if simulated_timestamp.tzinfo is None:
            raise ValueError("Delivery simulated timestamp must be timezone-aware")
        with self._lock:
            if self._scheduled_due.get(command.delivery_id) != command:
                return None
            record = self._records.get(command.delivery_id)
            if record is None or record.state not in {
                FixDeliveryState.PENDING,
                FixDeliveryState.DUE,
                FixDeliveryState.RETRY_PENDING,
            }:
                return None
            if simulated_timestamp < command.due_timestamp:
                return None
            if record.cycle_attempt_count >= self.max_attempts_per_cycle:
                return None
            if (
                record.state == FixDeliveryState.RETRY_PENDING
                and record.next_retry_timestamp != command.due_timestamp
            ):
                return None
            self._scheduled_due.pop(command.delivery_id)
            record = replace(
                record,
                state=FixDeliveryState.DUE,
                next_retry_timestamp=None,
            )
            self._records[command.delivery_id] = record
            return record

    def head_ready(self, session_id: str) -> Optional[OutboundFixDelivery]:
        with self._lock:
            queue = self._session_queues.get(session_id)
            if not queue:
                return None
            record = self._records[queue[0]]
            return record if record.state == FixDeliveryState.DUE else None

    def note_unavailable(
        self,
        delivery_id: str,
        *,
        reason: str,
    ) -> OutboundFixDelivery:
        with self._lock:
            record = self._require_due_head_locked(delivery_id)
            self._failures += 1
            self._scheduled_due.pop(delivery_id, None)
            record = replace(
                record,
                state=FixDeliveryState.RETRY_PENDING,
                last_failure_reason=reason,
                next_retry_timestamp=None,
            )
            self._records[delivery_id] = record
            return record

    def start_attempt(
        self,
        delivery_id: str,
        *,
        simulated_timestamp: datetime,
    ) -> OutboundFixDelivery:
        with self._lock:
            record = self._require_due_head_locked(delivery_id)
            if record.cycle_attempt_count >= self.max_attempts_per_cycle:
                raise ValueError("FIX delivery attempt cycle is exhausted")
            self._scheduled_due.pop(delivery_id, None)
            self._attempts += 1
            if record.attempt_count > 0:
                self._retries += 1
            record = replace(
                record,
                attempt_count=record.attempt_count + 1,
                cycle_attempt_count=record.cycle_attempt_count + 1,
                last_attempt_at=self.wall_time,
                last_attempt_simulated_at=simulated_timestamp,
                last_failure_reason=None,
            )
            self._records[delivery_id] = record
            return record

    def record_transport_failure(
        self,
        delivery_id: str,
        *,
        reason: str,
        simulated_timestamp: datetime,
    ) -> OutboundFixDelivery:
        with self._lock:
            record = self._require_due_head_locked(delivery_id)
            self._failures += 1
            next_retry = (
                simulated_timestamp
                + timedelta(milliseconds=self.retry_interval_ms)
                if record.cycle_attempt_count < self.max_attempts_per_cycle
                else None
            )
            record = replace(
                record,
                state=FixDeliveryState.RETRY_PENDING,
                last_failure_reason=reason,
                next_retry_timestamp=next_retry,
            )
            self._records[delivery_id] = record
            if next_retry is None:
                self._scheduled_due.pop(delivery_id, None)
            else:
                self._arm_due_locked(delivery_id, next_retry)
            return record

    def record_permanent_failure(
        self,
        delivery_id: str,
        *,
        reason: str,
    ) -> OutboundFixDelivery:
        with self._lock:
            record = self._records[delivery_id]
            if record.state == FixDeliveryState.PERMANENTLY_FAILED:
                return record
            record = self._require_due_head_locked(delivery_id)
            self._failures += 1
            self._permanently_failed += 1
            self._scheduled_due.pop(delivery_id, None)
            record = replace(
                record,
                state=FixDeliveryState.PERMANENTLY_FAILED,
                last_failure_reason=reason,
                next_retry_timestamp=None,
            )
            self._records[delivery_id] = record
            return record

    def record_delivered(
        self,
        delivery_id: str,
    ) -> OutboundFixDelivery:
        with self._lock:
            record = self._records[delivery_id]
            if record.state == FixDeliveryState.DELIVERED:
                return record
            if record.state == FixDeliveryState.PERMANENTLY_FAILED:
                raise ValueError(
                    f"Cannot deliver permanently failed record {delivery_id}"
                )
            record = self._require_due_head_locked(delivery_id)
            queue = self._session_queues[record.destination.session_id]
            record = replace(
                record,
                state=FixDeliveryState.DELIVERED,
                delivered_at=self.wall_time,
                last_failure_reason=None,
                next_retry_timestamp=None,
            )
            self._records[delivery_id] = record
            self._scheduled_due.pop(delivery_id, None)
            queue.popleft()
            self._delivered += 1
            self._delivered_record_ids.append(delivery_id)
            self._prune_delivered_locked()
            return record

    def _prune_delivered_locked(self) -> None:
        while (
            len(self._delivered_record_ids)
            > self.terminal_retention_records
        ):
            delivery_id = self._delivered_record_ids.popleft()
            record = self._records.get(delivery_id)
            if record is None or record.state != FixDeliveryState.DELIVERED:
                continue
            self._delivered_tombstones[record.domain_id] = (
                _DeliveredTombstone(
                    run_generation=record.run_generation,
                    destination_session_id=record.destination.session_id,
                    message_type=record.message_type,
                )
            )
            self._records.pop(delivery_id, None)
            self._scheduled_due.pop(delivery_id, None)
            self._domain_index.pop(record.domain_id, None)
            try:
                self._record_order.remove(delivery_id)
            except ValueError:
                pass

    def start_reconnect_cycle(
        self,
        session_id: str,
    ) -> Optional[OutboundFixDelivery]:
        with self._lock:
            queue = self._session_queues.get(session_id)
            if not queue:
                return None
            record = self._records[queue[0]]
            if record.state not in {
                FixDeliveryState.DUE,
                FixDeliveryState.RETRY_PENDING,
            }:
                return None
            record = replace(
                record,
                state=FixDeliveryState.DUE,
                cycle_attempt_count=0,
                next_retry_timestamp=None,
            )
            self._records[record.delivery_id] = record
            self._scheduled_due.pop(record.delivery_id, None)
            return record

    def prepare_after_scheduler_reset(
        self,
        current_simulated_time: datetime,
    ) -> tuple[tuple[FixDeliveryDueCommand, int], ...]:
        """Return nonterminal records to schedule in the new scheduler generation."""
        scheduled: list[tuple[FixDeliveryDueCommand, int]] = []
        with self._lock:
            self._scheduled_due.clear()
            for delivery_id in self._record_order:
                record = self._records[delivery_id]
                if record.state in {
                    FixDeliveryState.DELIVERED,
                    FixDeliveryState.PERMANENTLY_FAILED,
                }:
                    continue
                if record.state == FixDeliveryState.PENDING:
                    seconds = (
                        record.simulated_due_timestamp
                        - current_simulated_time
                    ).total_seconds()
                    delay_ms = max(0, math.ceil(seconds * 1_000))
                    due_timestamp = max(
                        record.simulated_due_timestamp, current_simulated_time
                    )
                else:
                    delay_ms = 0
                    due_timestamp = current_simulated_time
                    record = replace(
                        record,
                        state=FixDeliveryState.DUE,
                        cycle_attempt_count=0,
                        next_retry_timestamp=None,
                    )
                    self._records[delivery_id] = record
                command = self._arm_due_locked(delivery_id, due_timestamp)
                scheduled.append((command, delay_ms))
        return tuple(scheduled)

    def pending_order_keys(self) -> set[tuple[int, int]]:
        with self._lock:
            return {
                (record.run_generation, record.payload.order_id)
                for record in self._nonterminal_records_locked()
                if (
                    isinstance(record.payload, ExecutionReport)
                    and record.payload.order_id > 0
                )
            }

    def pending_cancel_keys(self) -> set[tuple[int, int]]:
        with self._lock:
            keys: set[tuple[int, int]] = set()
            for record in self._nonterminal_records_locked():
                payload = record.payload
                if isinstance(payload, CancelReject) and payload.response_to != "1":
                    continue
                ingress_sequence = (
                    payload.cancel_ingress_sequence
                    if isinstance(payload, ExecutionReport)
                    else payload.ingress_sequence
                )
                if ingress_sequence is not None:
                    keys.add((payload.run_generation, ingress_sequence))
            return keys

    def pending_replace_keys(self) -> set[tuple[int, int]]:
        with self._lock:
            keys: set[tuple[int, int]] = set()
            for record in self._nonterminal_records_locked():
                payload = record.payload
                if isinstance(payload, ExecutionReport):
                    sequence = payload.replace_ingress_sequence
                else:
                    sequence = payload.ingress_sequence if payload.response_to == "2" else None
                if sequence is not None:
                    keys.add((payload.run_generation, sequence))
            return keys

    def snapshot(
        self,
        *,
        now: Optional[datetime] = None,
    ) -> FixDeliveryQueueSnapshot:
        current = now or self.wall_time
        with self._lock:
            records = tuple(self._records.values())
            states = {
                state: sum(record.state == state for record in records)
                for state in FixDeliveryState
            }
            nonterminal = tuple(
                record
                for record in records
                if record.state != FixDeliveryState.DELIVERED
            )
            oldest = (
                max(
                    0.0,
                    (
                        current
                        - min(record.enqueued_at for record in nonterminal)
                    ).total_seconds(),
                )
                if nonterminal
                else None
            )
            sessions_with_backlog = sum(
                bool(queue) for queue in self._session_queues.values()
            )
            return FixDeliveryQueueSnapshot(
                enqueued=self._enqueued,
                queue_depth=len(nonterminal),
                pending=states[FixDeliveryState.PENDING],
                due=states[FixDeliveryState.DUE],
                retry_pending=states[FixDeliveryState.RETRY_PENDING],
                delivered=self._delivered,
                permanently_failed=self._permanently_failed,
                abandoned=0,
                sessions_with_backlog=sessions_with_backlog,
                attempts=self._attempts,
                retries=self._retries,
                failures=self._failures,
                retained_delivered_records=states[
                    FixDeliveryState.DELIVERED
                ],
                oldest_pending_age_seconds=oldest,
            )

    def _nonterminal_records_locked(
        self,
    ) -> Iterable[OutboundFixDelivery]:
        return (
            record
            for record in self._records.values()
            if record.state != FixDeliveryState.DELIVERED
        )

    def _require_due_head_locked(
        self,
        delivery_id: str,
    ) -> OutboundFixDelivery:
        record = self._records[delivery_id]
        if record.state != FixDeliveryState.DUE:
            raise ValueError(
                f"Delivery {delivery_id} is not due: {record.state.value}"
            )
        queue = self._session_queues.get(record.destination.session_id)
        if not queue or queue[0] != delivery_id:
            raise ValueError(
                f"Delivery {delivery_id} is not the session FIFO head"
            )
        return record
