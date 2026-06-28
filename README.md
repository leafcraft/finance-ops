# finance-ops — Finance Operations

A LeafMesh deployment that runs accounts payable, accounts receivable, and
the monthly close as a **peer-to-peer team of governed agents** — not a
fixed pipeline. Invoices arrive from any channel, get normalized,
PO-matched, fraud-screened, GL-coded, signed off by the right human tier,
posted to the ERP, and recorded in a hash-chained audit. The Manager hovers
above and intervenes only on narration cues, escalation, or contract
violations; the agents hand work to each other directly via conditional
`can_call` edges. **Read `PRD.md` for the full architecture.**

## Story

A vendor emails an invoice to AP@northwind.com: **Acme Supplies, $2,500,
PO-12345**. The `email_invoice` entry point turns it into a mesh call to
**invoice_intake** (the records intake specialist), which OCR-normalizes the
attachment into the canonical `InvoiceEvent`, pulls line items / tax /
terms, and sets routing flags — here, `requires_po_match` (≥ $1K with a PO).
Intake's wires fire: **matching** (the PO-matching clerk) looks up PO-12345
in the ERP, matches 95% of lines, clears the 0.9 threshold; **payment_risk**
is skipped (under $10K, known vendor, no anomaly). Both reach **gl_coding**
(the GL accountant), which codes the invoice to `6500-Software-and-SaaS`
with confidence 0.92 and stashes the context for post-approval recovery.

gl_coding's approval-matrix wires read the amount: $2,500 < $5K, confident,
no anomaly → **ap_clerk** (the AP clerk). **Priya**, an AP clerk, sees the
task in her Hosted Studio inbox: "Routine invoice review — Acme Supplies,
$2,500, GL 6500." She replies **approve**. That routes to **erp_poster**
(the AP posting specialist), which recovers the full invoice from the
session stash, writes the entry to NetSuite, and hands off to
**audit_logger** (the compliance recorder), which appends a hash-chained
WORM line. Total human touch: one click. Had the amount been $80,000, the
same matrix would have routed Priya's queue past her to the **CFO** with
dual-sign required; had payment_risk scored ≥ 0.7, it would have gone
straight to the **Controller** with a written briefing — the wires *are*
the policy.

## Why it works in an organization

Finance / back-office is the ~5–6%-of-revenue, ~85%-routine function the
operating-ROI model flags as top-of-stack for governed agents: the work
(extraction, matching, coding, reconciliation, posting) is mechanical and
high-volume, but it is **regulator-visible**, so every action must be
audited and every dollar must keep a human sign-off. This mesh automates
the mechanical middle — a real LLM extracts and codes, a deterministic
matrix routes approvals — while humans keep the high-judgment gates (AP
clerk → Controller → CFO) and a hash-chained WORM audit keeps it
defensible. Policy lives in **knowledge groups** (AP runbook, vendor
master, fraud playbook, chart of accounts): edit the doc and every agent's
behavior changes with no model retraining. State lives in the customer's
**ERP and treasury systems** (NetSuite / SAP / QuickBooks / Kyriba),
reached via MCP connectors — not in template storage. The ROI: a
$100M-revenue company spending ~$5–6M/yr moves the routine hours across the
20× gap between ~$100/hr loaded payroll and ~$5/hr governed agents, while
the Controller and CFO still sign every dollar above threshold.

## The team

Peer-to-peer mesh (Rule 1 — work starts from many places and flows forward,
no conductor hub):

