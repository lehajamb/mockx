# MockX C++20 two-venue smart order router example

`mockx_sor` demonstrates how two independent MockX instances can test a
latency-sensitive smart order router. It takes one limit parent, synchronizes
two GLIMPSE/ITCH books, sweeps eligible displayed depth by price, submits at
most one IOC limit child per venue over FIX 4.4, aggregates terminal Execution
Reports, and exits.

This is deliberately a small test component rather than a production SOR. It
does not score fees, predict latency, discover hidden liquidity, replace
orders, or chase an IOC remainder.

## Data path

Each venue reuses `cpp_market_feed`'s feed handler. UDP is received directly
into a cache-line-aligned SPSC datagram slot, validated MoldUDP64 and ITCH
messages are viewed in place, and a GLIMPSE cut repairs any continuity loss.
The feed thread publishes a fixed-capacity, 16-level snapshot directly into a
second SPSC ring. Ring overflow and feed recovery are fail-closed conditions.

The coordinator requires both feeds to be `LIVE` and both QuickFIX sessions to
be logged on. BUY parents merge asks low-to-high; SELL parents merge bids
high-to-low. Equal prices use `VenuePriority`. Allocations for one venue are
collapsed into one IOC child at that venue's worst selected price. A moving
book can cancel an IOC remainder, but the example performs no second pass.
One `ThreadedSocketInitiator` owns the two sessions described by the standard
[QuickFIX session configuration](https://quickfixengine.org/c/documentation/getting-started/configuration.html).

QuickFIX has its own allocation model. The zero-copy claim applies to the
market-data ingress and explicit in-process SPSC handoffs.

## QuickFIX C++ prerequisite

QuickFIX C++ is an intentional installed dependency. CMake searches an active
vcpkg toolchain, pkg-config, normal install prefixes, and `QUICKFIX_ROOT`; it
never downloads source during configure.
See the [QuickFIX C++ repository](https://github.com/quickfix/quickfix) for its
source-build and vcpkg installation paths.

With vcpkg:

```powershell
vcpkg install quickfix:x64-windows
cmake -S examples/cpp_sor -B examples/cpp_sor/build `
  -DCMAKE_TOOLCHAIN_FILE=C:/vcpkg/scripts/buildsystems/vcpkg.cmake
```

With an official source installation, pass its prefix:

```bash
cmake -S examples/cpp_sor -B examples/cpp_sor/build \
  -DQUICKFIX_ROOT=/opt/quickfix -DCMAKE_BUILD_TYPE=Release
```

Build and test:

```powershell
cmake --build examples/cpp_sor/build --config Release
ctest --test-dir examples/cpp_sor/build -C Release --output-on-failure
```

On Linux, `-DMOCKX_SOR_ENABLE_SANITIZERS=ON` enables AddressSanitizer and
UndefinedBehaviorSanitizer. The normal C++ tests do not start MockX. Configure
with `-DMOCKX_SOR_ENABLE_LIVE_TEST=ON` from the MockX Python environment to add
the two-venue end-to-end CTest.

## Demo topology

The checked-in fixtures define:

| Component | Venue A | Venue B |
|---|---:|---:|
| MockX API | 8101 | 8102 |
| FIX | 4101 | 4102 |
| MoldUDP64 | 239.192.10.1:18100 | 239.192.10.2:18200 |
| GLIMPSE | 18101 | 18201 |

### Docker quick start (recommended)

The example has its own Compose stack, separate from the repository-root live
demo. From the repository root, build and start both venues, both Windows-host
MoldUDP64 relays, and the one-shot FIX book seeder:

```powershell
docker compose -f examples/cpp_sor/docker-compose.yml up --build
```

Wait until `seed-books` prints the following line and exits with code 0. The
two MockX services and feed relays remain running for `mockx_sor.exe`:

```text
SOR books ready: Venue A asks 0.002@100.00, 0.009@102.00; Venue B asks 0.003@99.00, 0.001@100.00
```

The seeder uses HTTP only for readiness, simulation lifecycle, and final book
verification. It logs on as `SEED_A` and `SEED_B`, submits all passive GTC
orders over FIX, waits for their `NEW` Execution Reports, and then disconnects.
Those sessions map to `passive-a` and `passive-b`; the later SOR sessions remain
mapped to `sor-demo`, so mandatory self-trade prevention does not block fills.

For a detached stack, inspect the completed seeder before running the SOR:

```powershell
docker compose -f examples/cpp_sor/docker-compose.yml up --build -d
docker compose -f examples/cpp_sor/docker-compose.yml ps -a
docker compose -f examples/cpp_sor/docker-compose.yml logs seed-books
```

The bootstrap deliberately ends any active demo sessions before starting
`sor-a` and `sor-b`, so it can restore the deterministic books at any time:

```powershell
docker compose -f examples/cpp_sor/docker-compose.yml run --rm seed-books
```

Stop and remove only this example's containers and network with:

```powershell
docker compose -f examples/cpp_sor/docker-compose.yml down
```

If either feed cannot become live, confirm Docker Desktop resolves
`host.docker.internal`, ports 18100 and 18200 are available for UDP on the
host, and both relay services are still running. Inspect their output with
`docker compose -f examples/cpp_sor/docker-compose.yml logs`.

### Manual setup

To run the same topology without Docker, start each exchange in its own
terminal from the repository root:

```powershell
python -m src.mockx.cli --config examples/cpp_sor/venue_a.yml `
  --fix-config examples/cpp_sor/venue_a_acceptor.cfg
```

```powershell
python -m src.mockx.cli --config examples/cpp_sor/venue_b.yml `
  --fix-config examples/cpp_sor/venue_b_acceptor.cfg
```

Reset both deterministic sessions and seed the same passive liquidity through
the dedicated FIX initiator configuration. Venue A receives 2 lots at 100 and
9 at 102; venue B receives 3 lots at 99 and 1 at 100. Quantities are decimal
instrument units, while the SOR CLI uses ITCH shares/internal lots.

```powershell
python examples/cpp_sor/seed_books.py `
  --fix-config examples/cpp_sor/seed_books.cfg
```

### Run the SOR

Set GLIMPSE passwords outside the process list and route a seven-lot BUY with a
100 limit:

```powershell
$env:MOCKX_SOR_A_GLIMPSE_PASSWORD = "SECRET_A"
$env:MOCKX_SOR_B_GLIMPSE_PASSWORD = "SECRET_B"
examples\cpp_sor\build\Release\mockx_sor.exe `
  --config examples/cpp_sor/sor.cfg --parent-id DEMO-1 `
  --symbol BTC-USDT --side BUY --quantity-lots 7 `
  --lot-size 0.001 --limit-price 100.00
```

The expected plan sends four lots to B at 100, two lots to A at 100, and
reports one initially unrouted lot. Successful completion includes a `RESULT`
line with six filled lots, exact `notional=0.5970000`, and a 99.50 VWAP.

```text
PARENT id=DEMO-1 side=BUY symbol=BTC-USDT requested_lots=7 limit=100.0000
CHILD cl_ord_id=DEMO-1-B venue=B quantity_lots=4 limit=100.0000
CHILD cl_ord_id=DEMO-1-A venue=A quantity_lots=2 limit=100.0000
RESULT requested_lots=7 planned_lots=6 initial_unrouted_lots=1 filled_lots=6 ioc_canceled_lots=0 rejected_lots=0 total_remainder_lots=1 notional=0.5970000 notional_price4_lots=5970000 vwap=99.5000
```

## CLI and outcomes

Run `mockx_sor --help` for every option. Prices accept at most four fractional
digits and are stored as integer ITCH Price(4). `--quantity-lots` is a positive
32-bit share/lot count; `--lot-size` converts it exactly to FIX `OrderQty`
without floating point.

Exit codes are: `0` completed route (including a normal remainder), `2`
configuration/CLI error, `3` feed failure, `4` FIX/logon/report timeout, `5`
internal invariant failure, and `6` rejected child.
