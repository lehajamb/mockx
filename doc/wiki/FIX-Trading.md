# FIX Trading

FIX 4.4 through QuickFIX is MockX's public live order-entry interface. Use REST to start a simulation and advance its clock, then submit orders, cancellations and amendments over FIX. [Getting Started](https://github.com/lehajamb/mockx/wiki/Getting-Started) shows a complete first run.

## Supported messages

| Direction | Message |
|---|---|
| Incoming | New Order Single (`35=D`) |
| Incoming | Order Cancel Request (`35=F`) |
| Incoming | Order Cancel/Replace Request (`35=G`) |
| Incoming, rejected | Market Data Request (`35=V`); use the market-data interfaces instead |
| Outgoing | Execution Report (`35=8`), Order Cancel Reject (`35=9`) |
| Outgoing validation/session handling | Market Data Request Reject (`35=Y`), session Reject (`35=3`), Business Message Reject (`35=j`) |

Supported new orders are BUY/SELL, LIMIT/MARKET and GTC/IOC. Omitted time in force defaults to GTC; unsupported values are rejected. There are no stop orders or amendments of order type/time in force.

## Values and validation

Quantity must be finite, positive and exactly divisible by the configured lot size. LIMIT price must be finite, positive and exactly divisible by tick size. MARKET orders must omit price. Converted unit counts must fit a positive signed 64-bit integer; enabling ITCH imposes additional wire bounds described in [Configuration](https://github.com/lehajamb/mockx/wiki/Configuration).

`ClOrdID (11)` must contain 1–64 visible ASCII characters without surrounding whitespace. Use a new ID for every normalized D, F or G request. That shared duplicate-ID namespace belongs to the stable QuickFIX `SessionID`; it survives logout, reconnect and simulation reset, and clears only on process restart. Another `SessionID` has an independent namespace.

| Failure | Response category |
|---|---|
| Malformed FIX headers | Session Reject |
| Missing or unusable `ClOrdID` on D/G, or unusable `OrigClOrdID` on G | Business Message Reject |
| Identifiable semantic new-order failure | Rejected Execution Report |
| Cancel (`35=F`) body parse/normalization error, including missing or unusable client IDs | Order Cancel Reject with domain reason `INVALID_REQUEST`; unavailable identity fields use placeholders |
| Cancel/replacement domain rejection, or replacement body error after its IDs are parsed | Order Cancel Reject |
| Lifecycle not accepting application ingress | Business Message Reject with a stable lifecycle code |

New-order (`35=D`) and replacement (`35=G`) normalization failures use code-prefixed rejection text, such as `OFF_LOT_QUANTITY`, `OFF_TICK_PRICE`, `PRICE_NOT_ALLOWED` and `NUMBER_OUT_OF_RANGE`. This is not a universal format for `Text (58)`: ordinary cancel/replacement domain rejects use descriptive text (for example, a stale `OrigClOrdID` explanation), and cancel body parse errors carry the parsing exception text. NaN and infinity are never copied into accepted orders or public numeric payloads. Lifecycle-gated requests do not reserve their client IDs.

## Matching and reports

MockX uses price-time priority. Each fill creates an immutable passive-side report followed by an immutable aggressor-side report at the same simulated timestamp. A sweep through several passive orders can therefore produce intermediate `PARTIAL` reports before `FILLED`.

| Order outcome | Remainder behavior |
|---|---|
| Unfilled GTC LIMIT | Rests as `NEW` |
| Partially filled GTC LIMIT | Rests as `PARTIAL` |
| IOC with any remaining quantity | Remaining quantity is canceled |
| MARKET with any remaining quantity | Remaining quantity is canceled regardless of TIF |
| Terminal order | Absent from the live book |

An execution report is a snapshot captured when the event occurs. Later changes to an order cannot rewrite an earlier report. `OrderQty`, `CumQty`, `LeavesQty`, `LastQty` and `AvgPx` should be interpreted per report, rather than reconstructed from a mutable server-side order.

## Ownership and self-trade prevention

Every accepted order has an immutable participant owner assigned by `fix.session_participants`. The mapping key is the complete QuickFIX session string. Several FIX sessions may belong to the same participant; session aliases are only display/latency metadata.

On the first encounter with a passive order owned by the same participant, MockX cancels both orders' complete remaining quantities and stops matching the aggressor. It emits passive then aggressor cancellation reports, creates no trade ID, and adds no trade volume for the prevented encounter. Earlier fills against other participants remain in cumulative quantity and average price. Both cancellation reports have zero leaves and this text shape:

```text
SELF_TRADE_PREVENTION: canceled passive and aggressor; prevented_qty=<quantity>
```

This policy cannot be disabled or replaced. To test normal fills, configure distinct participants, as in the two-session [Docker and Live Demo](https://github.com/lehajamb/mockx/wiki/Docker-and-Live-Demo).

## Cancellation

A cancel must use a fresh `ClOrdID` and reference the target's latest accepted client ID through `OrigClOrdID (41)`. Only the owning FIX session may cancel an order. Resolution happens when the scheduled cancel reaches engine processing, so a concurrent fill can change the result.

Pending and resting `NEW`/`PARTIAL` orders can be canceled. Filled or rejected orders produce `TOO_LATE_TO_CANCEL`; canceled orders produce `ALREADY_CANCELED`. Unknown and foreign-session references remain distinct rejection cases. A successful pending cancel advances the order version so an older queued new-order processing event cannot resurrect it.

## Native cancel/replace

Submit `35=G` with a fresh `ClOrdID`, latest accepted `OrigClOrdID`, `Symbol (55)`, `Side (54)`, `OrdType (40)`, total `OrderQty (38)` and limit `Price (44)`. Omitted `TimeInForce (59)` means GTC. Only a live, resting GTC LIMIT in `NEW` or `PARTIAL` is eligible at engine execution time.

| Requested amendment | Effect |
|---|---|
| Same price, smaller total quantity | Preserve FIFO and displayed order reference; reduce leaves |
| Same price and total quantity | Preserve FIFO; acknowledge the new client ID without publishing a book change |
| Any price change or larger total quantity | Remove old liquidity, lose FIFO, immediately match, then rest any remainder at the back |
| Total quantity at or below execution-time `CumQty` | Clamp `OrderQty` to `CumQty`, remove leaves and finish `FILLED` |

`OrderQty` is total lifetime order quantity, not requested leaves. The old order remains executable during inbound/engine latency, so intervening fills affect eligibility and clamping. Clamping creates no trade or canceled quantity. Replacement does not draw another simulated new-order rejection.

Symbol, side, type, TIF, owning session and participant cannot change. Successful amendments preserve public `OrderID`, original submission time, cumulative fills and filled notional, but advance the active client ID, including for a no-op. Older accepted references then reject as `STALE_ORDER_REFERENCE`. A rejected amendment never becomes an accepted revision, but its normalized new request ID remains consumed.

The acknowledgment is one Execution Report with `ExecType (150)=5`, the new `ClOrdID`, previous accepted `OrigClOrdID`, and `OrdStatus (39)=0`, `1` or `2` for NEW, PARTIAL or FILLED. It precedes replacement-triggered fill reports. There is no interim Pending Replace report or persistent `REPLACED` order status. Replacement rejects use `35=9`, `CxlRejResponseTo (434)=2`; ordinary cancel rejects use `434=1`.

Starting with an unfilled resting BUY `N-1` for `0.010` BTC-USDT at `100.00`, the application fields for reducing the quantity are:

```text
35=G|11=R-1|41=N-1|55=BTC-USDT|54=1|40=2|38=0.006|44=100.00|59=1|60=20261018-12:00:00.000|
```

The vertical bar represents SOH. QuickFIX supplies the session header, body length and checksum. After acceptance, reference `R-1` for the next cancel/replace. The bundled helper sends the same operation through an already connected session:

```python
from examples.fix_support import send_replace

send_replace(
    session_id,
    cl_ord_id="R-1", orig_cl_ord_id="N-1", symbol="BTC-USDT",
    side="1", quantity="0.006", price="100.00",
)
```

## Deterministic ingress and latency

QuickFIX callbacks parse immutable command data and hand it to a central ingress sequencer. Published `main` drains acknowledged envelopes in ascending order of:

```text
(simulated receive timestamp, complete stable SessionID, FIX MsgSeqNum, run-local ingress sequence)
```

The final ingress sequence breaks remaining ties and resets on successful simulation start. Later scheduler stages use FIFO ordering for equal timestamps. Cancellation stages are `cancel_received`, `cancel_arrived_at_exchange`, `cancel_engine_process`, and shared `fix_delivery_due`; replacements have corresponding `replace_*` stages.

Inbound network latency and requester session delay precede engine latency. Outbound latency controls when the immutable report becomes eligible for delivery. An already queued equal-time match can execute before a later cancel. These are explicit timing rules, not a promise that a cancel wins every race. See [Matching Engine and Scheduler](https://github.com/lehajamb/mockx/wiki/Matching-Engine-and-Scheduler).

## Identities and reliable delivery

| Public identity | Format |
|---|---|
| `OrderID` | `G{generation:08d}-O{counter:08d}` |
| `ExecID` | `G{generation:08d}-E{counter:08d}` |
| `TradeID` | `G{generation:08d}-T{counter:08d}` |

The bundled [FIX44 dictionary](https://github.com/lehajamb/mockx/blob/556e28b533171ceee63864e396faedfb2ef7aa8e/configs/FIX44.xml) declares optional `TradeID (1003)` as a string. Internal cancel-reject and delivery IDs are not additional FIX wire fields; a `35=9` message has no `ExecID`.

Reports and cancel rejects enter a process-lifetime application queue once. Only the oldest undelivered record for a stable FIX session can be sent. Disconnected sessions keep their backlog. Transport exceptions or a false `sendToTarget` result retry using simulated time and bounded attempts; logon starts an immediate fresh bounded cycle. Serialization failures become `PERMANENTLY_FAILED` and block that session's later records without blocking other sessions.

Simulation end/reset preserves this backlog. Repeated logons and stale due events cannot resend records already marked delivered inside MockX. However, the queue is process-local and is lost on process/host failure. QuickFIX separately owns sequence numbers, stores and protocol resends. This is not end-to-end exactly-once delivery. Inspect `/stats.fix_delivery` and `/readyz`; a retained permanently failed head has no public in-place recovery and requires fixing its cause and restarting the process.

## Source references

- [FIX gateway and ingress](https://github.com/lehajamb/mockx/blob/556e28b533171ceee63864e396faedfb2ef7aa8e/src/mockx/gateway.py)
- [Delivery queue](https://github.com/lehajamb/mockx/blob/556e28b533171ceee63864e396faedfb2ef7aa8e/src/mockx/delivery.py)
- [Execution report tests](https://github.com/lehajamb/mockx/blob/556e28b533171ceee63864e396faedfb2ef7aa8e/tests/test_fix_execution_reports.py)
- [Replacement contract tests](https://github.com/lehajamb/mockx/blob/556e28b533171ceee63864e396faedfb2ef7aa8e/tests/test_replace_fix.py)
- [Self-trade prevention tests](https://github.com/lehajamb/mockx/blob/556e28b533171ceee63864e396faedfb2ef7aa8e/tests/test_pr8_self_trade_prevention.py)
