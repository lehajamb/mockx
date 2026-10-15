# Native protocols and external dashboard

MockX runs FIX, ITCH/MoldUDP64, GLIMPSE/SoupBinTCP and QuestDB as one native
exchange stack. The matching engine's completed command effects feed both its
market-data publication and the typed QuestDB journal. The browser dashboard is
an external consumer; it does not add viewer callbacks to the engine.

The [dashboard guide](../examples/dashboard/README.md) describes the derived
40-level arrays, complete-command reconstruction, simulated-time selectors,
publication lag, read-only Grafana connection and Docker setup. Native clients
continue to use [GLIMPSE/ITCH](../examples/cpp_market_feed/README.md) for live books.

The [two-venue SOR example](../examples/cpp_sor/README.md) runs two exchange
instances against one QuestDB service with distinct venue/run identities. Its
passive FIX sessions and strategy FIX sessions have separate participants, so
mandatory self-trade prevention preserves actual ownership semantics.

To demonstrate both initialization submission paths, native replace and cancel:

```powershell
python examples/batch_orders.py --mode list
python examples/batch_orders.py --mode pipeline --batch-size 100
```

Both paths use ordinary matching and individual ExecutionReports. A native list
is immediate and unfragmented; it does not roll back successful siblings when
another member is rejected. Initialization waits for every member's NEW outcome
and verifies the book before replacing the first member with FIX `G` and
cancelling it with FIX `F`. List-member replacements/cancellations retain tag 66.

FIX replace quantity is the chain's total requested quantity, including previous
fills. Reducing size at the same price retains FIFO priority and emits an ITCH
`X` reduction. A price change or quantity increase removes the displayed
reference and introduces a fresh one, so native feed clients follow normal
delete/add mechanics.
