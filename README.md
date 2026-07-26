# MockX - FIX-Speaking Mock Exchange with Time Control

A Python-based mock exchange with FIX protocol support, deterministic latency simulation, optional time-series persistence, and process-local observability. This exchange simulates realistic trading conditions including network latency, partial fills, and order rejections. Its time-control API allows external systems such as backtesting engines to control the exchange clock for deterministic simulations.

## Features

- **FIX 4.4 Protocol Support**: Full support for order submission via QuickFIX
- **Deterministic Matching Engine**: Price-time priority order matching
- **Event-Driven Scheduler**: Sophisticated event scheduler with external time control
- **Latency Simulation**: Configurable network and engine latency
- **Market Microstructure**: Slippage, partial fills, and rejection simulation
- **Session Management API**: Start/end sessions with unique run IDs for backtesting
- **Time Control API**: Advance exchange time externally for deterministic backtesting
- **REST API**: Real-time order book snapshots and statistics
- **WebSocket Support**: Live market data streaming
- **InfluxDB Integration**: Time-series data storage for metrics and analysis
- **Prometheus Metrics**: Per-process metrics on the admin API
- **Multi-Exchange**: Run multiple exchanges with different configurations
- **Docker Ready**: Fully containerized with docker-compose

## Quick Start

### 1. Build and Start

```bash
# Build and start all services
docker compose up -d --build

# Wait for services to be healthy
./scripts/wait_for_health.sh
```

### 2. Submit Test Orders

```bash
# Submit orders to the first exchange
python examples/submit_orders.py --host localhost --port 4001
```

### 3. View Order Books

- MockX-1 Order Book: http://localhost:8001/ui/BTC-USDT
- MockX-2 Order Book: http://localhost:8002/ui/BTC-USDT

### 4. View Metrics

- MockX-1 Prometheus exposition: http://localhost:8001/metrics
- MockX-2 Prometheus exposition: http://localhost:8002/metrics

The Prometheus and Grafana services in `docker-compose.yml` are examples and
remain commented out until a deployment supplies scrape and dashboard
configuration.

## Project Structure

```
.
├── src/mockx/          # Main source code
│   ├── __init__.py
│   ├── api.py          # REST/WebSocket API with session management
│   ├── cli.py          # Command-line interface
│   ├── engine.py       # Matching engine
│   ├── gateway.py      # FIX protocol handler
│   ├── latency.py      # Event scheduler with time control
│   ├── main.py         # Main application
│   ├── metrics.py      # Prometheus metrics and TSDB integration
│   ├── models.py       # Data models
│   └── tsdb_writer.py  # InfluxDB writer
├── configs/            # Exchange configurations
├── tests/              # Test suite
├── examples/           # Example scripts
├── scripts/            # Utility scripts
├── templates/          # HTML templates
├── docker-compose.yml  # Multi-container setup
└── Dockerfile          # Container definition
```

## Architecture

```
┌─────────────┐     ┌──────────────┐     ┌─────────────┐
│ FIX Client  │────▶│ FIX Gateway  │────▶│  Matching   │
└─────────────┘     └──────────────┘     │   Engine    │
                                         └──────┬──────┘
                                                │
                    ┌──────────────┐            │
                    │  REST API &  │◀───────────┤
                    │  WebSocket   │            │
                    └──────────────┘            │
                                                │
                    ┌──────────────┐     ┌──────▼──────┐
                    │   InfluxDB   │◀────│   Event     │
                    │  Prometheus  │     │  Scheduler  │
                    └──────────────┘     └─────────────┘
```

## Configuration Reference

Configuration is done via YAML files. See `configs/mockx1.yml` for an example:

