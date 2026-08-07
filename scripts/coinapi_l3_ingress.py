#!/usr/bin/env python3
"""Download CoinAPI L3 Full Limit Order Book flat files for a pair/exchange window.

CoinAPI Flat Files are exposed over an S3-compatible API:

  endpoint: https://s3.flatfiles.coinapi.io
  access key: COINAPI_KEY environment variable
  secret key: literal string "coinapi"
  dataset:    T-LIMITBOOK_FULL

Remote layout::

  T-LIMITBOOK_FULL/D-{YYYYMMDD|YYYYMMDDHH}/E-{EXCHANGE}/
      IDDI-{id}+SC-{COINAPI_SYMBOL}+S-{EXCHANGE_SYMBOL}.csv.gz

Partitions in the ``coinapi`` bucket are daily before 2026-06-09 and hourly
from that date onward. The previous day's daily files are also briefly
available in ``coinapi-daily-tail`` (24h retention).

Downloaded ``.csv.gz`` objects are decompressed and written flat into
``data/coinapi/l3`` (or ``--output-dir``). Identity is carried only in the
filename::

  {output_dir}/{pair}_{exchange}_{extract_time}_{partition}_{remote_basename}.csv

Examples::

  $env:COINAPI_KEY = "..."
  python scripts/coinapi_l3_ingress.py BTC-USDT coinbase \\
      --start 2025-09-01T00:00:00Z --end 2025-09-01T06:00:00Z

  python scripts/coinapi_l3_ingress.py BTC-USD COINBASE \\
      --start 2026-01-17T20:00:00Z --end 2026-01-17T22:00:00Z \\
      --dry-run
"""

from __future__ import annotations

import argparse
import gzip
import logging
import os
import re
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Optional, Sequence

try:
    import boto3
    from botocore.client import BaseClient, Config
    from botocore.exceptions import BotoCoreError, ClientError
except ImportError as exc:  # pragma: no cover - import guard for clear UX
    raise SystemExit(
        "boto3 is required. Install it with: pip install boto3"
    ) from exc


LOGGER = logging.getLogger("coinapi_l3_ingress")

ENDPOINT_URL = "https://s3.flatfiles.coinapi.io"
REGION_NAME = "us-east-1"
SECRET_ACCESS_KEY = "coinapi"
DATA_TYPE = "T-LIMITBOOK_FULL"
PRIMARY_BUCKET = "coinapi"
DAILY_TAIL_BUCKET = "coinapi-daily-tail"

# CoinAPI switched LIMITBOOK_FULL (and trades/quotes) in `coinapi` from daily
# to hourly partitions on this UTC date.
HOURLY_PARTITION_START = datetime(2026, 6, 9, tzinfo=timezone.utc)

DEFAULT_OUTPUT_DIR = Path("data") / "coinapi" / "l3"
CHUNK_SIZE = 8 * 1024 * 1024

# Remote object keys look like:
# T-LIMITBOOK_FULL/D-20250901/E-BINANCE/IDDI-138123+SC-BINANCE_SPOT_BTC_USDT+S-BTCUSDT.csv.gz
KEY_PATTERN = re.compile(
    r"^T-LIMITBOOK_FULL/"
    r"D-(?P<partition>\d{8}|\d{10})/"
    r"E-(?P<exchange>[^/]+)/"
    r"(?P<filename>IDDI-(?P<iddi>[^+]+)"
    r"\+SC-(?P<symbol_id>[^+]+)"
    r"\+S-(?P<exchange_symbol>.+?)\.csv\.gz)$"
)


@dataclass(frozen=True)
class RemoteObject:
    """One matching L3 flat-file object on CoinAPI S3."""

    bucket: str
    key: str
    size: int
    partition: str
    exchange: str
    symbol_id: str
    exchange_symbol: str
    filename: str


@dataclass(frozen=True)
class DownloadPlan:
    """Resolved local destination for a remote object."""

    remote: RemoteObject
    local_path: Path


