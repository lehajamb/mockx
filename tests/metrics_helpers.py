"""Reusable assertions for persisted QuestDB metric identities."""

from src.mockx.metrics import MetricRecord


def questdb_identity(
    record: MetricRecord,
) -> tuple[str, int, str, int]:
    """Return the exact WAL deduplication identity used by QuestDB."""
    return (
        record.table,
        record.timestamp_ns,
        record.run_id,
        record.event_sequence,
    )


def assert_unique_questdb_identities(
    records: tuple[MetricRecord, ...] | list[MetricRecord],
) -> None:
    identities = [questdb_identity(record) for record in records]
    assert len(identities) == len(set(identities))