```
  any of [email, portal, ERP webhook, mobile, manual, monthly-close cron,
          vendor inquiry, new-vendor first invoice, duplicate sweep,
          payment-run kickoff]
                                 │
                                 ▼
                       invoice_intake_agent (LLM, knowledge-grounded —
                          normalize + extract fields + routing flags)
                                 │
       ┌──── if PO or 3-way req ─┴───────────────────────────┐
       ▼                                                     ▼ if anomaly risk
   matching_agent                                     payment_risk_agent
   (programmatic — PO match,                          (LLM, knowledge —
    then 3-way match when req'd)                       dup ledger pre-flight
       │  ▲                                            + fraud verdict)
       └──┘ peer can_call back                             │ score >= 0.7 →
       to invoice_intake (bounded)                         │ controller_human
                                                           ▼
       ┌───────────────── fan-in: wait_for ─────── gl_coding_agent (LLM,
       │                                            knowledge: chart_of_accounts)
       ▼
   THE APPROVAL MATRIX — explicit conditions on gl_coding's outbound wires
   (no router agent; the thresholds are readable in configs/config.yaml):
       < $5K, conf ≥ 0.7, no anomaly → ap_clerk_human
       < $50K otherwise              → controller_human
       ≥ $50K                        → cfo_human (dual-sign ≥ $500K)
       ┌─────────────┬─────────────┐
       ▼             ▼             ▼
  ap_clerk_human controller_human cfo_human   (HITL — channel-native)
       │
       ▼  (on approve)
   erp_poster_agent (programmatic — NetSuite/SAP/QuickBooks/…, fail-closed)
       │
       ▼
   audit_logger_agent (external "custom" — hash-chained WORM sink, terminus)

  side-arm callable from any specialist:
     vendor_comms_agent (LLM, mode-dispatched — drafts exception queries
     TO vendors AND answers vendor inquiries via its own entry points)
  pre-payment scheduler:
     payment_scheduler_agent (connector-only MCP → your treasury) → erp_poster
  gatekeeper for new counterparties:
     vendor_compliance_agent (LLM — KYC + onboarding gate, fail-closed)
  parallel / scheduled citizens (NOT in the per-invoice flow):
     close_cycle_orchestrator_agent (MCP sweep of your ERP)
     loan_origination_agent / loan_servicing_agent (credit domain math)
     regulatory_monitor_agent (daily reg-feed scan, memory-deduped)
```

### Agent table

Role = a human job title; every agent — supporting and connector ones
included — reads like a role on a real finance team.