def parse_timestamp(value: str) -> datetime:
    """Parse ISO-8601 or unix epoch into an aware UTC datetime."""

    raw = value.strip()
    if not raw:
        raise argparse.ArgumentTypeError("timestamp must be non-empty")

    if re.fullmatch(r"-?\d+(\.\d+)?", raw):
        ts = float(raw)
        # Heuristic: values without a fractional component and fewer than 1e12
        # are treated as seconds; larger integers as milliseconds.
        if abs(ts) >= 1e12:
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=timezone.utc)

    normalized = raw.replace(" ", "T")
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"

    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"invalid timestamp {value!r}; use ISO-8601 or unix epoch"
        ) from exc

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def normalize_exchange(exchange: str) -> str:
    """CoinAPI exchange path segment is uppercase (e.g. COINBASE, BINANCE)."""

    cleaned = exchange.strip().upper().replace(" ", "").replace("-", "")
    if not cleaned:
        raise ValueError("exchange must be non-empty")
    return cleaned


def normalize_pair(pair: str) -> str:
    """Canonical display form keeps a single hyphen when present."""

    cleaned = pair.strip().upper().replace(" ", "").replace("_", "-")
    if not cleaned:
        raise ValueError("pair must be non-empty")
    return cleaned


def decode_exchange_symbol(symbol: str) -> str:
    """Decode CoinAPI exchange-symbol encodings into a hyphenated pair form.

    Examples:
      BTCUSDT      -> BTCUSDT
      BTC-USD      -> BTC-USD
      BTC__002DUSD -> BTC-USD
    """

    decoded = symbol.upper()
    decoded = decoded.replace("__002D", "-").replace("__002F", "/")
    decoded = decoded.replace("_", "-")
    return decoded


def compact_symbol(symbol: str) -> str:
    """Strip separators so BTC-USDT, BTC_USDT, and BTCUSDT compare equal."""

    return re.sub(r"[^A-Z0-9]", "", symbol.upper())


def symbol_matches(remote: RemoteObject, pair: str) -> bool:
    """Return True when a remote object refers to the requested pair.

    Matching accepts common exchange-symbol spellings and CoinAPI symbol ids:

    * S-BTCUSDT / S-BTC-USDT / S-BTC__002DUSD
    * SC-BINANCE_SPOT_BTC_USDT / SC-COINBASE_SPOT_BTC_USD
    """

    canonical = normalize_pair(pair)
    wanted_compact = compact_symbol(canonical)
    wanted_base_quote = canonical.replace("-", "_")

    exchange_symbol = remote.exchange_symbol.upper()
    symbol_id = remote.symbol_id.upper()
    decoded_symbol = decode_exchange_symbol(exchange_symbol)

    if compact_symbol(exchange_symbol) == wanted_compact:
        return True
    if compact_symbol(decoded_symbol) == wanted_compact:
        return True
    if decoded_symbol.replace("/", "-") == canonical:
        return True
    # CoinAPI symbol ids embed BASE_QUOTE, e.g. COINBASE_SPOT_BTC_USD.
    if re.search(rf"(?:^|_){re.escape(wanted_base_quote)}(?:_|$)", symbol_id):
        return True
    return False


def iter_partitions(start: datetime, end: datetime) -> Iterator[str]:
    """Yield partition labels covering [start, end).

    Daily labels (YYYYMMDD) are used before HOURLY_PARTITION_START.
    Hourly labels (YYYYMMDDHH) are used from that instant onward.
    """

    if end <= start:
        return

    cursor = start.astimezone(timezone.utc)
    end_utc = end.astimezone(timezone.utc)

    # Align to the start of the first relevant partition.
    if cursor < HOURLY_PARTITION_START:
        cursor = cursor.replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        cursor = cursor.replace(minute=0, second=0, microsecond=0)

    while cursor < end_utc:
        if cursor < HOURLY_PARTITION_START:
            yield cursor.strftime("%Y%m%d")
            next_cursor = cursor + timedelta(days=1)
            # Do not jump past the hourly cutover without emitting the
            # remaining pre-cutover day once.
            if next_cursor > HOURLY_PARTITION_START and cursor < HOURLY_PARTITION_START:
                cursor = HOURLY_PARTITION_START
            else:
                cursor = next_cursor
        else:
            yield cursor.strftime("%Y%m%d%H")
            cursor = cursor + timedelta(hours=1)


