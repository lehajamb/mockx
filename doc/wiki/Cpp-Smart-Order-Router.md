# C++ Smart Order Router

`mockx_sor` is a C++20 example that routes one limit parent across two independent MockX venues. It synchronizes both GLIMPSE/ITCH books, chooses eligible displayed liquidity by price, sends at most one IOC limit child per venue through FIX 4.4, aggregates terminal reports, and exits.

It is a focused simulator client: it does not model fees, predict latency, seek hidden liquidity, replace orders, or chase an unfilled IOC remainder.

## Build prerequisites

Use CMake 3.20 or newer, a C++20 compiler, and an installed QuickFIX C++ library. The Python `quickfix` package alone does not satisfy the native C++ prerequisite. CMake searches the active vcpkg toolchain, pkg-config, normal install prefixes, and `QUICKFIX_ROOT`; it does not download QuickFIX source during configuration.

From an x64 Visual Studio developer prompt, one installation path is vcpkg (adjust its installation path):

```powershell
vcpkg install quickfix:x64-windows
cmake -S examples/cpp_sor -B examples/cpp_sor/build -DCMAKE_TOOLCHAIN_FILE=C:/vcpkg/scripts/buildsystems/vcpkg.cmake
cmake --build examples/cpp_sor/build --config Release
ctest --test-dir examples/cpp_sor/build -C Release --output-on-failure
```

On Linux with a source-installed QuickFIX prefix:

```bash
cmake -S examples/cpp_sor -B examples/cpp_sor/build -DQUICKFIX_ROOT=/opt/quickfix -DCMAKE_BUILD_TYPE=Release
cmake --build examples/cpp_sor/build --parallel
ctest --test-dir examples/cpp_sor/build --output-on-failure
```

Normal C++ tests do not start MockX. `-DMOCKX_SOR_ENABLE_LIVE_TEST=ON` enables the two-venue end-to-end test and requires configuring from the MockX Python environment. On Linux, `-DMOCKX_SOR_ENABLE_SANITIZERS=ON` enables AddressSanitizer and UndefinedBehaviorSanitizer.

## Start the two venues

The SOR example has its own Compose stack, separate from the repository-root live market. From the repository root:

```text
docker compose -f examples/cpp_sor/docker-compose.yml up --build
```

It starts two exchanges, two Docker-to-host MoldUDP64 relays, and a one-shot FIX seeder. Wait for `seed-books` to print this line and exit with code 0:

```text
SOR books ready: Venue A asks 0.002@100.00, 0.009@102.00; Venue B asks 0.003@99.00, 0.001@100.00
```

The exchanges and relays remain running. For detached operation and bootstrap diagnostics:

```text
docker compose -f examples/cpp_sor/docker-compose.yml up --build -d
docker compose -f examples/cpp_sor/docker-compose.yml ps -a
docker compose -f examples/cpp_sor/docker-compose.yml logs seed-books
```

| Endpoint | Venue A | Venue B |
|---|---|---|
| REST API | `localhost:8101` | `localhost:8102` |
| FIX | `localhost:4101` | `localhost:4102` |
| MoldUDP64 | `239.192.10.1:18100` | `239.192.10.2:18200` |
| GLIMPSE | `localhost:18101` | `localhost:18201` |

The fixtures use realtime clocks with a one-millisecond interval. Do not issue manual time advancement against them. The relay setup targets Docker Desktop's `host.docker.internal`; ensure it resolves appropriately when adapting the stack to another Docker host.

The seeder uses HTTP for readiness, lifecycle, and verification, and FIX for all passive orders. `SEED_A` and `SEED_B` map to participants `passive-a` and `passive-b`; SOR sessions map to `sor-demo`. These different owners allow fills without bypassing self-trade prevention.

## Route the demonstration parent

With Windows PowerShell:

```powershell
$env:MOCKX_SOR_A_GLIMPSE_PASSWORD = "SECRET_A"
$env:MOCKX_SOR_B_GLIMPSE_PASSWORD = "SECRET_B"
examples\cpp_sor\build\Release\mockx_sor.exe --config examples/cpp_sor/sor.cfg --parent-id DEMO-1 --symbol BTC-USDT --side BUY --quantity-lots 7 --lot-size 0.001 --limit-price 100.00
```

With Bash:

```bash
export MOCKX_SOR_A_GLIMPSE_PASSWORD=SECRET_A
export MOCKX_SOR_B_GLIMPSE_PASSWORD=SECRET_B
examples/cpp_sor/build/mockx_sor --config examples/cpp_sor/sor.cfg --parent-id DEMO-1 --symbol BTC-USDT --side BUY --quantity-lots 7 --lot-size 0.001 --limit-price 100.00
```

These passwords match the checked-in demo credentials. The CLI quantity is seven lots, equivalent to `0.007` BTC-USDT at the configured lot size. The parent limit excludes Venue A's `102.00` liquidity.

