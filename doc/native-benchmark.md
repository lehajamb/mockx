# Native exchange submission benchmark

[`scripts/benchmark_native_exchange.py`](../scripts/benchmark_native_exchange.py)
compares sequential `NewOrderSingle` (`D`), pipelined `D`, and native
`NewOrderList` (`E`) through the required native exchange stack. The recorded
24-case run is in
[`scripts/native_benchmark_results.json`](../scripts/native_benchmark_results.json).

## Reproduce

From an installed MockX environment at the repository root:

```powershell
python scripts/benchmark_native_exchange.py --depths 100,1000 --orders 100 --window 100 --sink-delays-ms 0,5 --dashboard-every 0,100 --output scripts/native_benchmark_results.json
```

Use `--questdb-url http://localhost:9000` to write to a real QuestDB instance.
Each case receives a fresh run UUID. The recorded matrix used the acknowledged
memory sink. Additional real QuestDB measurements are recorded below.

Real QuestDB mode reads `QUESTDB_HEALTH_URL`, `QUESTDB_USERNAME`,
`QUESTDB_PASSWORD`, and `QUESTDB_REQUEST_TIMEOUT_MS`. The health URL defaults
to the command-line URL, credentials default to absent and must be supplied
together, and the request timeout defaults to 10,000 ms. The same settings
apply to its journal-reader HTTP requests. Other exchange environment overrides
are excluded so the benchmark workload and writer limits stay controlled.

## Workload and measurement

Each case seeds an uncrossed book with 100 or 1,000 distinct price levels per
side, one order of ten lots per level. The timed workload adds 100 passive
one-lot buy orders to existing levels. Seeding, startup, and final session
checkpoint creation are excluded. Timing includes FIX parsing, ingress,
matching, immutable reports, native journal admission, ITCH encoding and
packetization, and the final persistence acknowledgment.

Sequential `D` advances the exchange after each order. Pipelined `D` and native
`E` use windows of 100 orders. Network and engine latency settings are zero.
Every case checks acknowledgments, live order counts, full book invariants,
and a digest of the final price levels, FIFO order, and leaves quantities.
The three submission modes produced identical digests in all eight scenarios.

The FIX acceptor is replaced by an in-process report collector. Native
MoldUDP64 packetization is exercised, but packets are counted instead of sent
over UDP. GLIMPSE starts its real listener; no snapshot client is connected.
The persistence writer flushes at 256 rows or 256 KiB, with capacity limits of
4,096 rows and 4 MiB. The scheduler processes 128 events per cooperative batch.
The slow-sink cases add five milliseconds to each acknowledged write.

Dashboard load polls acknowledged journal rows every 100 submitted orders and
uses the external `NativeJournalReader` to reconstruct and read forty levels.
It never calls the engine snapshot API. For this benchmark the reader shares
the producer process, so process CPU includes projection work. Real QuestDB
mode queries the database; memory mode reads acknowledged rows from the sink.
Neither mode measures browser rendering or an independently scheduled viewer.

## Recorded comparison

Rounded orders per second, including the final writer flush:

| Levels per side | Sink delay per write | Journal dashboard polling | Sequential D | Pipelined D | Native E |
| ---: | ---: | --- | ---: | ---: | ---: |
| 100 | 0 ms | Off | 198 | 191 | 197 |
| 100 | 0 ms | Every 100 orders | 161 | 231 | 179 |
| 100 | 5 ms | Off | 135 | 196 | 226 |
| 100 | 5 ms | Every 100 orders | 173 | 165 | 241 |
| 1,000 | 0 ms | Off | 73 | 50 | 59 |
| 1,000 | 0 ms | Every 100 orders | 71 | 86 | 78 |
| 1,000 | 5 ms | Off | 69 | 88 | 73 |
| 1,000 | 5 ms | Every 100 orders | 73 | 81 | 85 |

For the same 100-order workload, sequential and pipelined `D` each created
100 physical FIX messages and 900 journal rows; `E` created one physical FIX
message and 801 journal rows. All modes created 100 ITCH messages. Sequential
submission sent 100 counted packets, while both windowed modes sent four.
The largest observed writer occupancy across the matrix was 301 rows and
127,675 bytes, including queued and in-flight records.

