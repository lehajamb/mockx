# Configuration

MockX loads one exchange YAML file and one QuickFIX settings file. Startup validates the merged configuration before external traffic starts. Begin with the checked-in [local configuration](https://github.com/lehajamb/mockx/blob/556e28b533171ceee63864e396faedfb2ef7aa8e/configs/mockx-local.yml), then change only the settings your scenario needs.

```bash
python -m src.mockx.cli --config configs/mockx-local.yml --fix-config configs/mockx1.cfg
```

The executable, configuration paths, and examples assume the repository root is the working directory. `CFG_PATH` and `FIX_CONFIG_FILE` also select the YAML and QuickFIX files. See [Getting Started](https://github.com/lehajamb/mockx/wiki/Getting-Started) for installation and the first run.

## Configuration sections

| Section | Purpose | Important rules |
|---|---|---|
| `exchange_name` | Instance name exposed by the API | Nonempty string |
| `instruments` | Symbol catalog and exact price/quantity grids | Nonempty, unique normalized symbols; positive finite tick/lot values |
| `latency` | Simulated inbound, engine, outbound and session delays | Integer milliseconds; use `network_ms` or both directional values |
| `execution` | Simulated rejection probability and reproducible RNG seed | Probability 0–1; seed is a signed 64-bit integer |
| `admin_api` | Shared HTTP/WebSocket listener and administrative route switch | Host defaults to `127.0.0.1`; port 1–65535 |
| `fix` | FIX enablement, participant ownership and delivery retry bounds | Enabled sessions need exact participant mappings |
| `scheduler` | External or realtime advancement and event budget | `external` is the default |
| `websocket` | Queue, send, heartbeat and browser freshness timings | Timings are seconds, not simulated milliseconds |
| `cors_origins` | Explicit cross-origin browser callers | Unique HTTP(S) origins; wildcard forbidden; default empty |
| `prometheus` | `/metrics` exposition | Enabled by default |
| `questdb` | Optional buffered persistence | Omit the section or use `enabled: false` to disable |
| `itch` / `moldudp64` | Optional displayed-order multicast feed | ITCH requires valid MoldUDP64 configuration |
| `glimpse` / `soupbintcp` | Optional atomic snapshot and interrupted-spin recovery | GLIMPSE requires ITCH/MoldUDP64 and SoupBinTCP |

Models reject unknown fields and obsolete options. YAML booleans must be actual `true`/`false` values, rather than strings. Errors are deterministic and sorted by field path, for example `execution.reject_prob: Input should be less than or equal to 1`.

## Minimal local exchange

This complete example enables one FIX participant, external clock control and a single symbol. Save it as a separate YAML file and launch it with the checked-in `configs/mockx1.cfg` QuickFIX settings.

```yaml
exchange_name: MockX-Wiki
instruments:
  BTC-USDT:
    tick_size: "0.01"
    lot_size: "0.001"
    price_display_precision: 2
    quantity_display_precision: 3
latency:
  network_ms: 0
  engine_ms: 1
execution:
  reject_prob: 0.0
  random_seed: 42
admin_api:
  host: 127.0.0.1
  port: 8001
  enabled: true
fix:
  enabled: true
  session_participants:
    "FIX.4.4:MOCKX1->CLIENT1": participant-1
scheduler:
  mode: external
questdb:
  enabled: false
```

`admin_api.enabled: false` removes the session-start, session-end and time-control routes. It does not disable the shared read-only API or WebSocket listener. The bundled application has no production authentication or TLS termination; bind administration to a trusted interface.

## Instruments and exact values

Quote decimal increments in YAML to preserve the intended decimal representation. MockX converts admissible prices and quantities into integer ticks and lots before matching. It does not round an off-grid order onto the instrument grid.

| Setting/value | Result |
|---|---|
| `tick_size: "0.01"`, price `100.00` | 10,000 exact ticks |
| `lot_size: "0.001"`, quantity `0.006` | 6 exact lots |
| Price `100.005` on a `0.01` tick | `OFF_TICK_PRICE` |
| Quantity `0.0005` on a `0.001` lot | `OFF_LOT_QUANTITY` |
| An exact grid value exceeding signed 64-bit units | `NUMBER_OUT_OF_RANGE` |

Display precision must be 0–18 and cannot be less than the increment's exact scale after trailing zeros are removed. Omission infers that scale: `0.2500` requires two decimal places. Increment scale above 18 is rejected even when display precision is omitted.

REST, WebSocket and FIX display values are fixed-scale decimal strings. Totals retain exact significant digits. Displayed average prices use one round-half-even operation at the configured price precision, calculated from integer fill totals. Do not convert these strings to binary floats in a client that must preserve exactness.

Enabling ITCH adds wire limits: symbols must fit eight ASCII bytes, tick sizes must be representable with four decimal places, quantities must fit the unsigned 32-bit share field, and prices cannot exceed `200000.0000`. One internal lot represents one ITCH share; clients use `lot_size` separately to convert shares into base units.

## Latency and clock control

Choose one network-delay shape:

```yaml
latency:
  inbound_ms: 2
  outbound_ms: 3
  engine_ms: 1
  session_delays:
    slow_client: 5
```

Directional fields must be supplied together and cannot coexist with `network_ms`. Every delay is an integer from 0 to 2,147,483,647 ms. Session delays are keyed by session alias and add to inbound latency; aliases do not determine participant ownership.

| Scheduler field | Default | Range |
|---|---:|---|
| `mode` | `external` | `external` or `realtime` |
| `interval_ms` | 10 | 1–60,000 |
| `max_advance_ms` | 86,400,000 | 1–86,400,000 |
| `max_events_per_advance` | 100,000 | 1–1,000,000 |

The default external clock advances only through administration. Realtime mode advances by `interval_ms` while the lifecycle permits it and rejects manual advancement. The event budget bounds a complete advancement, including zero-delay work spawned by handlers. See [Sessions and Simulated Time](https://github.com/lehajamb/mockx/wiki/Sessions-and-Simulated-Time).

## FIX identities and retries

QuickFIX protocol sessions are configured in the `.cfg` file. Map every configured session's complete acceptor-side `SessionID` string to a participant in YAML:

```yaml
fix:
  enabled: true
  session_participants:
    "FIX.4.4:MOCKX1->CLIENT1": participant-1
  delivery_retry_interval_ms: 1000
  delivery_max_attempts_per_cycle: 3
  delivery_terminal_retention_records: 10000
```

Multiple sessions may share a participant, but orders from them still trigger mandatory self-trade prevention when they cross. Ownership cannot be disabled. Retry intervals use simulated milliseconds; the attempt limit includes the first send, and a new logon starts a fresh bounded cycle. Retention bounds delivered payloads, not the process-lifetime duplicate-ID namespace. See [FIX Trading](https://github.com/lehajamb/mockx/wiki/FIX-Trading).

## WebSocket and browser timings

| Field | Default | Meaning |
|---|---:|---|
| `heartbeat_interval_seconds` | 10 | Connection heartbeat cadence |
| `outbound_queue_capacity` | 8 | Pending frames, excluding the current send |
| `send_timeout_seconds` | 5 | Maximum duration of a network send |
| `heartbeat_stale_seconds` | 30 | Browser heartbeat freshness threshold |
| `market_data_stale_seconds` | 30 | Browser market-data freshness threshold |
| `fallback_poll_seconds` | 2 | Browser REST fallback interval |
| `reconnect_initial_seconds` | 1 | Initial reconnect delay |
| `reconnect_max_seconds` | 30 | Maximum reconnect delay |
| `staleness_check_seconds` | 1 | Browser freshness check cadence |

All timing values must be finite and in `(0, 86400]`; heartbeat and market-data staleness must exceed heartbeat cadence. Queue capacity is 1–100,000, and maximum reconnect delay must be at least the initial delay. See [WebSocket Market Data](https://github.com/lehajamb/mockx/wiki/WebSocket-Market-Data) for conflation and sequence semantics.

## Persistence and environment overrides

An enabled `questdb` section requires `url` and `health_url`, both absolute HTTP(S) origins without paths, queries or fragments. Username and password are optional but must be supplied together. An explicit `questdb` section defaults to enabled; use `enabled: false` when configuring values without activating persistence.

Supported environment variables override their corresponding YAML fields before the same validation runs:

```text
QUESTDB_URL                  QUESTDB_HEALTH_URL
QUESTDB_USERNAME             QUESTDB_PASSWORD
QUESTDB_ENABLED              QUESTDB_FLUSH_BATCH_SIZE
QUESTDB_MAX_BUFFER_RECORDS   QUESTDB_MAX_BUFFER_BYTES
QUESTDB_FLUSH_INTERVAL_MS    QUESTDB_RETRY_BACKOFF_MS
QUESTDB_MAX_RETRY_DELAY_MS   QUESTDB_OVERFLOW_RETRY_ATTEMPTS
QUESTDB_SHUTDOWN_TIMEOUT_MS  QUESTDB_OVERFLOW_POLICY
QUESTDB_REQUEST_TIMEOUT_MS
```

`QUESTDB_ENABLED` accepts case-insensitive `true` or `false`. Integer overrides must parse as integers. Unknown legacy variable names are not recognized. Record capacity must be at least batch size, maximum retry delay must be at least initial backoff, and overflow policy is `reject_new` or `drop_oldest`. Persistence loss can fail a simulation run; consult [Metrics and Persistence](https://github.com/lehajamb/mockx/wiki/Metrics-and-Persistence) before changing its bounds.

For feed configuration, use the complete [ITCH and GLIMPSE](https://github.com/lehajamb/mockx/wiki/ITCH-and-GLIMPSE) example.

## Source references

- [Configuration models, bounds and environment merge](https://github.com/lehajamb/mockx/blob/556e28b533171ceee63864e396faedfb2ef7aa8e/src/mockx/configuration.py)
- [Published README configuration reference](https://github.com/lehajamb/mockx/blob/556e28b533171ceee63864e396faedfb2ef7aa8e/README.md#12-configuration)
- [Validation regression coverage](https://github.com/lehajamb/mockx/blob/556e28b533171ceee63864e396faedfb2ef7aa8e/tests/test_pr8_validation.py)
