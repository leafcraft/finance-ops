# finance-ops — Finance-Operations Pod

Five digital employees + four human roles that run a payable end to end —
**invoice arrives → confidence-scored extraction (AP / AR / close) → duplicate +
fraud screen → PO/3-way match + GL coding (cross-checked) → approval tier (AP
clerk / Controller / CFO) → ERP posting → live status** — with a human on every
gate and the systems (email / vendor portal / ERP) as tools inside the agents,
never members.

**Read `PRD.md`** for the full agent-by-agent spec. This is a **pod-standard
reference** (SDK 2.4.131): a team of self-reliant agents, not a pipeline.

## The story

It's the 28th. A vendor bill lands in the AP mailbox at 4:57 pm, one of ninety
sitting in the month-end backlog. The old way: a clerk keys the header, hunts the
PO, eyeballs the goods receipt, guesses a GL code, walks it to whoever can sign
for that amount, and — three days later — types it into NetSuite. Somewhere in
those ninety, one is a re-submission that will get paid twice, and one is coded
to a cost center that will blow the department's variance report.

The pod runs that chain end to end, working alongside the people who already own
the decisions. **Ivy** reads each invoice with a confidence score on every field
and, when the month-end sweep fires, fans across the whole backlog — each copy
claiming a different invoice. **Ray** checks it against the posted ledger before
anyone codes it, so the re-submission is caught, not paid. **Mat** matches the PO
and the goods receipt with fixed-tolerance arithmetic, codes it against the live
chart of accounts, and cross-checks the totals before a human sees the sheet. The
approval matrix routes it to the *right* signer by amount. Only after a real human
approval does **Poe** post it — and Poe re-checks that approval, refuses to post
on silence, and deletes a half-written pack if anything trips. **Cora** answers
"where is this invoice?" from the case record the whole time.

## Why it works in an organization

It wins "no double payments, no mis-codes" the only honest way: **Ivy never
guesses** — a field she cannot read with confidence becomes a question to the AP
clerk, not a number on a coding sheet. **Ray's dedupe floor is deterministic** —
a confirmed duplicate is forced to a controller review in code, not left to a
model's mood. **Mat cross-checks** before anyone sees the sheet and returns to Ivy
on a mismatch (bounded — one round, then it terminates to the Lead) rather than
balancing the books itself. **Poe re-checks that a human actually approved**
before it posts anything, and **deletes a half-posted pack** if its floor trips.
**Silence never advances an invoice** — every gate default-denies to
`held_for_review` and lands in the audit sink. Every decision, post, and approval
is hash-chained into a WORM ledger a regulator can replay. ROI: the AP clerk's
keying and chasing time collapses, mis-codes and double-pays are caught before
the money moves, and month-end close stops being a fire drill.

## The team (10 members)

| Agent | Role (human job title) | Responsibilities | Can perform |
|---|---|---|---|
| `fin_coordinator_agent` | Finance-Ops Coordinator | Owns the case end to end; opens it, keeps live status, is the requester's one contact | LLM; orchestrates Ivy→Ray→Mat→Poe; routes exceptions to the Lead; **never extracts, codes, or approves** |
| `invoice_intake_agent` | Invoice Intake & Extraction Analyst | Reads each invoice with a confidence score, classifies AP/AR/close, runs the policy pre-check | LLM + **super_agent** + **playbook** (AP/AR/close lanes) + **instances** (backlog fan-out with an atomic claim tool) + intake tools; `@chain` **confidence floor** (fail closed) |
| `payment_risk_agent` | Payment-Risk Officer | Duplicate ledger pre-flight + fraud/anomaly verdict from vendor history | LLM; `@chain` **dedupe floor** — a confirmed duplicate is forced to controller review |
| `matching_gl_agent` | Matching & GL-Coding Builder | PO/3-way match + GL coding; cross-checks totals tie out; renders the coding sheet | LLM + **playbook**; `@chain` **cross-check floor**; bounded back-edge to Ivy; renders the draft to `./out`; approval matrix on its wires |
| `erp_poster_agent` | ERP Poster & Ledger Liaison | Re-checks approval, posts the approved entry to the ERP, records the ledger | Programmatic **finisher**; approve-token re-check → invariants → post + render pack → **delete-on-block** → WORM ledger |
| `finance_ops_lead_human` | Accountable owner (Human) | Reviews exceptions, owns policy calls, decides every gate | Human gate — everything that leaves the pod |
| `ap_clerk_human` | AP Clerk (Human) | Answers Ivy's questions; signs off routine coding sheets (< $5K) | Human gate |
| `controller_human` | Controller (Human) | Regulated sign-off for exceptions, $5K–$50K, low-confidence coding, flagged risk | Human gate |
| `cfo_human` | CFO (Human) | High-value sign-off (≥ $50K); dual sign-off above $500K | Human gate |
| `audit_logger_agent` | WORM audit | Hash-chains every decision, post, approval, and held gate | External (custom sink); terminus |

