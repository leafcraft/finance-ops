# finance-ops — example scenarios

Six realistic scenarios that exercise the major paths through the pod. Each file
is a self-contained JSON (`_entry_point` + `_description` + `data`) that runs
end-to-end via `mesh_call` against the seed source store — no connector needed.

## How to run

```bash
# All six scenarios in sequence:
python examples/run_examples.py

# One specific scenario (any substring match works):
python examples/run_examples.py 02_duplicate
python examples/run_examples.py close_sweep
```

Each scenario prints its entry point, the description, and the result. Audit
lines accumulate in `logs/finance_audit.jsonl` — every line is hash-chained to
the previous one, so a single `tail -f` gives you the operational ledger across
runs.

## Scenario map

| # | Scenario | Entry point | What it exercises |
|---|---|---|---|
| 1 | AP services invoice ($80K) | `email_invoice` | Ivy extract → Mat code → approval matrix routes to **CFO** (>= $50K) → Poe posts |
| 2 | Duplicate re-submission | `email_invoice` | Ray's **dedupe floor** catches an already-posted invoice → forced controller review (not paid twice) |
| 3 | 3-way quantity variance | `portal_invoice` | Mat's **3-way match floor** finds a short goods receipt → cross-check fails → bounded back-edge to Ivy → Lead |
| 4 | "Where is this invoice?" | `invoice_status` | Cora answers stage / waiting-on from the case record, inventing nothing |
| 5 | Month-end close sweep | `daily_close_sweep` | Ivy fans across the backlog via `scaling.copies` — each copy claims a **disjoint** invoice atomically |
| 6 | Dual sign-off ($642,500) | `portal_invoice` | Above $500K: **CFO approves → Poe routes to the Controller → posts only with both signatures** (two different people) |

## Why these scenarios

1. **The routine case** — most invoices extract cleanly and route by amount.
2. **The trust mechanism** — a confirmed duplicate is forced to a controller
   review *in code*, not left to a model's judgement.
3. **The bidirectional case** — a real mismatch back-edges to re-extraction
   (bounded) rather than auto-paying or force-balancing.
4. **The coordinator** — status is answered from the record, never invented.
5. **The fan-out** — the close sweep drains a backlog with no double-work.

## Note

These run the full LLM chain (Tier 1: license + API keys + Redis). The finisher
and floor guarantees are also covered without any LLM/Redis in
`tests/test_floors.py`. Add a `07_*.json` and the runner picks it up
automatically.
