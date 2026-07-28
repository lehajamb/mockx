"""InfluxDB persistence adapter for immutable metric records."""

from __future__ import annotations

import asyncio
import logging
from typing import Sequence

try:
    from influxdb_client import InfluxDBClient, Point
    from influxdb_client.client.write_api import SYNCHRONOUS
except ModuleNotFoundError:  # Core metrics tests require no InfluxDB package.
    InfluxDBClient = Point = SYNCHRONOUS = None

from .metrics import MetricRecord, MetricsSinkError


logger = logging.getLogger(__name__)


class InfluxDBMetricsSink:
    """Synchronous-acknowledgement InfluxDB client behind an async boundary."""

    def __init__(
        self,
        url: str,
        token: str,
        org: str,
        bucket: str,
    ) -> None:
        if InfluxDBClient is None:
            raise RuntimeError(
                "InfluxDB support requires the influxdb-client package"
            )
        self.org = org
        self.bucket = bucket
        self.client = InfluxDBClient(url=url, token=token, org=org)
        # The application-owned BufferedMetricsWriter provides batching.  A
        # synchronous write API gives an unambiguous success/failure boundary.
        self.write_api = self.client.write_api(write_options=SYNCHRONOUS)
        self._closed = False
        self._close_lock = asyncio.Lock()

    async def write(self, records: Sequence[MetricRecord]) -> None:
        if self._closed:
            raise MetricsSinkError("InfluxDB metrics sink is closed")
        if not records:
            return
        points = [self._to_point(record) for record in records]
        try:
            await asyncio.to_thread(
                self.write_api.write,
                bucket=self.bucket,
                org=self.org,
                record=points,
            )
        except Exception as exc:
            raise MetricsSinkError("InfluxDB metric write failed") from exc

    async def flush(self) -> None:
        if self._closed:
            return
        try:
            await asyncio.to_thread(self.write_api.flush)
        except Exception as exc:
            raise MetricsSinkError("InfluxDB metric flush failed") from exc

    async def close(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            try:
                await asyncio.to_thread(self.write_api.close)
                await asyncio.to_thread(self.client.close)
            except Exception as exc:
                raise MetricsSinkError("InfluxDB metric close failed") from exc
            self._closed = True

    @staticmethod
    def _to_point(record: MetricRecord):
        point = Point(record.measurement)
        for key, value in record.tags:
            point.tag(key, value)
        for key, value in record.fields:
            point.field(key, value)
        point.time(record.persistence_timestamp_ns, write_precision="ns")
        return point
