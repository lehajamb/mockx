# WebSocket Market Data

Subscribe to `ws://127.0.0.1:8001/ws/BTC-USDT` to receive immutable, complete book snapshots and fills from each published matching action. The endpoint shares the API listener. For a ready-made viewer, open `http://127.0.0.1:8001/ui/BTC-USDT` after starting the exchange.

## Message types

The first application message is the current snapshot, pinned before the connection becomes visible to later broadcasts. It carries the latest published sequence without incrementing it, and its `trades` array is empty:

```json
{
  "type": "market_data",
  "symbol": "BTC-USDT",
  "sequence": 42,
  "timestamp": "1970-01-01T00:00:02+00:00",
  "bids": [],
  "asks": [],
  "trades": []
}
```

Each bid/ask level has the same shape as [REST API](https://github.com/lehajamb/mockx/wiki/REST-API) levels:

```json
{
  "price": "99.00",
  "qty": "1.000",
  "strategy_qty": "0.000",
  "market_qty": "1.000",
  "order_count": 1
}
```

Subsequent messages contain complete latest-state levels. Their `trades` array contains only fills from that matching action, for example:

```json
{
  "type": "market_data",
  "symbol": "BTC-USDT",
  "sequence": 43,
  "timestamp": "1970-01-01T00:00:02.001000+00:00",
  "bids": [],
  "asks": [],
  "trades": [
    {
      "trade_id": "G00000001-T00000017",
      "price": "100.00",
      "qty": "2.000",
      "aggressor_side": "BUY",
      "timestamp": "1970-01-01T00:00:02.001000+00:00"
    }
  ]
}
```

Numeric instrument values are exact decimal strings. Timestamps are simulated UTC, including in heartbeats. Heartbeat cadence uses connection timing; a heartbeat does not advance simulated time or market-data sequence:

```json
{
  "type": "heartbeat",
  "symbol": "BTC-USDT",
  "timestamp": "1970-01-01T00:00:02.001000+00:00",
  "last_sequence": 43
}
```

## Sequence and reset semantics

Sequence is global across the exchange's symbols. It starts at 0, increments once per published book snapshot, and resets to 0 when a new simulation session resets the engine. Session start closes existing sockets; reconnect to obtain the new initial snapshot. Sequence is not durable across process restarts.

A gap on one symbol can result from updates on other symbols or deliberate conflation of unsent snapshots. A gap alone cannot establish that the displayed book is corrupt: each snapshot is complete. The stream does not promise delivery of every intermediate snapshot or every trade to a slow consumer. Use FIX execution reports for an order owner's execution history, and choose an appropriate audit/persistence path for other historical needs.

Treat a reconnect's first message as a new baseline. Do not compare its sequence to a previous run's sequence without understanding the reset boundary. There is no resume token or guaranteed replay API.

## Delivery under congestion

Each subscription has one stable internal connection ID, bounded queue and sender task. The default is eight pending frames, excluding the currently sending frame. The initial snapshot is pinned; at most one later unsent market-data snapshot is retained. A newer snapshot replaces it, while duplicate/older snapshots are discarded by the server queue.

At most one heartbeat is pending. Heartbeats can be coalesced or dropped under congestion; snapshots may displace them. Fatal error and close control traffic retain priority. Network writes never hold the registry lock, and broadcasts only perform bounded in-memory enqueues, so a slow socket cannot delay other clients, matching or clock advancement.

Each send defaults to a five-second timeout. On timeout the server removes the client, terminates its sender and attempts a bounded close with code 1013. These queues are process-local; there is no distributed fan-out or broker. Configuration values are listed in [Configuration](https://github.com/lehajamb/mockx/wiki/Configuration).

## Errors and close codes

| Situation | Behavior |
|---|---|
| Unknown symbol | `type: "error"`, code `UNKNOWN_SYMBOL`, then close 1008 |
| Registration during `STARTING` or `ENDING` | Error envelope, then close 1013 |
| Send timeout / overloaded connection | Removal and bounded close attempt with 1013 |
| Simulation reset | Sender cancellation and socket close with 1001 |

Old-generation delayed snapshots are discarded instead of being delivered to newly registered clients. Reconnect with bounded backoff and obtain the initial snapshot before considering the subscription usable again.

## Browser viewer behavior

The built-in viewer derives `ws://` or `wss://`, host and port from the page origin. It tracks heartbeat freshness and market-data freshness separately. Heartbeats alone do not suppress REST fallback: when market data becomes stale, the viewer polls `/book/{symbol}` until another WebSocket market-data frame arrives.

The current viewer has a limitation: every market-data frame refreshes freshness and disables fallback, even a duplicate. Duplicates do not redraw the book, but a lower sequence is accepted and can replace the display. The viewer does not enforce monotonic WebSocket sequences. A global sequence gap by itself does not trigger polling.

Reconnect uses bounded exponential backoff with one timer. Server YAML supplies heartbeat, freshness, polling, reconnect and staleness-check intervals. The viewer includes live tables, a 120-snapshot best-bid/ask history chart and cumulative 20-level depth. Charts load Highcharts from its CDN; tables remain usable if it is unavailable. See the source README's license note before using the chart demo beyond qualifying personal or educational non-commercial use.

## Source references

- [API message models and subscription lifecycle](https://github.com/lehajamb/mockx/blob/556e28b533171ceee63864e396faedfb2ef7aa8e/src/mockx/api.py)
- [Queue and sender implementation](https://github.com/lehajamb/mockx/blob/556e28b533171ceee63864e396faedfb2ef7aa8e/src/mockx/websocket_delivery.py)
- [Market-data contract tests](https://github.com/lehajamb/mockx/blob/556e28b533171ceee63864e396faedfb2ef7aa8e/tests/test_websocket_market_data.py)
- [Delivery isolation tests](https://github.com/lehajamb/mockx/blob/556e28b533171ceee63864e396faedfb2ef7aa8e/tests/test_websocket_delivery.py)
- [Browser viewer](https://github.com/lehajamb/mockx/blob/556e28b533171ceee63864e396faedfb2ef7aa8e/templates/book.html)
