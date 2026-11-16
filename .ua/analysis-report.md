# MockX architecture analysis

MockX is a deterministic FIX-speaking mock exchange for strategy testing and simulation, combining an exact price-time order book and controlled clock with REST administration, immutable WebSocket market data, optional QuestDB persistence, and experimental ITCH/GLIMPSE feeds. Note: this project has over 100 source files; consider scoping analysis to a subdirectory for faster results.

Analyzed commit: `e45c06f1bb6d8bddade3b89ed462f036354a8a19`.

- Files: **251/251**
- Nodes: **2291**
- Relationships: **7621**
- Architectural layers: **8**
- Guided tour: **15 steps**
- Code lessons: **30 detailed**, **221 overview-only**

## Files by category

| Type | Count |
|---|---:|
| config | 25 |
| code | 182 |
| infra | 5 |
| docs | 30 |
| markup | 7 |
| script | 2 |

## Nodes by type

| Type | Count |
|---|---:|
| file | 191 |
| function | 1666 |
| class | 373 |
| service | 5 |
| pipeline | 1 |
| document | 30 |
| config | 25 |

## Relationships by type

| Type | Count |
|---|---:|
| contains | 2584 |
| exports | 1552 |
| imports | 510 |
| calls | 2394 |
| tested_by | 297 |
| inherits | 16 |
| configures | 29 |
| deploys | 8 |
| depends_on | 87 |
| documents | 139 |
| related | 5 |

## Architectural layers

- **Matching and deterministic time** (7 file-level nodes): Authoritative price-time matching, indexed FIFO order books, immutable commands and reports, exact tick/lot arithmetic, validation, generation identifiers, and inclusive deterministic scheduling.
- **FIX ingress and exchange lifecycle** (7 file-level nodes): Process composition and CLI startup, stable-session ownership, ordered FIX command admission, report serialization and retained delivery, and atomic generation-fenced session transitions.
- **Administration and market data** (9 file-level nodes): Typed REST administration, readiness and statistics endpoints, bounded WebSocket delivery and its browser view, plus ITCH/MoldUDP64 publication and GLIMPSE/SoupBinTCP snapshot recovery.
- **Metrics and persistence** (2 file-level nodes): Run and lifetime counters, exact trade totals, latency observations, immutable metric event identity, bounded asynchronous buffering, and acknowledged QuestDB persistence.
- **Trading clients and replay** (47 file-level nodes): Python FIX order and live-market examples, historical CoinAPI replay and native-feed qualification, the C++ recovered market-feed consumer, and the two-venue smart order router.
- **Tests and qualification** (106 file-level nodes): Python unit, property, integration, concurrency and performance suites, reusable test fixtures, native client tests and live smoke checks, and the existing coverage measurement artifact.
- **Configuration and deployment** (40 file-level nodes): Strict runtime configuration models, exchange and FIX settings, the FIX dictionary, dependency and CMake manifests, container/service definitions, health scripts, documentation deployment, and development-tool integration.
- **Documentation and architecture guides** (34 file-level nodes): Project and example guides, repository development instructions, wiki references, replay validation notes, and the interactive architecture, matching and scheduler documentation assets.

## Guided tour

1. **Exchange Purpose and Contracts** — Start with the README to establish what MockX simulates and which public interfaces it supports. FIX carries order entry, REST controls simulation administration, and WebSocket plus optional native feeds expose committed market data. Keep the documented matching, timing, ownership and immutable-report guarantees in view as you follow the implementation.
2. **Exact Values and State** — The shared models define the commands, mutable orders, FIFO books and immutable reports used throughout the exchange. Numeric helpers and instrument metadata translate decimal wire values into exact integer ticks and lots, then format results without changing authoritative quantities. These files supply the vocabulary and accounting rules needed to understand every later subsystem.
3. **Configuration and Process Ownership** — With the domain types established, follow a concrete local configuration through strict validation and the CLI into MockExchange. The composition root owns the engine, scheduler, gateway, API, metrics and native feeds, including their startup, readiness and shutdown. Reading these files together shows where runtime policy is selected and where component references are wired.
4. **Price-Time Matching** — The matching engine applies the command and quantity rules introduced earlier to authoritative order books. Follow submission through price/FIFO matching, participant self-cross prevention and the treatment of market or IOC remainders. Cancellation and native replacement share this state, while each completed action produces immutable execution reports and committed displayed-order events.
5. **Deterministic Simulated Time** — The scheduler determines when engine commands and delivery events run, so simulated latency becomes part of reproducible exchange behavior. Equal timestamps retain FIFO order, advancement includes events exactly at the target, and a failed generation retains dead-letter diagnostics. Real monotonic CPU durations remain separate from the simulated timestamps used by the business workflow.
6. **Atomic Session Boundaries** — Session lifecycle control coordinates the engine and scheduler as one simulation generation. It serializes start/end transitions, administrative advancement, ingress and snapshot capture so consumers observe stable boundaries. Accepted transitions are protected from cancellation, and metric loss can fail a run at a command boundary rather than leave ambiguous progress.
7. **FIX Ingress and Delivery** — Now follow the public order-entry path from QuickFIX callbacks into generation-qualified, ordered commands. Stable session ownership and ClOrdID claims keep routing and duplicate handling tied to the correct participant context. The retained delivery queue carries immutable logical responses through per-session FIFO ordering, versioned deadlines and bounded retry cycles.
8. **Administration and WebSocket Data** — The API exposes the lifecycle controls and committed state developed in the previous steps through typed REST, readiness, metrics and browser routes. WebSocket connections receive globally sequenced snapshots through bounded per-client buffers. Dedicated sender tasks and snapshot conflation isolate slow consumers while preserving the initial snapshot and error/close ordering.
9. **Native ITCH Publication** — The same committed displayed-order events can drive the optional downstream ITCH feed. The publisher owns run-scoped announcements and heartbeats, the encoder validates binary field representations, and the MoldUDP64 transport packs bounded multicast datagrams. Message-count sequencing connects these layers and gives downstream consumers a continuity boundary to track.
10. **Recovering a Feed Snapshot** — GLIMPSE complements live multicast with an authenticated snapshot recovery service over SoupBinTCP. Its listener outlives individual runs, while cached snapshot sessions and interrupted-spin replay remain bounded and generation-scoped. Trace the framed login, data and heartbeat controls to see how a client obtains a stable recovery cut before continuing with the live feed.
11. **Metrics and Persistence** — Metrics observe the matching, timing and delivery boundaries introduced so far, keeping event-owned counters and exact trade totals. Each persisted logical event has an immutable identity, and a bounded writer manages asynchronous admission, retries and final draining. The QuestDB adapter requires acknowledged writes and fixed WAL schemas so timestamp collisions and retries do not erase distinct events.
12. **Historical Market Replay** — The replay example puts the exchange contracts to work with historical CoinAPI order-book data. Source parsing preserves exact ticks, lots, nanosecond timestamps and order lifetimes without exposing future state. The runner then coordinates paced releases, FIX acknowledgements and observed market data, producing quality and timing results for the completed replay.
13. **Native Consumers and Routing** — The C++ examples exercise the native feed from the consumer side of the exchange boundary. The feed handler combines a bounded multicast ring with GLIMPSE recovery and sequence stitching, then publishes reconstructed depth. The smart order router uses ready venue depth to plan price-prioritized sweeps with parent limits, exact lot quantities and deterministic venue tie-breaking.
14. **Proving the Invariants** — Return to the core guarantees with tests that deliberately corrupt cached book state and generate long command histories. The invariant suite checks independent consistency properties rather than relying only on example outcomes. Generated replacement histories extend those checks across revisions and displayed liquidity, connecting the engine behavior to the feed contracts.
15. **Running the Complete System** — Finish by connecting the components to a runnable demonstration. The Docker image packages Python, native QuickFIX dependencies, the application and its protocol assets, while Compose brings together QuestDB, MockX, the multicast relay and the continuous FIX driver. Health dependencies gate traffic until the required services are ready, making the startup ownership from Step 3 concrete.

