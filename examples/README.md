# finance-ops — example scenarios

Five realistic AP scenarios that exercise the major paths through this
mesh. Each file is a self-contained JSON payload that runs end-to-end
via `mesh_call`.

## How to run

```bash
# All five scenarios in sequence:
python examples/run_examples.py

# One specific scenario:
python examples/run_examples.py 01_happy_path
python examples/run_examples.py vendor_inquiry   # any substring match works
```

Each scenario prints its entry-point, the input description, and the
result. Audit lines accumulate in `logs/audit.jsonl` — every line is
hash-chained to the previous one, so a single `tail -f` of that file
gives you the operational ledger across runs.

## Scenario map

| # | Scenario                          | Entry point      | Expected route                                                                            |
|---|-----------------------------------|------------------|-------------------------------------------------------------------------------------------|
| 1 | Happy-path subscription renewal   | `manual_entry`   | intake (normalize + extract) → matching (PO) → GL → approval-router → AP-clerk → ERP-poster → audit |
| 2 | Fraud signal (round number, new vendor) | `manual_entry` | intake (normalize + extract) → **payment-risk** (dup pre-flight + fraud verdict) → controller-human (high-risk override) → audit |
| 3 | PO mismatch — vendor over-billed  | `manual_entry`   | intake (normalize + extract) → **matching** (partial PO match) → vendor-comms (exception query) → vendor query → audit |
| 4 | High-value CFO sign-off ($250K)   | `manual_entry`   | intake (normalize + extract) → GL → approval-router → **CFO-human + controller dual-sign** → ERP |
| 5 | Vendor inquiry — "where's my pay?" | `vendor_inquiry` | vendor-comms (inquiry_response mode) → lookup status → drafts reply → audit             |

## Why these scenarios

Every AP team sees these five shapes in real life. They cover:

1. **The 80% case** — most invoices are sub-ceiling, PO-matched, no
   anomalies. Scenario 1 confirms the routine path runs straight through.
2. **The trust mechanism** — when fraud signals fire, the routing must
   bypass the AP clerk and land on Controller. Scenario 2 verifies the
   override fires correctly.
3. **The bidirectional case** — a real AP team spends most of its
   judgement time on PO mismatches. Scenario 3 verifies the
   vendor-comms side-arm activates (exception_query mode) and we draft
   a clean vendor query rather than auto-paying or auto-rejecting.
4. **The default-deny ceiling** — for high-value items, the approval
   matrix forces CFO involvement regardless of vendor history. Scenario 4
   verifies the threshold conditions on `gl_coding_agent`'s wires actually
   route to CFO + that dual-sign logic engages above the high-value
   floor.
5. **The reverse direction** — vendors ask AP for status all day long.
   Scenario 5 verifies the `vendor_inquiry` entry point (vendor_comms,
   inquiry_response mode) produces a drafted response (it does NOT
   auto-send — every reply still has a Controller in the loop on the
   audit trail).

## What's NOT in the scaffold examples (yet)

A real shop runs many more shapes — accruals, credit memos, intercompany
transfers, 1099 prep, currency conversion, expense-report ingest. These
are tracked in the parent README's roadmap section and added as the
suite expands. If a scenario you need is missing today, write it as a
new `06_*.json` and the runner picks it up automatically.
