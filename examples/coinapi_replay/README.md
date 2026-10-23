# Local CoinAPI L3 replay

This application drives the existing MockX exchange from a local L3 CSV. One
MockX book contains both historical liquidity and strategy orders. The replay
client translates source changes into ordinary FIX `D`, `F`, and `G` requests;
the optional midpoint controller runs independently in the same application.
Strategies connect through their own FIX session and consume MockX's existing
market-data interfaces.

The application does not download data, contact CoinAPI, require an API key, or
read a separate trades dataset. The bundled [sample_l3.csv](sample_l3.csv) is
**synthetic test data**, not an exchange capture or evidence of a venue's L3
coverage.

## Run the example

Run commands from the repository root. On Windows, initialize and activate the
environment in each PowerShell terminal:

```powershell
. "C:\Users\lehaj\anaconda3\shell\condabin\conda-hook.ps1"
conda activate mockx
```

Inspect the available options or validate a file without connecting to MockX:

```powershell
python -m examples.coinapi_replay --help
python -m examples.coinapi_replay --input examples/coinapi_replay/sample_l3.csv --symbol BTC-USD --prepare-only --output prepared.json
```

Preparation reports `prepared_not_execution_qualified`. It validates and
normalizes the source, but does not establish that ordinary FIX matching can
reproduce the interval. Temporary prepared events are removed when the command
exits; a subsequent run prepares its input again.

Start the exchange in another terminal:

```powershell
python -m src.mockx.cli --config examples/coinapi_exchange.yml --fix-config examples/coinapi_acceptor.cfg
```

Then run a baseline through real QuickFIX connections:

```powershell
python -m examples.coinapi_replay --input examples/coinapi_replay/sample_l3.csv --symbol BTC-USD --profile exact --seed-mode bulk --output exact-bulk.json
python -m examples.coinapi_replay --input examples/coinapi_replay/sample_l3.csv --symbol BTC-USD --profile exact --seed-mode fix --output exact-fix.json
```

The runner starts and ends its own simulation session. It does not replace an
already active session. Run these commands sequentially; the replay runner must
have exclusive lifecycle and clock control. Other clients may send strategy
orders during interactive runs, but must not start/end sessions or advance the
clock. Keep strategy order submission disabled during baseline qualification.

The runner checks the active run generation before clock advancement and
cleanup. These checks do not atomically fence another administrator's concurrent
session changes. If the HTTP start response is lost, a session may have started
even though the runner could not record ownership. Inspect `/stats` and end it
manually only after confirming that it is your run; do not blindly end whichever
session is active.

For an interactive run, connect a strategy separately and choose the pacing:

```powershell
python -m examples.coinapi_replay --input examples/coinapi_replay/sample_l3.csv --symbol BTC-USD --profile interactive --seed-mode bulk --speed 1 --output interactive.json
```

Enable bounded midpoint control explicitly:

```powershell
python -m examples.coinapi_replay --input examples/coinapi_replay/sample_l3.csv --symbol BTC-USD --profile interactive --speed 1 --midpoint-control --controller-max-qty 10 --controller-total-qty 100 --controller-depth 2 --controller-reaction-ms 100 --output controlled.json
```

These controller budgets are example values in base-asset units, not a calibrated
market-impact model. With no divergence, the controller may submit no orders.
The short synthetic sample is a smoke-test input; use an appropriately qualified
longer interval for strategy interaction or sustained load.

## Configuration and sessions

| File or option | Purpose |
|---|---|
| `examples/coinapi_exchange.yml` | Instrument tick/lot sizes, participant mapping, external clock, latency and rejection settings |
| `examples/coinapi_acceptor.cfg` | MockX QuickFIX acceptor; default port `4001` |
| `examples/coinapi_client.cfg` | Replay and controller initiator sessions |
| `--config`, `--fix-config` | Override the replay application's YAML and initiator configuration paths |
| `--base-url` | Override the HTTP address; default derives from YAML, normally `http://127.0.0.1:8001` |
| `--symbol` | Required destination MockX instrument, configured in YAML |
| `--source-symbol` | Assert the CoinAPI source symbol if absent from source metadata |
| `--date` | UTC calendar date for time-only source timestamps |
| `--timeout` | Logon, request and drain timeout in seconds; default `15` |
| `--max-pending` | Bound pending requests and queued source events; default `10000` |
| `--run-id` | Simulation session label; default `coinapi-replay` |

Use the same YAML configuration for the exchange and runner. Both profiles
require `scheduler.mode: external` and enabled FIX. The exact profile requires
zero rejection probability and zero network, engine and session delays.
The runner compares the start response's `execution_config_fingerprint` with
the local execution configuration and fails if they differ. The digest covers
instruments, latency, execution, scheduler, FIX enablement and participant
mappings; deployment addresses, paths and persistence settings are excluded.
Interactive runs may use configured delays and rejection behavior, although an
unexpected historical request rejection fails the run instead of silently
dropping source events. The shipped configuration has zero delays and zero
rejection probability.

