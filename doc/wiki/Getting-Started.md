# Getting Started

Run a local exchange, submit three orders through FIX, and inspect the resulting book. For a self-running market with QuestDB and market feeds, use the [Docker demo](https://github.com/lehajamb/mockx/wiki/Docker-and-Live-Demo).

## Prerequisites and installation

Use Python 3.11 (the version used by the Docker image), Git, and the native build tools needed by QuickFIX. QuickFIX includes C++ code; a missing compiler or compatible native package is an installation issue, not an exchange configuration error.

Clone the repository and select the version documented by this wiki:

```text
git clone https://github.com/lehajamb/mockx.git
cd mockx
git checkout 556e28b533171ceee63864e396faedfb2ef7aa8e
python -m venv .venv
```

Activate the environment in each terminal that runs Python:

```bash
# Linux/macOS shell
source .venv/bin/activate
```

```powershell
# Windows PowerShell
.venv\Scripts\Activate.ps1
```

Then install the pinned dependencies from the repository root:

```text
python -m pip install -r requirements.txt
```

The repository runs directly from its source tree. Keep the repository root as the working directory so the Python modules, FIX dictionary, templates, and relative configuration paths resolve.

## 1. Start the server

In terminal 1:

```text
python -m src.mockx.cli --config configs/mockx-local.yml --fix-config configs/mockx1.cfg
```

This configuration provides:

| Setting | Value |
|---|---|
| REST, WebSocket, browser UI, metrics | `127.0.0.1:8001` |
| FIX acceptor | Port `4001`, `MOCKX1` to `CLIENT1` |
| Instruments | `BTC-USDT`, `ETH-USDT` |
| Tick / lot sizes | `0.01` / `0.001` |
| Clock | External; advances only when requested |
| Simulated network / engine latency | `0` / `1` millisecond |
| Random rejection probability | `0` |
| QuestDB and binary feeds | Disabled |

For the HTTP commands below, choose the block for your shell. On PowerShell use `Invoke-RestMethod`; the Bash examples assume the actual curl executable.

## 2. Check health and start a simulation

In a separate terminal, check process liveness and traffic readiness, then start the run:

```bash
curl -f http://127.0.0.1:8001/healthz
curl -f http://127.0.0.1:8001/readyz
curl -f -X POST http://127.0.0.1:8001/api/v1/session/start \
  -H 'Content-Type: application/json' -d '{"run_id":"quickstart"}'
```

```powershell
Invoke-RestMethod http://127.0.0.1:8001/healthz
Invoke-RestMethod http://127.0.0.1:8001/readyz
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8001/api/v1/session/start -ContentType 'application/json' -Body '{"run_id":"quickstart"}'
```

A simulation session is separate from a FIX connection. FIX logon alone does not start a run. A second start while the run is active returns `409 SESSION_ALREADY_ACTIVE`.

## 3. Submit FIX orders

In terminal 2, with the Python environment activated:

```text
python examples/submit_orders.py --fix-config examples/fix_client.cfg --symbol BTC-USDT --timeout 30
```

Wait until the client prints `Orders are queued`. It sends a sell of `5.000` at `100.00`, a buy of `2.000` at `100.00`, and a buy of `1.000` at `99.00`. All three belong to the same participant. The crossing pair deliberately demonstrates mandatory self-trade prevention.

## 4. Advance simulated time

While the client waits for reports, use terminal 3:

```bash
curl -f -X POST http://127.0.0.1:8001/admin/advance-time \
  -H 'Content-Type: application/json' -d '{"milliseconds":10}'
```

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8001/admin/advance-time -ContentType 'application/json' -Body '{"milliseconds":10}'
```

The request processes events at or before its target. With the supplied configuration, 10 milliseconds covers the queued orders and their report delivery. Advancing before the client submits its orders does not process those future arrivals; wait for the message above or advance again if necessary.

Expected result:

- The first sell and crossing buy finish canceled by self-trade prevention; no trade is created.
- The final buy receives `NEW` and rests at `99.00` for `1.000`.
- The client exits successfully after observing the expected outcomes. A timeout exits unsuccessfully and prints diagnostic guidance.

## 5. Inspect and finish

```bash
curl -f http://127.0.0.1:8001/book/BTC-USDT
curl -f http://127.0.0.1:8001/stats
curl -f http://127.0.0.1:8001/metrics
curl -f -X POST http://127.0.0.1:8001/api/v1/session/end
```

```powershell
Invoke-RestMethod http://127.0.0.1:8001/book/BTC-USDT
Invoke-RestMethod http://127.0.0.1:8001/stats
Invoke-WebRequest http://127.0.0.1:8001/metrics | Select-Object -ExpandProperty Content
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8001/api/v1/session/end
```

Before ending, open [the book UI](http://127.0.0.1:8001/ui/BTC-USDT). The book contains one bid at `99.00`, quantity `1.000`, and no asks. Prices and quantities are decimal strings, preserving exact tick/lot values.

Session end returns final run statistics. The process stays up; start another named run to reset the book and run-local state. Use a distinct `run_id` for a distinct logical run, especially when persistence is enabled.

## Where to go next

Read [FIX Trading](https://github.com/lehajamb/mockx/wiki/FIX-Trading) for cancellation and replacement, [Sessions and Simulated Time](https://github.com/lehajamb/mockx/wiki/Sessions-and-Simulated-Time) for lifecycle and clock rules, and [Configuration](https://github.com/lehajamb/mockx/wiki/Configuration) for instruments, participants, and latency. For fills between different participants, run the [live demo](https://github.com/lehajamb/mockx/wiki/Docker-and-Live-Demo).

Sources: [README quick start](https://github.com/lehajamb/mockx/blob/556e28b533171ceee63864e396faedfb2ef7aa8e/README.md#5-local-quick-start), [local configuration](https://github.com/lehajamb/mockx/blob/556e28b533171ceee63864e396faedfb2ef7aa8e/configs/mockx-local.yml), [bounded FIX client](https://github.com/lehajamb/mockx/blob/556e28b533171ceee63864e396faedfb2ef7aa8e/examples/submit_orders.py), [requirements](https://github.com/lehajamb/mockx/blob/556e28b533171ceee63864e396faedfb2ef7aa8e/requirements.txt).
