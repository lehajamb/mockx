# Development and Troubleshooting

Use a checkout matching the wiki's documented revision when reproducing an example. Application contracts are tested in the repository; the wiki describes published `main`, not unmerged feature branches.

## Python development workflow

Run commands from the repository root. On this project's Windows Conda setup, initialize and activate the environment in the same PowerShell session before invoking Python:

```powershell
. "C:\Users\lehaj\anaconda3\shell\condabin\conda-hook.ps1"
conda activate mockx
python -m pytest -q
```

The first path is specific to the repository maintainer's installation; use your own Conda installation path on another machine. For a fresh environment, follow [Getting Started](https://github.com/lehajamb/mockx/wiki/Getting-Started), install the native QuickFIX prerequisites, and run `python -m pip install -r requirements.txt`. This revision runs modules from the repository root and has no package metadata for an editable installation.

Useful focused checks are:

```powershell
python -m pytest -q tests/test_readme_examples.py tests/test_time_control_api.py tests/test_api_lifecycle.py
python -m pytest -q tests/test_order_book_invariants.py tests/test_execution_reports.py tests/test_scheduler_failures.py
python -m pytest -q tests/test_replace_core.py tests/test_replace_fix.py tests/test_replace_properties.py
python -m pytest -q tests/test_persistence_run_failure.py tests/test_persistence_retry_backoff.py
```

Then run the full validation prescribed by the README:

```powershell
python -m compileall -q src tests examples
python -m pytest -q
docker compose config
```

`docker compose config` validates configuration rendering; it does not prove that the Docker stack can start or that multicast reaches a host client. Python tests are not a substitute for the separate C++ builds and opt-in live integration tests. Record unavailable dependencies and skipped checks instead of claiming they passed.

## Native examples

The feed client and smart order router have their own CMake/CTest suites. For example, with a C++20 toolchain and CMake installed:

```powershell
cmake -S examples/cpp_market_feed -B examples/cpp_market_feed/build
cmake --build examples/cpp_market_feed/build --config Release
ctest --test-dir examples/cpp_market_feed/build -C Release --output-on-failure
```

