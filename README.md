# MockX - FIX-Speaking Mock Exchange with Time Control

A Python-based mock exchange with FIX protocol support, deterministic latency simulation, time-series database integration, and full observability. This exchange simulates realistic trading conditions including network latency, slippage, partial fills, and order rejections. It features a unique time-control API that allows external systems (like backtesting engines) to control the exchange's clock for deterministic simulations.

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
- **Prometheus Metrics**: Complete observability with pre-built Grafana dashboards
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

- Prometheus: http://localhost:9090
- Grafana Dashboard: http://localhost:3000 (admin/admin)

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
  network_ms: 50      # Round-trip network latency
  engine_ms: 10       # Engine processing latency
  
execution:
  partial_fill_prob: 0.25   # Probability of partial fills (0-1)
  reject_prob: 0.01         # Probability of order rejection (0-1)
  random_seed: 42           # Seed for deterministic randomness
    
admin_api:
  port: 8000          # REST API and WebSocket port
  
prometheus:
  port: 9100          # Metrics endpoint

# Optional TSDB configuration (can be overridden by environment variables)
tsdb:
  url: "http://localhost:8086"
  token: "mockx-token"
  org: "mockx-org"
  bucket: "mockx"
```

## API Endpoints

### REST API

#### General Endpoints
- `GET /` - Exchange status
- `GET /healthz` - Health check
- `GET /readyz` - Readiness check
- `GET /metrics` - Prometheus metrics
- `GET /symbols` - List available symbols
- `GET /book/{symbol}` - Order book snapshot
- `GET /stats` - Trading statistics (includes current run_id)
- `GET /ui/{symbol}` - Order book web UI

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
- Processes FIX order arrivals with configured network latency
- Schedules engine processing with configured engine latency
- Ensures deterministic order of events

### TSDB Writer (`tsdb_writer.py`)
Writes all exchange events to InfluxDB:
- Order events (new, filled, cancelled, rejected)
- Trade executions
- Market data updates
- System metrics
- All data is tagged with run_id for session tracking

### Matching Engine (`engine.py`)
- Price-time priority matching
- Supports limit orders
- Configurable execution parameters (slippage, partial fills, rejections)
- Generates execution reports and market data updates

### FIX Gateway (`gateway.py`)
- QuickFIX-based implementation
- Handles NewOrderSingle and OrderCancelRequest
- Sends ExecutionReports back to clients
- Thread-safe integration with async event loop

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

## Integration with Backtesting

MockX is designed to work seamlessly with backtesting frameworks:

1. **Session Management**: Start a session with a unique run_id before backtesting
2. **Time Control**: The backtester controls the exchange's clock via the advance-time API
3. **Deterministic Execution**: All events are processed in exact time order
4. **Data Persistence**: All events are written to InfluxDB tagged with the run_id

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
- Batch TSDB writes by adjusting batch_size in tsdb_writer

## License

This project is provided as-is for educational and development purposes.
