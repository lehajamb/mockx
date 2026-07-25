"""
tsdb_writer.py

Provides a client for writing metrics to a time-series database (InfluxDB).
"""

import logging
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS, ASYNCHRONOUS
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

class TSDBWriter:
    """
    Handles batch writing of metrics to InfluxDB to avoid performance bottlenecks.
    """
    def __init__(self, url: str, token: str, org: str, bucket: str, batch_size: int = 1000):
        """
        Initializes the InfluxDB client and write API.

        Args:
            url: The URL of the InfluxDB instance.
            token: The authentication token.
            org: The InfluxDB organization.
            bucket: The bucket to write data to.
            batch_size: The number of data points to collect before writing.
        """
        self.run_id: Optional[str] = None
        try:
            self.client = InfluxDBClient(url=url, token=token, org=org)
            # Use asynchronous batching for performance
            self.write_api = self.client.write_api(write_options=ASYNCHRONOUS)
            self.bucket = bucket
            logger.info(f"TSDBWriter initialized for InfluxDB at {url}, bucket '{bucket}'.")
        except Exception as e:
            logger.error(f"Failed to initialize InfluxDB client: {e}")
            self.client = None
            self.write_api = None

    def write_metric(self, measurement: str, tags: Dict[str, str], fields: Dict[str, Any], timestamp: int):
        """
        Writes a single data point to the InfluxDB buffer.

        Args:
            measurement: The name of the measurement (e.g., 'trades').
            tags: A dictionary of tags for the data point (e.g., {'symbol': 'BTC-USDT'}).
            fields: A dictionary of fields (the actual data).
            timestamp: The UTC timestamp of the event (in nanoseconds).
        """
        if not self.write_api:
            return

        try:
            point = Point(measurement)
            
            # Add run_id tag if it exists
            if self.run_id:
                tags['run_id'] = self.run_id

            for key, value in tags.items():
                point.tag(key, value)
            for key, value in fields.items():
                # InfluxDB requires float for most numeric types
                point.field(key, float(value))
            point.time(timestamp, write_precision='ns')
            
            self.write_api.write(bucket=self.bucket, record=point)
        except Exception as e:
            logger.error(f"Failed to write metric to InfluxDB: {e}")

    def close(self):
        """
        Flushes any buffered data and closes the client connection.
        This should be called on application shutdown.
        """
        if self.write_api:
            logger.info("Flushing remaining metrics to TSDB...")
            self.write_api.close()
        if self.client:
            self.client.close()
        logger.info("TSDBWriter closed.")

    def flush(self):
        """Force flush all pending writes without closing the connection."""
        if self.write_api:
            self.write_api.flush()
            logger.info("Flushed pending metrics to TSDB")