def partition_bounds(partition: str) -> tuple[datetime, datetime]:
    """Return half-open UTC interval covered by a partition label."""

    if len(partition) == 8:
        start = datetime.strptime(partition, "%Y%m%d").replace(tzinfo=timezone.utc)
        return start, start + timedelta(days=1)
    if len(partition) == 10:
        start = datetime.strptime(partition, "%Y%m%d%H").replace(tzinfo=timezone.utc)
        return start, start + timedelta(hours=1)
    raise ValueError(f"unsupported partition label: {partition}")


def partition_overlaps_window(partition: str, start: datetime, end: datetime) -> bool:
    part_start, part_end = partition_bounds(partition)
    return part_start < end and part_end > start


def buckets_for_partition(partition: str, *, now: Optional[datetime] = None) -> list[str]:
    """Select S3 buckets that may hold objects for a partition."""

    now = now or datetime.now(timezone.utc)
    buckets = [PRIMARY_BUCKET]

    # coinapi-daily-tail only keeps yesterday's daily partition for ~24h.
    if len(partition) == 8:
        yesterday = (now - timedelta(days=1)).strftime("%Y%m%d")
        if partition == yesterday:
            buckets.append(DAILY_TAIL_BUCKET)
    return buckets


def build_s3_client(api_key: str) -> BaseClient:
    """Create a boto3 S3 client pointed at CoinAPI Flat Files."""

    return boto3.client(
        "s3",
        region_name=REGION_NAME,
        endpoint_url=ENDPOINT_URL,
        aws_access_key_id=api_key,
        aws_secret_access_key=SECRET_ACCESS_KEY,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
        ),
    )


def list_objects(
    client: BaseClient,
    *,
    bucket: str,
    prefix: str,
) -> Iterator[tuple[str, int]]:
    """Paginate ListObjectsV2 and yield (key, size) pairs."""

    continuation_token: Optional[str] = None
    while True:
        kwargs = {
            "Bucket": bucket,
            "Prefix": prefix,
            "MaxKeys": 1000,
        }
        if continuation_token:
            kwargs["ContinuationToken"] = continuation_token

        response = client.list_objects_v2(**kwargs)
        for item in response.get("Contents") or []:
            key = item.get("Key")
            if not key or key.endswith("/"):
                continue
            yield key, int(item.get("Size") or 0)

        if not response.get("IsTruncated"):
            break
        continuation_token = response.get("NextContinuationToken")
        if not continuation_token:
            break


def parse_remote_object(bucket: str, key: str, size: int) -> Optional[RemoteObject]:
    match = KEY_PATTERN.match(key)
    if match is None:
        return None
    return RemoteObject(
        bucket=bucket,
        key=key,
        size=size,
        partition=match.group("partition"),
        exchange=match.group("exchange"),
        symbol_id=match.group("symbol_id"),
        exchange_symbol=match.group("exchange_symbol"),
        filename=match.group("filename"),
    )


def discover_objects(
    client: BaseClient,
    *,
    pair: str,
    exchange: str,
    start: datetime,
    end: datetime,
) -> list[RemoteObject]:
    """List and filter L3 objects for the requested window."""

    exchange_id = normalize_exchange(exchange)
    found: dict[tuple[str, str], RemoteObject] = {}

    for partition in iter_partitions(start, end):
        if not partition_overlaps_window(partition, start, end):
            continue

        prefix = f"{DATA_TYPE}/D-{partition}/E-{exchange_id}/"
        for bucket in buckets_for_partition(partition):
            LOGGER.info("Listing s3://%s/%s", bucket, prefix)
            try:
                listed = list(list_objects(client, bucket=bucket, prefix=prefix))
            except ClientError as exc:
                error_code = exc.response.get("Error", {}).get("Code", "Unknown")
                LOGGER.warning(
                    "Failed listing s3://%s/%s (%s): %s",
                    bucket,
                    prefix,
                    error_code,
                    exc,
                )
                continue

            if not listed:
                LOGGER.debug("No objects under s3://%s/%s", bucket, prefix)
                continue

            for key, size in listed:
                remote = parse_remote_object(bucket, key, size)
                if remote is None:
                    LOGGER.debug("Skipping unrecognized key: %s", key)
                    continue
                if remote.exchange != exchange_id:
                    continue
                if not symbol_matches(remote, pair):
                    continue

                dedupe_key = (remote.partition, remote.filename)
                # Prefer the primary historical bucket when the same file exists
                # in both coinapi and coinapi-daily-tail.
                existing = found.get(dedupe_key)
                if existing is None or (
                    existing.bucket == DAILY_TAIL_BUCKET and bucket == PRIMARY_BUCKET
                ):
                    found[dedupe_key] = remote

    return sorted(found.values(), key=lambda obj: (obj.partition, obj.filename, obj.bucket))


