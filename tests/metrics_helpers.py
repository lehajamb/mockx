"""Reusable assertions for persisted Influx metric identities."""

from src.mockx.metrics import MetricRecord


def influx_identity(
    record: MetricRecord,
) -> tuple[str, tuple[tuple[str, str], ...], int]:
    """Return the exact measurement/tag/timestamp identity used by InfluxDB."""
    return (
        record.measurement,
        tuple(sorted(record.tags)),
        record.persistence_timestamp_ns,
    )


def assert_unique_influx_identities(
    records: tuple[MetricRecord, ...] | list[MetricRecord],
) -> None:
    identities = [influx_identity(record) for record in records]
    assert len(identities) == len(set(identities))