The **LeafMesh Manager** supervises — it reads every hand-off and escalates
stalls / policy calls / approval timeouts to the Finance-Ops Lead automatically.

## Prerequisites — what to set, in three tiers

Everything is set in `.env` (`cp env .env`). The **whole LLM chain (extract →
risk → code → approval routing) runs on Tier 1 alone** against the seed source
store (`./data`); invoice reads, ERP posting, and approval notifications use dev
stand-ins that **announce themselves** until you wire Tier 2.

### Tier 1 — required to boot (dev mode)
| Setting | For | Notes |
|---|---|---|
| `LEAFMESH_LICENSE_KEY` | LeafMesh runtime | **Required.** |
| `ANTHROPIC_API_KEY` | Ivy + Mat (Claude) | **Required.** |
| `OPENAI_API_KEY` | Cora + Ray + Manager (`gpt-4o-mini`) | **Required** unless you change the models. |
| `REDIS_HOST` / `PORT` / `PASSWORD` | State store | Defaults to `localhost:6379`. `docker run -d -p 6379:6379 redis:7-alpine`. |
| `HITL_OUTBOUND_URL` | Where human members are notified | Defaults to a local stub. |

### Tier 2 — real invoice connectors (set the URL, uncomment the `mcp` block)
| Setting | Enables |
|---|---|
| `ERP_MCP_URL` / `_TOKEN` | Real invoice / PO / goods-receipt reads from your ERP |
| `MAIL_MCP_URL` / `_TOKEN` | Real invoice emails + attachments |

> ⚠️ The SDK rejects an `mcp` block with an **empty** URL at load, so the block
> ships **commented** on `invoice_intake_agent` in `configs/config.yaml`. Fill the
> URL, then uncomment.

### Tier 3 — direct ERP posting replaces the dev SQLite ledger.
Going live = swap the function bodies in `agency/_shared/erp_poster.py` (the post)
and `agency/_shared/store.py` (the reads) for your ERP's REST/RPC — the agents do
not change.

## Run it

```bash
pip install -r requirements.txt          # core LeafMesh runs the pod day-0
docker run -d -p 6379:6379 redis:7-alpine
cp env .env                              # fill at least Tier 1
python validate_config.py                # pre-flight: wiring, yields, dead-code
python main.py                           # API at http://127.0.0.1:18820/docs
```

Submit an invoice (day-0, against the seed store — no connector needed):

```bash
curl -X POST http://127.0.0.1:18820/api/mesh/request \
  -H "Content-Type: application/json" \
  -d '{"entry_point":"email_invoice",
       "data":{"source_ref":"evt-acme-2207","message":"invoice from Acme"}}'
```

`evt-acme-2207` is a deliberate re-submission of an already-posted invoice — watch
Ray's dedupe floor force it to a controller review rather than pay it twice. On
approval, Poe posts to the dev ERP ledger and renders the pack to `./out/`.

> Packaging note: `pip install leafmesh` (core) includes all LLM providers and
> runs the pod. For the real ERP/mail connectors add `leafmesh[mcp]`. Avoid
> `leafmesh[all]` on Python 3.12+ (it pulls `cchardet`, which has no wheel) — add
> only the specific extras you need.

---

*Built with `create-leafmesh`. The config is the department; the PRD is the brief.*
*Design doctrine: `.claude/skills/leafmesh/building-agents-thoroughly.md`.*