```yaml
exchange_name: "MockX-1"
symbols:
  - BTC-USDT
  - ETH-USDT
  
latency:
  inbound_ms: 25      # Submit to engine-receive simulated delay
  engine_ms: 10       # Engine receive to first-report simulated delay
  outbound_ms: 25     # Report creation to delivery simulated delay
  # network_ms: 50    # Legacy symmetric fallback: half in, half out
  
execution:
  partial_fill_prob: 0.25   # Probability of partial fills (0-1)
  reject_prob: 0.01         # Probability of order rejection (0-1)
  random_seed: 42           # Seed for deterministic randomness
    
admin_api:
  port: 8000          # REST API and WebSocket port
  
prometheus:
  enabled: true       # Exposed at /metrics on the admin API port

# Optional TSDB configuration (can be overridden by environment variables)
tsdb:
  url: "http://localhost:8086"
  token: "mockx-token"
  org: "mockx-org"
  bucket: "mockx"
  batch_size: 1000
```

## API Endpoints

### REST API

#### General Endpoints
- `GET /` - Exchange status
- `GET /healthz` - Health check
- `GET /readyz` - Readiness check
- `GET /metrics` - Prometheus text exposition (`prometheus_client` required)
- `GET /symbols` - List available symbols
- `GET /book/{symbol}` - Order book snapshot
- `GET /stats` - Immutable JSON statistics snapshot (includes current run_id)
- `GET /ui/{symbol}` - Order book web UI

`GET /stats` returns this stable shape; empty averages are `null`:

```json
{
  "generated_at": "2026-07-26T12:00:00+00:00",
  "run_id": null,
  "orders": {
    "submitted": 3,
    "accepted": 3,
    "rejected": 0,
    "canceled": 1,
    "filled": 2,
    "partial_fill_events": 0,
    "cancel_requests": 1,
    "cancel_rejects": 0,
    "active": 0,
    "resting": 0
  },
  "trades": {
    "count": 1,
    "quantity": 5.0,
    "notional": 500.0,
    "canceled_quantity": 3.0
  },
  "reports": {"created": 5, "delivery_failures": 0},
  "sessions": {"connected": 1},
  "book": {"levels": 0},
  "latency": {
    "average_real_matching_duration_us": 14.2,
    "average_real_report_serialization_duration_us": 8.1,
    "average_sim_inbound_latency_ms": 5.0,
    "average_sim_engine_latency_ms": 3.0,
    "average_sim_outbound_latency_ms": 7.0,
    "average_sim_end_to_end_latency_ms": 15.0,
    "invalid_measurements": 0
  },
  "persistence": {"failures": 0, "buffered_records": 0}
}
```

Snapshot reads do not reset counters. Active orders, resting orders, and book
levels are derived from the current engine and order books rather than report
delivery state.

#### Session Management (for Backtesting)
- `POST /api/v1/session/start` - Start a new session with a run_id
  ```json
  {
    "run_id": "backtest_20250116_143000"
  }
  ```
  This endpoint resets the exchange state and starts tracking metrics for the specified run_id.

- `POST /api/v1/session/end` - End current session and flush data

#### Time Control (for Backtesting)
- `POST /admin/advance-time` - Advance the exchange's clock to a specific time
  ```json
  {
    "timestamp": "2025-01-16T14:30:00Z"
  }
  ```
  This endpoint advances the scheduler's time and processes all events up to that point.

### WebSocket

- `WS /ws/{symbol}` - Real-time market data stream

## Key Components

### Event Scheduler (`latency.py`)
The heart of the deterministic simulation. The scheduler:
- Maintains an event queue sorted by timestamp
- Can be controlled externally via the time control API
- Processes FIX order arrivals with configured simulated inbound latency
- Schedules engine processing with configured simulated engine latency
- Schedules report delivery with configured simulated outbound latency
- Ensures deterministic order of events

#### Latency definitions

Simulated timestamps always come from `LatencyScheduler`. Real code durations
always come from the injected monotonic `perf_counter_ns` clock. The two clocks
are never subtracted from one another.

| Metric | Exact interval | Published unit |
| --- | --- | --- |
| `mockx_sim_inbound_latency_seconds` | simulated engine receive minus simulated client submit | seconds |
| `mockx_sim_engine_latency_seconds` | simulated first report creation minus simulated engine receive | seconds |
| `mockx_sim_outbound_latency_seconds` | simulated report delivery minus that report's creation | seconds |
| `mockx_sim_end_to_end_latency_seconds` | simulated report delivery minus simulated client submit | seconds |
| `mockx_real_matching_duration_seconds` | monotonic end minus start of `_match_aggressive_order` | seconds |
| `mockx_real_report_serialization_duration_seconds` | monotonic end minus start of FIX execution-report serialization | seconds |