| Agent | Role | Responsibilities | Can perform |
|---|---|---|---|
| `invoice_intake_agent` | Records intake specialist | Normalize any source (email / portal / ERP webhook / mobile / manual) into the canonical `InvoiceEvent`; extract lines / tax / terms; set policy routing flags (PO-match, 3-way, anomaly) | LLM, `gpt-4o-mini`, `enforce_yields` retry 3; knowledge `vendor_master`; fans out (conditions) to matching / payment_risk / gl_coding |
| `matching_agent` | PO-matching clerk | Look up the PO in the ERP, match line items, then run the 3-way match (invoice ↔ PO ↔ goods-receipt) when required; bound the re-extraction loop | Programmatic; **`@pre_compose`** pulls the PO + goods receipt + vendor record from the source store; **`@chain`** enforces the 3-way **tolerance arithmetic** floor (qty/price variance — LLM can't waive) + session-stash loop guard; routes gl_coding (clean) / invoice_intake (re-extract, bounded) / vendor_comms (gap) |
| `payment_risk_agent` | Payment-risk officer | Duplicate ledger pre-flight + fraud/anomaly verdict, grounded in the fraud playbook + vendor history; never block on one signal unless a confirmed duplicate | LLM, `gpt-4o-mini`, retry 2; knowledge `fraud_playbook`; **`@pre_compose`** pulls the vendor's prior posted invoices; **`@chain`** enforces the **duplicate-hashing** floor (strict + loose SHA-256 keys → forces fraud_score ≥ 0.85) + single-signal clamp; routes controller (≥ 0.7) / vendor_comms (dup or 0.4–0.7) / gl_coding (clean inline) / invoice_intake (clean preflight) |
| `gl_coding_agent` | GL accountant | Assign GL account / cost center / tax code from the chart of accounts; carry the dollar-threshold **approval matrix** on its outbound wires; stash context for post-gate recovery | LLM, `gpt-4o-mini`, retry 2; knowledge `chart_of_accounts`; **`@pre_compose`** pulls the active chart + vendor default mapping; micro-module enforces the **GL-validity** floor (a code not in the active chart → confidence < 0.7 → Controller) + keyword fallback + pre-gate stash; routes ap_clerk / controller / cfo by amount + confidence + anomaly |
| `vendor_comms_agent` | Vendor relations specialist | Draft exception queries TO vendors/requestors (gap found) AND answer vendor inquiries — payment status, disputes, receipts, tax forms — flagging high-stakes for human review | LLM, `gpt-4o-mini`, retry 2; knowledge `vendor_communication`; mode-dispatched (exception_query / inquiry_response); routes ap_clerk on review needed |
| `vendor_compliance_agent` | Compliance officer | Banking-grade KYC (identity / sanctions / PEP / beneficial owners) + new-vendor onboarding gate (tax form + OFAC + bank verify) before any first payment; fail-closed | LLM, `gpt-4o-mini`, retry 2; knowledge `sanctions_lists`; routes controller (SAR / EDD) / ap_clerk (review) / invoice_intake (clean onboarding only) |
| `ap_clerk_human` | AP clerk | First-line review of routine invoices under $5K; approve / reject / escalate; review vendor-facing drafts before send | Human, Hosted Studio inbox (shared); 1h timeout → escalate to Controller (default-deny); routes erp_poster (approve) / controller (escalate) / audit (reject) |
| `controller_human` | Controller | Regulated sign-off; exception judgment ($5K–$50K, low confidence, fraud, SAR/EDD); close-cycle owner | Human, Hosted Studio inbox (shared); 2h timeout → halt for review; routes erp_poster (approve) / cfo (escalate) / audit (reject / halt) |
| `cfo_human` | Chief Financial Officer | High-value sign-off (≥ $50K); dual sign-off (with Controller) ≥ $500K per policy | Human, Hosted Studio inbox (shared); 4h timeout → halt for review; routes erp_poster (approve) / audit (anything else) |
| `erp_poster_agent` | AP posting specialist | Post the approved invoice to the configured ERP; recover the invoice from the pre-gate session stash after the human gate; fail-closed (no payload ⇒ nothing posted) | Programmatic (Python actuator); reads `ERP_TYPE`; routes audit_logger |
| `payment_scheduler_agent` | Treasury analyst | Pick the optimal pay date + capture early-payment discounts in your treasury system (Kyriba / SAP TRM / NetSuite / Bill.com) | Programmatic, connector-only (`integration: mcp`, `TREASURY_MCP_URL`); routes erp_poster |
| `close_cycle_orchestrator_agent` | Month-end close lead | Sweep your ERP for unposted entries + reconciliation gaps, generate close reports | Programmatic, connector-only (`integration: mcp`, `ERP_MCP_URL`); cron shipped commented until wired; routes controller (gaps) / audit (clean) |
| `loan_origination_agent` | Credit underwriter | FICO / DTI / DSCR / rating underwriting matrix + amount-tiered approval for internal-credit / vendor-financing programs | Programmatic (Python domain math); routes controller (manager tier) / cfo (cfo + board tiers) on manual review |
| `loan_servicing_agent` | Loan servicing officer | Post loan payments (interest/principal split), generate payoff quotes, run the daily delinquency-bucket sweep | Programmatic (Python domain math + actuator); cron 05:00; routes controller (write-off review) / vendor_comms (collections) |
| `regulatory_monitor_agent` | Regulatory analyst | Daily scan of SEC / FinCEN / IRS / EU / state feeds; classify impact to finance processes; memory-dedupe already-reported items | LLM, `gpt-4o-mini`, cross-session memory; knowledge `regulatory_taxonomy`; cron 07:00; routes controller (immediate/high alerts) |
| `audit_logger_agent` | Compliance recorder | Append an immutable, hash-chained WORM audit line — the terminus of every flow | External (`framework: custom`); registered via `mesh.intelligence()` in `main.py`; terminal |

## Configuration — what to set, in three tiers

Everything is set in `.env` (`cp env .env`). The mesh boots and the full
**invoice → extract → match → code → approve → post → audit** chain runs on
**Tier 1 alone** — matching pulls real POs + goods receipts, payment_risk
pulls real posted history, and gl_coding pulls the real GL chart from the
**seeded dev store** (`agency/_shared/store.py`), so the deterministic
floors (3-way tolerance, duplicate hashing, GL-validity) fire end-to-end
with no connectors set. ERP / loan writes hit dev SQLite ledgers; knowledge
groups fall back to prompt rules + the keyword fallback chart. Tiers 2–3
turn the dev store + stubs into your real systems-of-record (every
`store.py` read is the connector contract).

### Tier 1 — required to boot (dev mode)

| Setting | What it's for | Required? |
|---|---|---|
| `LEAFMESH_LICENSE_KEY` | LeafMesh runtime license | **Required** |
| `LEAFMESH_ENV_TOKEN` | Per-environment telemetry token | **Required** |
| `OPENAI_API_KEY` | Every LLM agent + the Manager (`gpt-4o-mini`) | **Required** |
| `ANTHROPIC_API_KEY` | Optional — the BuiltIn Summarizer (narration) | Optional |
| `REDIS_HOST` / `REDIS_PORT` / `REDIS_PASSWORD` | Session state, dedup window, vendor cache | Optional (defaults `localhost:6379`, no password) |

With just Tier 1: `pip install -r requirements.txt && python validate_config.py && python main.py`.

### Tier 2 — host prerequisites / adapters (make the connectors real)

The system-of-record agents are **connector-only** (`integration: mcp`):
they need an MCP server in front of your ERP / treasury, not just an env
var. Setting the URL marks the wire; the connector must answer the declared
`tool_name`.

| Setting | Where it plugs in | Powers |
|---|---|---|
| `ERP_TYPE` | `agency/_shared/erp_poster.py` (tags every posted row; swap `ERPDispatcher.post()` for the real ERP API in prod) | erp_poster |
| `ERP_MCP_URL` / `ERP_MCP_TOKEN` | `close_cycle_orchestrator_agent` MCP connector (`tool_name: close_cycle_sweep`) | Month-end close sweep; enable its cron in `config.yaml` after wiring |
| `TREASURY_MCP_URL` / `TREASURY_MCP_TOKEN` | `payment_scheduler_agent` MCP connector (`tool_name: schedule_payment`) | Pay-date + discount-capture decisions |
| `FINANCE_OPS_ERP_LEDGER` | Dev-mode SQLite ledger path | Relocate the dev ERP ledger (irrelevant in prod) |
| `FINANCE_OPS_STORE_DIR` | Dev-mode seeded source store (`agency/_shared/store.py`) | Relocate the seeded invoices / POs / vendor master / GL chart / posted ledger (irrelevant in prod — reads hit the ERP) |
| `GL_DEFAULT_COST_CENTER` | `agency/gl_coding_agent.py` fallback cost center | Used only when `chart_of_accounts` is unseeded |

**Knowledge groups** (Tier 2, optional but recommended) ground the LLMs:
`vendor_master`, `fraud_playbook`, `chart_of_accounts`,
`vendor_communication`, `sanctions_lists`, `regulatory_taxonomy`,
`ap_runbook` (Manager). Until seeded, the LLM agents fall back to their
in-prompt deterministic rules and gl_coding's keyword fallback chart.

### Tier 3 — connectors / going live

| Setting | What it's for | Notes |
|---|---|---|
| `PAGER_WEBHOOK` | PagerDuty escalation target (manager.escalation) | **Consumed live** — set it and escalations page |
| `SLACK_FIN_OPS_CHANNEL` | Slack escalation message channel (manager.escalation) | **Consumed live** |
| `SLACK_BOT_TOKEN` / `SLACK_SIGNING_SECRET` | Swap the HITL inbox from Hosted Studio to Slack | ⚠️ Referenced ONLY by the **commented-out** `channels: slack:` block on the human gates — uncomment that block in `config.yaml` to use them; env alone does nothing |
| HITL operator IDs | Route gates to named operators instead of the shared inbox | `operator_ids` is **left unset** on every gate → tasks go to the shared inbox. Set real emails only — a placeholder makes tasks invisible |

## Run it

```bash
pip install -r requirements.txt
docker run -d -p 6379:6379 redis:7-alpine   # or: docker compose up redis -d
cp env .env                                  # fill at least Tier 1
python validate_config.py                    # pre-flight: wiring, yields, dead-code
python main.py                               # API at http://127.0.0.1:18820/docs
```

Send a synthetic invoice:

```bash
curl -X POST http://127.0.0.1:18820/api/mesh/request \
  -H "Content-Type: application/json" \
  -d '{
    "entry_point": "manual_entry",
    "data": {
      "source": "manual",
      "vendor_id": "v-acme-001",
      "vendor_name": "Acme Supplies",
      "amount_cents": 250000,
      "currency": "USD",
      "po_number": "PO-12345",
      "due_date": "2026-07-15"
    }
  }'
```

Worked examples (happy path, fraud signal, PO mismatch, CFO sign-off,
vendor inquiry) live in `examples/` — run them with
`python examples/run_examples.py`.

## Trust mechanisms

- **The approval matrix is on the wires** — explicit, commented conditions
  on `gl_coding_agent`'s outbound edges. A controller reads the approval
  policy without opening Python, and tunes it by editing YAML.
- **Hash-chained WORM audit sink** — every transition writes a line
  carrying the SHA-256 of the prior line; any insertion/deletion is
  detectable post-hoc.
- **HITL fallback is escalation, never auto-approval** — clerk timeout →
  Controller; Controller/CFO timeout → halt for review. Default-deny,
  encoded in YAML.
- **`enforce_yields` with retry on every LLM agent** — bad shape triggers
  Summarizer-driven correction, not a silent partial result. Every LLM
  prompt carries an `OUTPUT CONTRACT` listing every yield key.
- **Bounded back-edge** — the intake↔matching re-extraction cycle is
  capped (`po_retry_exhausted` via the session stash) so it can never
  ping-pong into the SDK chain cap.
- **Fail-closed actuator** — erp_poster refuses to post an entry it can't
  attach to a recovered invoice; nothing posts anonymously.
- **Knowledge-grounded LLMs** — policy edits propagate immediately, no
  model retraining.
- **Self-reliant agents (`@pre_compose`)** — matching pulls its own PO +
  goods receipt, payment_risk pulls the vendor's posted history, gl_coding
  pulls the active chart. Each fetches its own authority from the source
  store (the ERP in prod) rather than trusting an upstream LLM's echo.
- **Deterministic floors (`@chain`) the LLM can't waive** — the 3-way
  **tolerance arithmetic** (matching), the **duplicate-hashing** verdict
  (payment_risk, strict + loose SHA-256), the **GL-validity** check
  (gl_coding), and the dollar-threshold **approval matrix** (gl_coding's
  wires). Each is real Python the model cannot talk its way past.
- **Seeded dev store** — `agency/_shared/store.py` ships realistic
  invoices + matching POs (with goods receipts) + a vendor master + a GL
  chart + a posted ledger, so the full chain runs end-to-end with **no
  connectors set**. Every read is the contract a production connector must
  satisfy; swap the body and the agents are unchanged.
- **YAML-first** — Python remains only where it must: the ERP actuator,
  matching's PO lookup + tolerance floor + loop guard, payment_risk's
  dedupe floor, gl_coding's chart pull + validity floor + stash, and the
  loan domain math. System-of-record agents (payment_scheduler,
  close_cycle) are connector-only.

## Tests

```bash
pip install -r requirements-dev.txt
pytest tests/ -q
```

Coverage: config-shape invariants (every `can_call` condition valid, the
approval matrix explicit on gl_coding's wires, HITL fallbacks set, audit
sink wired, connector-only agents carry `integration` + `connector_config`);
unit tests for the kept Python (matching phases + loop guard, gl_coding
fallback chart + stash, erp_poster fail-closed); and `tests/test_floors.py`
for the deterministic floors (3-way tolerance catches a real mismatch,
strict + loose duplicate hashing, GL-validity forces Controller, approval
tier forced by amount, the store seeds).