def extract_timestamp_label(when: Optional[datetime] = None) -> str:
    """UTC extract time used for local filename versioning."""

    when = when or datetime.now(timezone.utc)
    return when.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def safe_filename_component(value: str) -> str:
    """Make a string safe for use in a single path component."""

    cleaned = re.sub(r"[^\w.\-]+", "_", value.strip(), flags=re.UNICODE)
    return cleaned.strip("._") or "unknown"


def strip_remote_compression_suffix(filename: str) -> str:
    """Drop ``.csv.gz`` / ``.gz`` so the local extracted name ends in ``.csv``."""

    lower = filename.lower()
    if lower.endswith(".csv.gz"):
        return filename[: -len(".csv.gz")]
    if lower.endswith(".gz"):
        return filename[: -len(".gz")]
    if lower.endswith(".csv"):
        return filename[: -len(".csv")]
    return filename


def build_local_path(
    remote: RemoteObject,
    *,
    pair: str,
    exchange: str,
    output_dir: Path,
    extract_time: str,
) -> Path:
    """Compose a flat, versioned local path for an extracted remote object.

    All files share one directory. Pair, exchange, extract time, and partition
    are embedded in the filename::

      {output_dir}/{pair}_{exchange}_{extract_time}_{partition}_{remote_basename}.csv
    """

    pair_part = safe_filename_component(normalize_pair(pair))
    exchange_part = safe_filename_component(normalize_exchange(exchange))
    partition_part = safe_filename_component(f"D-{remote.partition}")
    remote_base = safe_filename_component(strip_remote_compression_suffix(remote.filename))

    filename = (
        f"{pair_part}_{exchange_part}_{extract_time}_{partition_part}_{remote_base}.csv"
    )
    return output_dir / filename


def download_and_extract(
    client: BaseClient,
    remote: RemoteObject,
    local_path: Path,
    *,
    force: bool = False,
) -> bool:
    """Download one gzip object and extract it to a local CSV.

    Uses GetObject streaming instead of multipart transfer because CoinAPI
    Flat Files does not support multi-part downloads. The ``.csv.gz`` payload
    is gunzipped on the fly into ``local_path``.
    """

    if local_path.exists() and not force:
        LOGGER.info("Skipping existing file: %s", local_path)
        return False

    local_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = local_path.with_name(local_path.name + ".partial")

    LOGGER.info(
        "Downloading and extracting s3://%s/%s (%s bytes compressed) -> %s",
        remote.bucket,
        remote.key,
        remote.size,
        local_path,
    )

    body = None
    try:
        response = client.get_object(Bucket=remote.bucket, Key=remote.key)
        body = response["Body"]
        with gzip.GzipFile(fileobj=body, mode="rb") as gz_stream:
            with tmp_path.open("wb") as handle:
                shutil.copyfileobj(gz_stream, handle, length=CHUNK_SIZE)
        tmp_path.replace(local_path)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise
    finally:
        if body is not None:
            body.close()

    return True


def plan_downloads(
    objects: Sequence[RemoteObject],
    *,
    pair: str,
    exchange: str,
    output_dir: Path,
    extract_time: str,
) -> list[DownloadPlan]:
    return [
        DownloadPlan(
            remote=obj,
            local_path=build_local_path(
                obj,
                pair=pair,
                exchange=exchange,
                output_dir=output_dir,
                extract_time=extract_time,
            ),
        )
        for obj in objects
    ]