The application uses `replay_buy` and `replay_sell` sessions, plus `controller`
when midpoint control is enabled. Keep these client role names. Their acceptor
session IDs must map to **different participants** in `fix.session_participants`:

```yaml
fix:
  enabled: true
  session_participants:
    "FIX.4.4:MOCKX_COINAPI->replay_buy": replay-buy
    "FIX.4.4:MOCKX_COINAPI->replay_sell": replay-sell
    "FIX.4.4:MOCKX_COINAPI->controller": midpoint-controller
    "FIX.4.4:MOCKX_COINAPI->strategy_bot": strategy
```

Historical bids and buy aggressors use the BUY session; asks and sell
aggressors use the SELL session. This lets historical aggressors trade with
historical liquidity without disabling MockX's mandatory self-trade prevention.
The runner does not start the strategy client. A strategy using the bundled
acceptor connects as `strategy_bot` to `MOCKX_COINAPI` on port `4001`.

## Accepted local source format

The adapter follows the fields and action meanings in CoinAPI's
[Full Limit Order Book flat-file documentation](https://www.coinapi.io/products/flat-files/docs/datasets/limitbook).
CoinAPI L3 coverage depends on the exchange and market; an L3-labelled API can
also return aggregated entries without order IDs. Such entries are rejected
here. See the [CoinAPI L3 dataset documentation](https://www.coinapi.io/datasets/crypto-order-book-l3-data).

Input is UTF-8 `.csv` or `.csv.gz`, with comma or semicolon separators and these
columns, optionally plus `symbol_id`:

```text
time_exchange,time_coinapi,is_buy,update_type,entry_px,entry_sx,order_id
```

- Every row needs an L3 `order_id`. `is_buy` must be `1` for BUY or `0` for SELL.
- Accepted actions are `ADD`, `SUB`, `SET`, `DELETE`, `MATCH`, and the initial
  `SNAPSHOT`. Numeric actions map `0..4` to `ADD`, `SUB`, `SET`, `DELETE`, `MATCH`.
  No undocumented numeric snapshot value is guessed.
- The file must begin with contiguous `SNAPSHOT` rows sharing both timestamps,
  followed by at least one increment. A snapshot-only file, a later snapshot,
  or a source ID reused after termination is unsupported.
- Prices and quantities must convert exactly through the configured instrument.
  No floating-point rounding is applied. Zero quantity is allowed only for
  `SET` and `DELETE`. Underflow, unknown IDs, and existing IDs changing side or
  price are rejected.
- Full timestamps retain up to nine fractional digits. Time-only values need
  `--date YYYY-MM-DD` or a trusted `D-YYYYMMDD`/`D-YYYYMMDDHH` directory component.
  Exchange time must not regress. Equal-time rows retain file order.
- The source symbol comes from `symbol_id`, the filename's `SC-...` component,
  or `--source-symbol`; supplied labels must agree. It is explicitly mapped to
  the destination `--symbol`, not inferred from a similar instrument name.
- Every completed increment must leave an uncrossed source state. The adapter
  does not guess multi-row atomic batches or repair incomplete/gapped data.

For example, an unlabelled local file can be checked with:

```powershell
python -m examples.coinapi_replay --input C:/data/orders.csv.gz --source-symbol COINBASE_SPOT_BTC_USD --symbol BTC-USD --date 2026-01-01 --prepare-only --output source-check.json
```

Preparation reconstructs source state **offline**, writes individual normalized
events and historical midpoint targets to a temporary JSONL stream, and stores
seen identities in temporary SQLite. The runtime retains client order/report
bookkeeping; it does not run a reference matching engine. Prepared events carry
the original row ordinal, exchange/CoinAPI nanoseconds, and a canonical digest
of sorted live source identities, sides, prices and leaves. The input SHA256
identifies the original file bytes, including compression when applicable.

Canonical digests are recomputed per event: preparation CPU cost scales with
live book size. Only digests and individual events are written per increment,
not a complete book snapshot per event. Runtime tracking still retains order,
event, request and report identity histories for the run, so its memory grows
with total events; `--max-pending` bounds backlog, not this retained history.
The disk-backed input stream therefore does not make the entire replay bounded
in memory. This is not yet a throughput-optimized importer or an indefinitely
running replay service for arbitrary large full-depth captures.

## Matching and quantity accounting

| Source event | FIX translation |
|---|---|
| `ADD` | GTC limit New Order Single (`D`) |
| `MATCH` | Opposite-side IOC limit `D`, using the recorded quantity and price |
| `SUB` / `SET` | Cancel/Replace (`G`) to the adjusted total, or Cancel (`F`) when the remaining quantity becomes zero |
| `DELETE` | Cancel (`F`) of the surviving source-owned order |

Each `MATCH` submits its aggressive quantity once. There is no accompanying
passive cancellation or second execution from a trades file. The IOC can trade
with a strategy order, another historical order, or cancel its unfilled
remainder at its price limit. That is an interaction with the simulated market,
not a claim to recover the original exchange's hidden aggressor command.

For a live source order, let `S` be its recorded remaining quantity, `H` its
cumulative recorded `MATCH` quantity since initialization, and `C` its actual
MockX `CumQty`. The client ledger retains `S` and `H` alongside reported order
state. Replacement uses:

```text
desired leaves = max(0, S + H - C)
OrderQty       = C + desired leaves
```

Explicit source deletion/zero-size termination cancels the surviving order.
Terminal simulated orders are not recreated by later amendments. Each source
order's amendments wait for its previous request outcome and use the latest
accepted `ClOrdID`. Duplicate execution reports do not apply fills twice.

`--seed-mode bulk` imports the initial resting snapshot during session start,
using the replay sessions as owners. Subsequent owner `F/G` requests follow the
ordinary FIX rules. Importing does not create trades, simulate inbound latency,
or emit FIX New acknowledgments; the HTTP response supplies the seed identities.
`--seed-mode fix` creates the same initial liquidity using normal GTC `D`
requests and waits for their reports. See the main README's
[snapshot API](../../README.md#initial-resting-snapshot) for its public fields.

## Qualification, clocks and controller limits

The exact profile serializes source requests and checks the seed plus every
increment against both source-identity digests derived from FIX reports and
MockX REST price-level totals/order counts. A mismatch fails the run; midpoint
control cannot conceal it. Success reports `qualified` for that file,
configuration, initial ordering and interval with no strategy traffic.

This qualification does not prove unseen FIFO ordering, recover missing
exchange messages, or reproduce original exchange wire packets. In particular,
a source `MATCH` naming a passive order that is not first in MockX's eligible
queue will fail if it changes the resulting identities. Even a passing interval
cannot validate queue relationships that were never exercised. Feed-handler
protocol/recovery tests remain separate consumers of MockX's existing feeds.

Historical timestamps remain source metadata. Replay maps elapsed source time
to the simulated clock after bootstrap, rather than advancing from Unix epoch
to the capture's calendar date. Exact replay rejects submicrosecond elapsed
times because the engine clock uses Python `datetime`; source nanoseconds are
never silently rounded for qualification. `--profile interactive --prepare-only`
can inspect such a file without claiming exact runtime precision.

Interactive `--speed N` advances historical time at N times wall time. The
source producer, FIX dispatcher, simulated-clock driver, and optional feed/
controller tasks run independently. Per-order dependencies can queue events;
backlog limits fail visibly instead of dropping them. Wall-clock scheduling and
external FIX clients make this profile unsuitable for exact-timestamp claims.
At EOF the controller stops creating orders and the runner drains pending work
within the timeout. It then drains the server FIX delivery queue and uses normal
FIX TestRequest/Heartbeat exchanges on each owned session to confirm that prior
reports have arrived, including passive fills delivered on another session,
before writing the final summary and closing. Strategy clients must stop
submitting at the interval end for this drain to remain finite.

The optional controller starts with the initial snapshot midpoint, consumes the
existing WebSocket depth feed, and receives each later source midpoint only when
its event becomes due. Offline read-ahead does not expose future targets.
Controller decisions wait while historical commands are
pending; the source producer does not wait for a controller order to finish.
The controller has its own FIX participant and at most one order in flight.

It chooses a full-level market sweep predicted to strictly reduce midpoint
error, breaking equal-error ties by smaller quantity and leaving a final
observed level on the consumed side. It submits an ordinary **market order**.
Quantity, total submitted quantity, observed depth and reaction interval are
bounded; actual fill prices/depth can differ if liquidity changes before the
order executes. Stale targets/books, excessive replay lag, one-sided books, and
unreachable improving midpoints suppress a decision.

`--controller-stale-ms` defaults to `5000`; `--controller-lag-ms` defaults to
`100`. Controller reaction, freshness and lag thresholds use wall-clock time.
The first reaction delay begins when a book snapshot is received. Freshness
uses the snapshot's simulated timestamp mapped onto the replay schedule, so
receiving an old buffered snapshot does not make that book fresh again.
The four quantity/depth/reaction arguments are required when enabling the CLI
controller. Market orders cannot replenish liquidity, narrow spreads, or
guarantee an exact midpoint. Controller activity is synthetic and changes the
execution experiment; it is always disabled in exact qualification.

## Evidence and checks

Successful runs write the `--output` JSON summary. The audit defaults to the
same path with an `.events.jsonl` suffix, or can be set with `--audit`. It records
the configuration manifest, source identity and due-event metadata, FIX sends/
reports, checkpoints, controller decisions and failures. When the controller's
feed task is enabled, the audit also captures observed market-data snapshots
and their delay relative to the replay schedule. The summary distinguishes
historical and controller request counts, controller fills, replay backlog/lag,
and wall-clock
round-trip percentiles over the last 4096 completed requests. These are not
exchange-calibrated latency estimates or full-run latency distributions.

Preparation has its own tests:

```powershell
python -m pytest -q tests/test_coinapi_source.py
```

Run the repository suite with `python -m pytest -q`. Native FIX availability,
transport timing and the chosen data interval still need validation in the
environment where an experiment will run.