Expected routing and outcome:

| Venue | Eligible liquidity | Child |
|---|---|---|
| B | 3 lots at `99.00`, 1 at `100.00` | IOC buy 4 lots, limit `100.00` |
| A | 2 lots at `100.00` | IOC buy 2 lots, limit `100.00` |
| Unrouted | No further liquidity at or below limit | 1 lot remains |

```text
PARENT id=DEMO-1 side=BUY symbol=BTC-USDT requested_lots=7 limit=100.0000
CHILD cl_ord_id=DEMO-1-B venue=B quantity_lots=4 limit=100.0000
CHILD cl_ord_id=DEMO-1-A venue=A quantity_lots=2 limit=100.0000
RESULT requested_lots=7 planned_lots=6 initial_unrouted_lots=1 filled_lots=6 ioc_canceled_lots=0 rejected_lots=0 total_remainder_lots=1 notional=0.5970000 notional_price4_lots=5970000 vwap=99.5000
```

The six filled lots are `0.006` instrument units. Exact notional is `0.5970000` and VWAP is `99.5000`. A normal remainder is a completed route, not an error.

Before repeating the demo, restore its deterministic books:

```text
docker compose -f examples/cpp_sor/docker-compose.yml run --rm seed-books
```

This bootstrap deliberately ends any current demo sessions before starting `sor-a` and `sor-b`. Use a new `--parent-id` such as `DEMO-2` for the next SOR invocation: MockX's client-order ID namespace survives simulation reset for the same FIX session.

## Run without Docker

Start each exchange in its own terminal, with the Python environment activated:

```text
python -m src.mockx.cli --config examples/cpp_sor/venue_a.yml --fix-config examples/cpp_sor/venue_a_acceptor.cfg
```

```text
python -m src.mockx.cli --config examples/cpp_sor/venue_b.yml --fix-config examples/cpp_sor/venue_b_acceptor.cfg
```

Then seed both books:

```text
python examples/cpp_sor/seed_books.py --fix-config examples/cpp_sor/seed_books.cfg
```

Run the same native SOR command after the seeder succeeds. Manual fixtures publish multicast directly; the Compose relay services are not used in this path.

## Routing, data flow, and failure behavior

Both feeds must be `LIVE` and both FIX sessions logged on before routing. BUY parents merge asks from low to high price; SELL parents merge bids from high to low. Equal prices use `VenuePriority`. All allocations to one venue collapse into one IOC child at that venue's worst selected price. A moving book may cancel some IOC quantity, and the example makes no second pass.

The router reuses the native feed handler. Each venue publishes fixed-capacity, 16-level snapshots through a bounded SPSC ring; overflow and recovery are fail-closed conditions. UDP receive storage and explicit ring handoffs avoid further datagram copying, while QuickFIX uses its own allocation model. The shared feed handler understands native replacement updates, but the SOR itself submits only IOC new orders.

Prices accept at most four fractional digits and use integer ITCH Price(4). `--quantity-lots` is a positive 32-bit share/lot count, and `--lot-size` converts exactly to FIX `OrderQty` without floating point. Run the native executable with `--help` for the full CLI.

| Exit code | Meaning |
|---:|---|
| 0 | Route completed, including a normal remainder |
| 2 | Configuration or CLI error |
| 3 | Feed failure |
| 4 | FIX logon/report timeout or FIX failure |
| 5 | Internal invariant failure |
| 6 | Rejected child |

If a feed cannot become live, check both relay services, host UDP ports 18100/18200, GLIMPSE credentials, and `host.docker.internal`. If FIX fails, compare session IDs and participant mappings with the supplied fixtures. Inspect service logs with:

```text
docker compose -f examples/cpp_sor/docker-compose.yml logs
```

Stop this example's containers and network:

```text
docker compose -f examples/cpp_sor/docker-compose.yml down
```

Sources: [SOR guide](https://github.com/lehajamb/mockx/blob/556e28b533171ceee63864e396faedfb2ef7aa8e/examples/cpp_sor/README.md), [Compose topology](https://github.com/lehajamb/mockx/blob/556e28b533171ceee63864e396faedfb2ef7aa8e/examples/cpp_sor/docker-compose.yml), [router/session configuration](https://github.com/lehajamb/mockx/blob/556e28b533171ceee63864e396faedfb2ef7aa8e/examples/cpp_sor/sor.cfg), [FIX seeder](https://github.com/lehajamb/mockx/blob/556e28b533171ceee63864e396faedfb2ef7aa8e/examples/cpp_sor/seed_books.py). See also [C++ Market Feed Client](https://github.com/lehajamb/mockx/wiki/Cpp-Market-Feed-Client) and [FIX Trading](https://github.com/lehajamb/mockx/wiki/FIX-Trading).
