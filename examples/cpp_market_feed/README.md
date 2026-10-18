# MockX C++20 GLIMPSE/ITCH market-feed example

`mockx_market_feed` is a dependency-free C++20 command-line client for the
MockX GLIMPSE 5.0 and TotalView-ITCH 5.0 interfaces. It joins the MoldUDP64
multicast stream first, takes an atomic order-by-order snapshot through
SoupBinTCP, and applies buffered ITCH beginning at the sequence carried by the
GLIMPSE `G` message.

The client maintains every resting order but prints an aggregated top-N book.
All prices remain integer ITCH Price(4) values and all quantities remain ITCH
shares, which MockX maps one-to-one to its internal lots.

## Data path

The UDP thread reserves a fixed ring slot and passes that slot directly to
`recvfrom`; accepted datagrams are never copied again in user space. The book
thread validates each Mold and ITCH length, begins the trivial wire object's
lifetime in that receive storage, and obtains the typed byte-only view with
`reinterpret_cast`. Big-endian integers are decoded byte by byte, so the code
does not perform unaligned native integer loads.

The ingress queue is a bounded single-producer/single-consumer ring using
lock-free 64-bit indices and acquire/release publication. Console I/O runs on
a third thread through a separate best-effort display ring. Ring saturation,
truncated UDP, malformed input, or a forward sequence gap invalidates
continuity and causes a new GLIMPSE cut; the client never knowingly continues
with a stale book.

MockX currently changes the live book with `A`, `E`, `C`, `D`, and `X`. The client
also consumes `R`, `H`, `S`, and the GLIMPSE-only `G`. Any other ITCH type is a
hard error because skipping a future book-changing message would make the
display incorrect.

Native FIX replacement preserves the order's FIX `OrderID` while treating
displayed references independently. Same-price quantity reductions publish
ITCH `X` with the number of removed shares and preserve FIFO/reference.
The client subtracts those shares and removes a zero-leaves order, rejecting
unknown references, zero reductions, and reductions larger than current leaves.
Price changes and quantity increases publish old-reference `D`, passive `E/C`
for any new executions, then fresh-reference `A` if a remainder rests. Deleted
references are never reused within a Mold session. GLIMPSE contains the current
displayed references, so live replay and recovery preserve the same FIFO book.
No-op and rejected replacements produce no book update.

## Build and test

Windows, from an x64 Visual Studio developer prompt:

```powershell
cmake -S examples/cpp_market_feed -B examples/cpp_market_feed/build
cmake --build examples/cpp_market_feed/build --config Release
ctest --test-dir examples/cpp_market_feed/build -C Release --output-on-failure
```

Linux with GCC or Clang:

```bash
cmake -S examples/cpp_market_feed -B examples/cpp_market_feed/build \
  -DCMAKE_BUILD_TYPE=Release
cmake --build examples/cpp_market_feed/build --parallel
ctest --test-dir examples/cpp_market_feed/build --output-on-failure
```

To run AddressSanitizer and UndefinedBehaviorSanitizer on Linux, configure with
`-DMOCKX_FEED_ENABLE_SANITIZERS=ON`. ThreadSanitizer can be selected with the
compiler's usual `-fsanitize=thread` compile and link flags.

An opt-in end-to-end CTest starts a real MockX session, completes GLIMPSE,
applies a later ITCH update, and verifies clean end-of-run handling.
Configure from the MockX Conda environment with
`-DMOCKX_FEED_ENABLE_LIVE_TEST=ON`; the normal build leaves it disabled so the
C++ unit tests remain dependency-free.

## Enable MockX feeds

Use a MockX configuration containing:

```yaml
itch:
  enabled: true
  market_category: " "
  financial_status: " "
  round_lot_size: 1
moldudp64:
  multicast_group: 239.192.0.1
  multicast_port: 18000
  session_prefix: MX
  heartbeat_interval_ms: 1000
  max_payload_bytes: 1400
  interface_address: 0.0.0.0
  ttl: 1
glimpse:
  enabled: true
soupbintcp:
  host: 127.0.0.1
  port: 18001
  session_prefix: GS
  credentials:
    GLIMP: CHANGE_ME
  login_timeout_ms: 30000
  write_timeout_ms: 5000
  max_connections: 64
  replay_ttl_ms: 60000
  max_cached_sessions: 32
  max_cached_bytes: 67108864
```

Start MockX and create an active simulation session before starting the client:

```bash
curl -f -X POST http://127.0.0.1:8001/api/v1/session/start \
  -H "Content-Type: application/json" \
  -d '{"session_id":"cpp-feed-demo"}'
```

## Run

Set the password without exposing it in the process list, then run the client:

```powershell
$env:MOCKX_GLIMPSE_PASSWORD = "CHANGE_ME"
examples\cpp_market_feed\build\Release\mockx_market_feed.exe --symbol BTC-USDT
```

```bash
export MOCKX_GLIMPSE_PASSWORD=CHANGE_ME
examples/cpp_market_feed/build/mockx_market_feed --symbol BTC-USDT
```

Use `--datagram-bytes` at least as large as MockX's
`moldudp64.max_payload_bytes`. Omit `--symbol` to display every instrument, or
repeat it to select several symbols. Run `mockx_market_feed --help` for all
network, display, ring, and recovery options.

Mold end-of-session, ITCH End of Messages, and Ctrl+C return zero. Invalid CLI, authentication,
socket setup, and protocol/recovery failures have distinct nonzero exit codes.
The example deliberately does not implement Mold retransmission requests;
fresh GLIMPSE snapshots are its recovery mechanism.