Inbound includes any configured session delay. Outbound and end-to-end samples
exist only for successfully delivered reports. Impossible timestamp ordering is
logged and counted; it is never converted with `abs()`. `/stats` converts the
same samples to milliseconds or microseconds only in explicitly named fields.

### TSDB Writer (`tsdb_writer.py`)
The optional InfluxDB adapter writes eligible metric event records:
- Order events (new, filled, cancelled, rejected)
- Trade executions
- Market data updates
- System metrics
- All data is tagged with run_id for session tracking

`BufferedMetricsWriter` flushes when `tsdb.batch_size` is reached, on explicit
session end, on application shutdown, on `close()`, and on async context-manager
exit. A batch is removed from memory only after the sink acknowledges its
write. On failure it is restored ahead of records produced during the failed
write, the failure is logged and counted, and a later `flush()` retries it.
Concurrent flushes are serialized, empty flushes are safe, and successful
repeated closes are idempotent. If a final shutdown write fails, `close()`
returns failure and leaves the writer open and buffered for an explicit retry;
MockX does not claim durable offline delivery.

InfluxDB uses synchronous acknowledgements behind an async thread boundary so a
successful buffer flush has a defined persistence result. Metric persistence
failures do not roll back trades or book mutations.

### Metric ownership and Prometheus

Metrics are updated from immutable command, trade, report, or rejection
snapshots at one lifecycle point:

| Metric | Owning event |
| --- | --- |
| `mockx_orders_submitted_total` | engine accepts ownership of a new command |
| `mockx_orders_accepted_total` | first non-rejection report for a unique order |
| `mockx_orders_rejected_total` | rejection report creation |
| `mockx_orders_canceled_total`, `mockx_canceled_quantity_total` | terminal cancellation report creation |
| `mockx_orders_filled_total` | first terminal filled report for a unique order |
| `mockx_partial_fill_events_total` | each order-specific `PARTIAL` trade report |
| `mockx_cancel_requests_total` | engine receives a cancel command |
| `mockx_cancel_rejects_total` | immutable cancel reject creation |
| `mockx_trades_total`, `mockx_traded_quantity_total`, `mockx_traded_notional_total` | one immutable trade record per fill |
| `mockx_execution_reports_total` | each order-specific report creation |
| `mockx_execution_report_delivery_failures_total` | a report destination is absent or its transport send fails |

One fill therefore increments `mockx_trades_total` once and
`mockx_execution_reports_total` twice. The two order-specific reports never
double-count quantity or notional, and an IOC/market canceled remainder is not
trade volume.

The application owns a separate Prometheus `CollectorRegistry`, so repeated app
creation in tests cannot duplicate globally registered time series. `/metrics`
exposes the counters above with the `mockx_` prefix; gauges are
`mockx_active_orders`, `mockx_resting_orders`, `mockx_connected_sessions`, and
`mockx_order_book_levels`. The six latency metrics in the preceding table are
histograms. Labels are bounded business dimensions only; raw session IDs,
order IDs, `ClOrdID`, trade IDs, and exception text are not Prometheus labels.

### Matching Engine (`engine.py`)
- Price-time priority matching
- Supports GTC/IOC limit orders and immediately executable market orders
- Configurable execution parameters (slippage, partial fills, rejections)
- Generates one trade record and two order-specific reports per fill

#### Order lifecycle and execution reports

MockX uses an execution-result acknowledgement model:

| Order | No fill | Partial fill | Full fill |
| --- | --- | --- | --- |
| Limit + GTC | Rest as `NEW` | Rest the remainder as `PARTIAL` | `FILLED` |
| Limit + IOC | `CANCELLED` | Preserve fills, cancel the remainder | `FILLED` |
| Market | `CANCELLED` | Preserve fills, cancel the remainder | `FILLED` |