def format_size(num_bytes: int) -> str:
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024.0 or unit == "TB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.2f} {unit}"
        value /= 1024.0
    return f"{num_bytes} B"


def run(args: argparse.Namespace) -> int:
    api_key = os.environ.get("COINAPI_KEY") or os.environ.get("COINAPI_API_KEY")
    if not api_key:
        LOGGER.error(
            "Set COINAPI_KEY (or COINAPI_API_KEY) in the environment before running."
        )
        return 2

    pair = normalize_pair(args.pair)
    exchange = normalize_exchange(args.exchange)
    start: datetime = args.start
    end: datetime = args.end

    if end <= start:
        LOGGER.error("end must be after start (got start=%s end=%s)", start, end)
        return 2

    output_dir = Path(args.output_dir).expanduser().resolve()
    extract_time = extract_timestamp_label()

    LOGGER.info(
        "Ingress L3 LIMITBOOK_FULL pair=%s exchange=%s window=[%s, %s) extract=%s",
        pair,
        exchange,
        start.isoformat(),
        end.isoformat(),
        extract_time,
    )

    client = build_s3_client(api_key)

    try:
        objects = discover_objects(
            client,
            pair=pair,
            exchange=exchange,
            start=start,
            end=end,
        )
    except (BotoCoreError, ClientError) as exc:
        LOGGER.error("Failed to list CoinAPI objects: %s", exc)
        return 1

    if not objects:
        LOGGER.warning(
            "No L3 flat files matched pair=%s exchange=%s in [%s, %s).",
            pair,
            exchange,
            start.isoformat(),
            end.isoformat(),
        )
        return 1

    plans = plan_downloads(
        objects,
        pair=pair,
        exchange=exchange,
        output_dir=output_dir,
        extract_time=extract_time,
    )

    total_bytes = sum(plan.remote.size for plan in plans)
    LOGGER.info(
        "Matched %d file(s), total %s",
        len(plans),
        format_size(total_bytes),
    )

    for plan in plans:
        LOGGER.info(
            "  s3://%s/%s -> %s (%s)",
            plan.remote.bucket,
            plan.remote.key,
            plan.local_path,
            format_size(plan.remote.size),
        )

    if args.dry_run:
        LOGGER.info("Dry run complete; no files downloaded.")
        return 0

    downloaded = 0
    skipped = 0
    failed = 0

    for plan in plans:
        try:
            did_download = download_and_extract(
                client,
                plan.remote,
                plan.local_path,
                force=args.force,
            )
        except (BotoCoreError, ClientError, OSError) as exc:
            LOGGER.error(
                "Download failed for s3://%s/%s: %s",
                plan.remote.bucket,
                plan.remote.key,
                exc,
            )
            failed += 1
            continue

        if did_download:
            downloaded += 1
        else:
            skipped += 1

    LOGGER.info(
        "Done. downloaded=%d skipped=%d failed=%d output_dir=%s",
        downloaded,
        skipped,
        failed,
        output_dir,
    )
    return 1 if failed else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Download CoinAPI L3 Full Limit Order Book flat files for a "
            "trading pair and exchange over a time window."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "pair",
        help="Trading pair, e.g. BTC-USDT or BTC-USD",
    )
    parser.add_argument(
        "exchange",
        help="Exchange id as used by CoinAPI, e.g. coinbase or BINANCE",
    )
    parser.add_argument(
        "--start",
        required=True,
        type=parse_timestamp,
        help="Start of interval (ISO-8601 or unix epoch, UTC if naive)",
    )
    parser.add_argument(
        "--end",
        required=True,
        type=parse_timestamp,
        help="End of interval (ISO-8601 or unix epoch, UTC if naive, exclusive)",
    )
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_DIR),
        help="Local directory root for downloaded flat files",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List matching remote objects without downloading",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-download even when the versioned local file already exists",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    # Quiet noisy botocore loggers unless verbose.
    if not args.verbose:
        logging.getLogger("botocore").setLevel(logging.WARNING)
        logging.getLogger("urllib3").setLevel(logging.WARNING)

    return run(args)


if __name__ == "__main__":
    sys.exit(main())