The SOR additionally requires installed QuickFIX C++. Its configuration never downloads that dependency automatically. Follow [C++ Market Feed Client](https://github.com/lehajamb/mockx/wiki/Cpp-Market-Feed-Client) and [C++ Smart Order Router](https://github.com/lehajamb/mockx/wiki/Cpp-Smart-Order-Router) for platform-specific builds and live-test flags.

## Change discipline

Add a failing regression test before fixing a reported defect. Keep book, order-state, immutable-report, scheduler-ordering, and persistence-identity invariants intact. Run targeted tests and the full suite after changing behavior.

MockX has no external production clients; repository tests are its consumers. Intentional contract changes should update implementation, tests, examples, README, configuration documentation, and wiki together. Do not preserve obsolete compatibility paths solely for hypothetical consumers, and do not silently alter FIX, REST, or WebSocket behavior.

The [Matching Engine and Scheduler](https://github.com/lehajamb/mockx/wiki/Matching-Engine-and-Scheduler) page lists the core correctness invariants. Refer to the repository's `AGENTS.md` for the complete change instructions.

## Diagnose readiness first

Read `/readyz` and `/stats` together. `/healthz` can succeed while traffic readiness is false.

| Symptom | Inspect | Action |
|---|---|---|
| `/healthz` is 200, `/readyz` is 503 | `components`, `core_ready`, `traffic_ready`, `persistence_required` | Check lifecycle/scheduler state and enabled QuestDB health; confirm QuickFIX loop/acceptor startup |
| Scheduler is `failed` | `/stats.scheduler.dead_letter` and original exception logs | Diagnose the handler, end the run, and start a fresh run; do not retry the dead-lettered event |
| Lifecycle is `FAILED` after destructive reset | Startup/reset failure logs | Correct the underlying failure and restart the process |
| Lifecycle is `RUN_FAILED` | `/stats.run_failure` and run persistence outcomes | Restore the sink, end the failed run, then rerun input in a fresh session |
| Session end returns `METRICS_FLUSH_FAILED` | Buffered/unflushed records and sink health | Restore QuestDB and retry end; retained records and the open run are preserved |
| Start returns `RUN_REQUIRES_END` | Current failed-run identifiers | End the failed run before starting another |

When a request was canceled or timed out during start/end, inspect state before retrying. Once a lifecycle transition is accepted, it completes under its owned task despite caller cancellation. An apparent timeout can therefore leave a successfully started or ended session.

## FIX logs on but no report arrives

1. Check that a simulation is active and that the client targets the chosen instance's FIX port and component IDs.
2. The default clock is external. Advance far enough to cover requester session, inbound, engine, and outbound latency.
3. Inspect `/stats.fix_delivery`, including queue state and the oldest pending age.
4. A `RETRY_PENDING` head can retry at the next logon. A `PERMANENTLY_FAILED` serialization head remains blocking across reconnect and run reset; correct the underlying serialization issue, then restart the process to clear it. There is no public in-place recovery endpoint.

For in-process tests submitting through QuickFIX callbacks, await `exchange.fix_app.wait_for_ingress()` before directly advancing the scheduler. Public administrative advancement already captures and waits for ingress.

A logged-on FIX session alone does not advance simulated time. An HTTP connection to the API is also not an order-entry session: live orders must be sent through FIX.

## Browser fallback and market-data continuity

The browser can show `HTTP fallback` while WebSocket heartbeats continue. Heartbeats do not count as fresh market data; REST polling begins when the viewer's market-data freshness timeout expires.

A valid market-data frame disables fallback, even if it duplicates an earlier frame. The bundled viewer also accepts a lower sequence and can display older state; it does not provide a sequence-regression guard. Global sequence gaps alone do not trigger fallback because other symbols and snapshot conflation can legitimately create gaps.

WebSocket has no guaranteed replay across disconnect or process restart. GLIMPSE recovery is bounded and process-local. After restarting MockX, request a fresh sequence-1 GLIMPSE snapshot rather than attempting to resume an old spin. See [WebSocket Market Data](https://github.com/lehajamb/mockx/wiki/WebSocket-Market-Data) and [ITCH and GLIMPSE](https://github.com/lehajamb/mockx/wiki/ITCH-and-GLIMPSE).

## Updating the wiki

Canonical Markdown lives in `doc/wiki`. Keep commands, wire examples, and configuration descriptions aligned with the source revision documented in the footer. When the source revision changes, review affected pages against implementation and contract tests; do not import pending local behavior into a wiki that still claims to describe published `main`.

Submit source updates through a documentation-only PR, validate navigation/anchors/code fences and relevant examples, then publish the identical Markdown files to the separate GitHub wiki Git repository. Publication steps and initialization/access recovery are maintained in `doc/wiki-maintenance.md`. Preserve unrelated wiki pages and use normal Git history rather than force-pushing.

The repository's interactive HTML documentation remains available through GitHub Pages. Its deployment is separate from the wiki, so updating one destination does not automatically publish the other.

## Sources

Pinned [repository instructions](https://github.com/lehajamb/mockx/blob/556e28b533171ceee63864e396faedfb2ef7aa8e/AGENTS.md), [README troubleshooting](https://github.com/lehajamb/mockx/blob/556e28b533171ceee63864e396faedfb2ef7aa8e/README.md#15-troubleshooting), [executable documentation tests](https://github.com/lehajamb/mockx/blob/556e28b533171ceee63864e396faedfb2ef7aa8e/tests/test_readme_examples.py), and [lifecycle cancellation tests](https://github.com/lehajamb/mockx/blob/556e28b533171ceee63864e396faedfb2ef7aa8e/tests/test_lifecycle_cancellation.py).