IOC and market orders never rest. A market order supplied with GTC is accepted
but still executes immediately because a market order has no restable limit
price. Its `Price` field is absent from FIX execution reports; `LastPx` and
`AvgPx` contain actual execution prices.

Each passive interaction creates one immutable fill and one trade record.
MockX then creates a passive-side `TRADE` report followed by an
aggressor-side `TRADE` report. Equal-time report deliveries retain this order
through scheduler sequence numbers. A multi-fill aggressor receives one report
per fill, with fill-specific `LastQty`/`LastPx` and monotonically updated
`CumQty`, `LeavesQty`, and `AvgPx`. A partially filled GTC aggressor does not
receive a later `NEW` report.

Execution events and order states are distinct:

| Event | FIX `ExecType` | Allowed `OrdStatus` |
| --- | --- | --- |
| New resting acknowledgement | `0` (New) | `0` (New) |
| Fill | `F` (Trade) | `1` (Partially filled) or `2` (Filled) |
| Cancellation | `4` (Canceled) | `4` (Canceled) |
| Rejection | `8` (Rejected) | `8` (Rejected) |

Report snapshots enforce
`CumQty + LeavesQty + CanceledQty == OrderQty`. Terminal IOC, market, and
client-requested cancellations have zero `LeavesQty`; their unexecuted
remainder is retained as `CanceledQty`.

`ClOrdID` is unique within a stable FIX session, not process-wide. MockX allows
different sessions to use the same value. Reuse within one session is rejected
for the lifetime of the current engine generation, including after terminal
completion; the explicit backtest-session reset starts a new generation.
Cancel requests resolve `OrigClOrdID` with the immutable
`(session_id, ClOrdID)` key.
Pending-new and resting orders with a live remainder can be canceled. A
successful cancellation removes only the live remainder, preserves prior
fills and average price, uses the cancel request ID as `ClOrdID`, and uses the
original order ID as `OrigClOrdID`. Unknown, filled, rejected, already
canceled, cross-session, mismatched, and stale requests produce an immutable
`OrderCancelReject`. Duplicate cancels follow the reject policy and cannot
mutate the book twice.

### FIX Gateway (`gateway.py`)
- QuickFIX-based implementation
- Handles NewOrderSingle and OrderCancelRequest
- Converts QuickFIX `SessionID` at the adapter boundary into an immutable,
  serializable `SessionIdentity`
- Routes ExecutionReports and OrderCancelRejects only by the report snapshot's
  stable `session_id`
- Thread-safe integration with async event loop

QuickFIX objects live only in the gateway's locked `SessionRegistry`. Reconnect
registration atomically replaces the transport for the same stable session ID.
Aliases are display/low-cardinality metric metadata and are never routing keys;
two sessions may use the same alias.

If a report's stable destination is disconnected or unknown, MockX logs the
report context, increments
`mockx_execution_report_delivery_failures_total`, and drops that delivery
attempt. It does not mutate or regenerate the report, change the order, queue
offline delivery, or fall back to another session. A delayed report retains its
original stable destination and will use the replacement transport if that
session has reconnected before delivery.

## FIX Connection

### Required Configuration
The exchange **requires** a FIX configuration file specified via:
- Command line: `--fix-config path/to/config.cfg`
- Environment variable: `FIX_CONFIG_FILE`

Example FIX configuration (`configs/mockx1.cfg`):
```ini
[DEFAULT]
BeginString=FIX.4.4
ResetOnLogon=Y
FileStorePath=store
FileLogPath=logs

[SESSION]
SenderCompID=MOCKX1
TargetCompID=CLIENT
SocketAcceptPort=4001
StartTime=00:00:00
EndTime=00:00:00
```

### Connection Example