The JSON also records producer process CPU, engine command CPU, sink write
counts, logical journal byte estimates, and projection lag at the last timed
poll. Logical bytes are admission-accounting estimates, not HTTP or disk
bytes. The lag can remain nonzero because the final flush occurs after the
last dashboard poll.

## Conditions and limits

This run used Python 3.13.14 on Windows 11, build 26300, on 14 October 2026.
No Python test suite ran concurrently; a Docker build may have competed for
host resources. Each case was measured once, without CPU isolation or a
statistical warmup/repetition protocol. The timing spread therefore does not
establish a universal throughput advantage for any submission mode.

The stable reductions in FIX message count, journal rows, and packet count
show the intended batching behavior. Core matching and the full correctness
audit still run for every order, so batching does not eliminate their cost.
This matrix covers passive adds; it does not measure fills, replace races,
failure recovery, actual FIX network throughput, QuestDB durability latency,
or a separately running dashboard. Use real QuestDB mode and repeated runs
on an otherwise idle host before setting capacity expectations.

## Real QuestDB and separate projector

Two additional 12-case matrices used QuestDB 9.4.3 in Docker, with HTTP
transactions acknowledged by the server. Results are in
[`native_questdb_benchmark_without_dashboard.json`](../scripts/native_questdb_benchmark_without_dashboard.json)
and
[`native_questdb_benchmark_with_dashboard.json`](../scripts/native_questdb_benchmark_with_dashboard.json).
The second matrix ran `examples/dashboard/projector.py` in a separate Python
process with its 250 ms interval. Producer CPU in these files excludes the
projector process. The projector queried the same QuestDB instance and wrote
forty-level display projections; Grafana/browser rendering was not running.

Rounded orders per second, including the final writer acknowledgment:

| Levels per side | Added write delay | External projector | Sequential D | Pipelined D | Native E |
| ---: | ---: | --- | ---: | ---: | ---: |
| 100 | 0 ms | Off | 164 | 162 | 165 |
| 100 | 0 ms | On | 97 | 119 | 140 |
| 100 | 5 ms | Off | 142 | 149 | 201 |
| 100 | 5 ms | On | 109 | 114 | 121 |
| 1,000 | 0 ms | Off | 34 | 33 | 38 |
| 1,000 | 0 ms | On | 42 | 32 | 37 |
| 1,000 | 5 ms | Off | 35 | 36 | 38 |
| 1,000 | 5 ms | On | 34 | 34 | 37 |

The three submission modes produced identical FIFO/leaves digests in every
scenario. Peak occupancy was 351 rows / 150,504 logical bytes without the
projector and 228 rows / 98,449 bytes with it. Individual JSON records include
producer and engine CPU, write counts, packet counts and logical bytes.
All twelve runs in the projector matrix received depth samples and reached
an `ENDED` cursor at their journal head. The projector produced 719 depth
samples for those runs. Its separate-process configuration and verification
are recorded in
[`native_questdb_benchmark_context.json`](../scripts/native_questdb_benchmark_context.json).

To reproduce, use the authentication settings above and run the following in
separate terminals against a native-schema QuestDB instance:

```powershell
python examples/dashboard/projector.py --questdb-url http://localhost:9000 --interval-ms 250
python scripts/benchmark_native_exchange.py --questdb-url http://localhost:9000 --depths 100,1000 --orders 100 --sink-delays-ms 0,5 --dashboard-every 0 --output scripts/native_questdb_benchmark_with_dashboard.json
```

Run the benchmark with the projector stopped for the comparison matrix.
These measurements used the same Windows/Python environment as the memory
matrix, with a local Docker database. The projector also discovered earlier
completed runs retained in that test database. Cases were measured once,
without CPU isolation; caching and host contention can improve or worsen a
later case. The results verify bounded buffering and business equivalence
under real persistence and independent projection load, rather than establish
a statistically reliable throughput or dashboard overhead estimate.