## Validation

Every scanned file and all 510 resolved internal imports are present. Node IDs, edge endpoints, layer membership, tour references, source ranges, and detailed lesson ranges were checked. Final graph and lesson validation returned no issues.

Repository tests: **1032 passed, 1 skipped**.

## Review corrections and limitations

- All 38 test-to-production coverage edges were correctly flipped to production-to-test. No node-ID, node-type or complexity remapping was necessary.
- Of the 25 dropped tested_by edges, 12 targeted fixture helper files and 7 targeted a throughput benchmark; these remain omitted because import dependencies are already represented and they are not standalone regression tests.
- Restored 6 tested_by edges to examples/cpp_market_feed/tests/live_smoke.py and examples/cpp_sor/tests/live_smoke.py. Both files are genuine native integration tests already tagged test, and each imports configuration.py, main.py and models.py in the authoritative importMap. The deterministic linker missed their unconventional filenames.
- Corrected the test_config helper summary to describe configuration construction rather than imply it is an assertion test; deterministic extraction identifies its call to validate_config.
- All 251 scan files and all 510 authoritative import relationships are present. No dangling endpoints, malformed required node fields or missing analyzed files remain.
- Known limitations: binary coverage artifact retained as a file node; optional protected tooling source inspection relied on bundled structure; native network source required source-backed supplementary symbols because bundled extraction returned no definitions.

- Batcher split a 40-file community at the 35-file limit; modularity may decrease.
- Neighbor context for models.py capped at 50 cross-batch entries; 18 entries omitted from context only, all resolved imports retained.
- Scanner included repository tooling files and .coverage; coverage is represented as a binary artifact, not source.
- Conda activation failed with Windows DLL relocation; merge succeeded with system Python 3.14. The mockx environment Python 3.13.14 was verified directly for subsequent Python steps.
- Supplemental direct inspection of .trail/hook.cjs was blocked by Trail tooling; bundled structural extraction still represented the file.
- Sandboxed pytest stalled in Windows asyncio socketpair setup. Outside the sandbox, all four API lifecycle tests and the full suite passed (1032 passed, 1 skipped).
- The version-pinned viewer launched successfully on localhost port 5174; browser verification displayed all eight architectural layers.
- Dashboard freshness reports one uncommitted working-tree file: the new Trail learning about native smoke-test coverage. It was created after scanning; no application source changed.
- Viewer emitted an upstream Node DEP0190 deprecation warning during browser launch; the dashboard remained operational.

Eight independent tooling, artifact, or marker files have no relationship edges; each remains represented and assigned to a layer:

- Node 'config:.claude/settings.json' has no edges (orphan)
- Node 'file:.coverage' has no edges (orphan)
- Node 'config:.grok/config.toml' has no edges (orphan)
- Node 'document:.grok/skills/trail/SKILL.md' has no edges (orphan)
- Node 'document:.trail/README.md' has no edges (orphan)
- Node 'config:.ua/config.json' has no edges (orphan)
- Node 'file:examples/cpp_sor/runtime/.gitkeep' has no edges (orphan)
- Node 'file:tests/__init__.py' has no edges (orphan)

## Artifacts

- `knowledge-graph.json`: interactive architecture graph and guided tour.
- `code-lessons.json`: source-aligned explanations.
- `fingerprints.json`: structural baseline for incremental analysis.
- `meta.json`: analyzed revision and file count.
- `intermediate/scan-result.json`: preserved deterministic file inventory and imports.

Application source files were not changed.