```python
import quickfix as fix

# Configure session
settings = fix.SessionSettings()
settings.setString("BeginString", "FIX.4.4")
settings.setString("SenderCompID", "CLIENT")
settings.setString("TargetCompID", "MOCKX1")
settings.setString("SocketConnectHost", "localhost")
settings.setString("SocketConnectPort", "4001")

# Create and send order
order = fix.Message()
order.getHeader().setField(fix.MsgType(fix.MsgType_NewOrderSingle))
order.setField(fix.ClOrdID("ORDER123"))
order.setField(fix.Symbol("BTC-USDT"))
order.setField(fix.Side(fix.Side_BUY))
order.setField(fix.OrderQty(10))
order.setField(fix.OrdType(fix.OrdType_LIMIT))
order.setField(fix.Price(50000))
```

## Testing

Run the test suite:

```bash
# Install dependencies
pip install -r requirements.txt

# Run tests
pytest tests/

# Run with coverage
pytest --cov=mockx tests/
```

## Development

### Running Locally

```bash
# Install dependencies
pip install -r requirements.txt

# Run single exchange (FIX config required)
python -m mockx.cli --config configs/mockx1.yml --fix-config configs/mockx1.cfg
```

### Environment Variables

- `INFLUXDB_URL` - InfluxDB URL (default: http://localhost:8086)
- `INFLUXDB_TOKEN` - InfluxDB authentication token
- `INFLUXDB_ORG` - InfluxDB organization
- `INFLUXDB_BUCKET` - InfluxDB bucket name
- `FIX_CONFIG_FILE` - Path to FIX configuration file
- `CFG_PATH` - Path to exchange configuration file

### Adding New Features

1. **Custom Execution Logic**: Modify `engine.py` to add new matching rules
2. **New Metrics**: Add metrics in `metrics.py` and update Grafana dashboard
3. **Additional Symbols**: Add to configuration file
4. **New API Endpoints**: Extend `api.py`

### Thread Safety

`FIXApplication` callbacks may run on background threads created by QuickFIX. The matching engine and latency scheduler operate on the main asyncio event loop and should not be accessed directly from other threads. Use `asyncio.get_event_loop().call_soon_threadsafe` to schedule engine or scheduler operations from these callbacks.

The session registry, metric counters/snapshots, and persistence buffer each
have isolated locks. No registry or buffer lock is held while calling QuickFIX,
the matching engine, or an external sink. Engine state remains single-threaded
on the asyncio loop, avoiding an engine/metrics/session lock-order dependency.

## Integration with Backtesting

MockX is designed to work seamlessly with backtesting frameworks:

1. **Session Management**: Start a session with a unique run_id before backtesting
2. **Time Control**: The backtester controls the exchange's clock via the advance-time API
3. **Deterministic Execution**: All events are processed in exact time order
4. **Data Persistence**: Eligible metric records are buffered for InfluxDB and
   tagged with the run_id when that optional sink is configured

Example backtesting flow:
```python
# 1. Start session
POST /api/v1/session/start
{"run_id": "backtest_20250116_143000"}

# 2. Submit orders via FIX
# ... FIX orders sent ...

# 3. Advance time to process events
POST /admin/advance-time
{"timestamp": "2025-01-16T14:30:01Z"}

# 4. Repeat steps 2-3 for entire backtest

# 5. End session and flush data
POST /api/v1/session/end
```

## Troubleshooting

### Container Health Issues

```bash
# Check container logs
docker logs mockx1

# Check if port is already in use
netstat -an | grep 4001

# Restart specific container
docker compose restart mockx1
```

### FIX Connection Issues

1. Ensure `TARGET_COMP_ID` and `SENDER_COMP_ID` match configuration
2. Check FIX logs in container: `docker exec mockx1 tail -f logs/*.log`
3. Verify network connectivity to port 4001
4. Ensure FIX configuration file exists and is accessible

### Time Control Issues

- Time can only move forward, not backward
- All events are processed when time is advanced
- Check scheduler logs for event processing details

### Performance Tuning

- Adjust `latency` values in config for faster/slower simulation
- Modify `execution.random_seed` for different deterministic scenarios
- Scale horizontally by adding more exchange instances in docker-compose.yml
- Batch TSDB writes with `tsdb.batch_size` in the YAML configuration

## License

This project is provided as-is for educational and development purposes.
