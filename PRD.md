# Finance Operations — Product Requirements / Build Doc

> A peer-to-peer finance back-office mesh: accounts payable, accounts
> receivable, and the monthly close. An invoice enters from any of ten
> channels; a **records intake specialist** (LLM) normalizes it into the
> canonical `InvoiceEvent` and sets policy routing flags. A **PO-matching
> clerk** matches it against the ERP (and runs the 3-way match when
> required); a **payment-risk officer** (LLM) runs a duplicate pre-flight
> + fraud verdict. Both fan into a **GL accountant** (LLM), whose outbound
> wires *are* the dollar-threshold approval matrix — routing to the **AP
> clerk**, **Controller**, or **CFO** by amount, confidence, and anomaly.
> On sign-off, an **AP posting specialist** (fail-closed Python actuator)
> writes the entry to the ERP and a **compliance recorder** appends a
> hash-chained WORM line. A **vendor relations specialist** drafts vendor
> queries and answers vendor inquiries; a **compliance officer** runs KYC
> + onboarding on new counterparties; **treasury**, **close**, **credit**,
> and **regulatory** roles run as scheduled / connector-only citizens.
>
> Built on LeafMesh as an **operation of self-reliant agents**: the
> approval policy is explicit on the wires (no router agent), state lives
> in the customer's ERP / treasury via MCP connectors, and every LLM
> agent pulls its own knowledge group and enforces its own output
> contract.

---

## 1. The operation at a glance

```
 an invoice enters (email · portal · ERP webhook · mobile · manual ·
   monthly-close cron · vendor inquiry · new-vendor first invoice ·
   duplicate sweep · payment-run kickoff)
        │
   ┌────▼──────────────────────────────────────────────────────────┐
   │ invoice_intake_agent  (LLM, knowledge: vendor_master)          │
   │  • normalize any source → canonical InvoiceEvent               │
   │  • extract lines / tax / terms                                 │
   │  • set policy flags: requires_po_match, requires_3way_match,   │
   │    anomaly_risk  (deterministic floors stated in the prompt    │
   │    AND backed on the wires)                                    │
   └──┬───────────────────────────────┬────────────────────────────┘
      │ if PO / 3-way required         │ if anomaly / ≥ $10K / new vendor
   ┌──▼──────────────────────┐   ┌─────▼───────────────────────────┐
   │ matching_agent          │   │ payment_risk_agent  (LLM)       │
   │ (programmatic)          │   │  • duplicate ledger pre-flight  │
   │  • PO lookup + line     │   │  • fraud verdict (≥ 0.7 needs   │
   │    match (ERP stub)     │   │    two signals; dup stands      │
   │  • 3-way when required  │   │    alone)                       │
   │  • bounded re-extract ──┼─► │  score ≥ 0.7 → controller_human │
   │    back to intake       │   │  dup / 0.4–0.7 → vendor_comms   │
   │  • gap → vendor_comms   │   └─────┬───────────────────────────┘
   └──┬──────────────────────┘         │ clean (inline)
      │ clean PO / 3-way               │
   ┌──▼─────────────── fan-in: wait_for invoice_intake AND
   │                    matching? AND payment_risk? ────────────────┐
   │ gl_coding_agent  (LLM, knowledge: chart_of_accounts)           │
   │  • GL account / cost center / tax code                         │
   │  • stash invoice+GL context for post-gate recovery             │
   │  • THE APPROVAL MATRIX rides the outbound wires:               │
   └──┬──────────────────┬──────────────────┬──────────────────────┘
      │ < $5K, conf ≥ .7  │ < $50K otherwise │ ≥ $50K
      │ no anomaly        │ (or low conf /   │ (dual-sign ≥ $500K)
      ▼                   ▼  anomaly)        ▼
  ap_clerk_human    controller_human    cfo_human   (HITL — shared inbox)
      │ approve            │ approve          │ approve
      └─────────┬──────────┴──────────────────┘
                ▼
        erp_poster_agent  (programmatic actuator — fail-closed;
          recovers invoice from the pre-gate session stash)
                │
                ▼
        audit_logger_agent  (external custom — hash-chained WORM, terminus)

  side-arm (callable from matching / payment_risk / loan_servicing, and
  its own four entry points):
     vendor_comms_agent  (LLM — exception_query | inquiry_response)
        → ap_clerk_human on requires_human_review

  pre-payment, off the pay path:
     payment_scheduler_agent  (MCP → treasury) → erp_poster_agent

  gatekeeper, off new_vendor_first_invoice / kyc_verify:
     vendor_compliance_agent  (LLM — KYC + onboarding, fail-closed)
        → controller (SAR/EDD) / ap_clerk (review) / invoice_intake (clean)

  scheduled / connector citizens (NOT in the per-invoice flow):
     close_cycle_orchestrator_agent  (MCP sweep of your ERP)
        → controller (gaps) / audit (clean)
     loan_origination_agent  (credit underwriting matrix)
        → controller / cfo on manual review
     loan_servicing_agent  (cron 05:00 — payments / payoff / delinquency)
        → controller (write-off) / vendor_comms (collections)
     regulatory_monitor_agent  (cron 07:00 — reg-feed scan, memory-deduped)
        → controller on immediate/high alerts
```

The chain flows **forward** — intake fans out, the specialists hand into
gl_coding, gl_coding routes to the right human tier, and the work lands in
the ERP and the audit. The Manager hovers above (learning routing on
non-regulated paths) and intervenes only on narration cues, escalation, or
`enforce_yields` contract violations — it is **not** in the happy path.
The one intentional cycle (matching↔intake re-extraction) is **bounded**
(see §4).

---

## 2. Actor inventory

Role = a human job title; every actor reads like a role on a real finance
team.

| Agent | Role | Class | One-line |
|---|---|---|---|
| `invoice_intake_agent` | Records intake specialist | 🤖 LLM | Normalize any source → `InvoiceEvent`; extract fields; set routing flags |
| `matching_agent` | PO-matching clerk | ⚙️ programmatic | PO lookup + line match, then 3-way match; bounded re-extraction loop |
| `payment_risk_agent` | Payment-risk officer | 🤖 LLM | Duplicate pre-flight + fraud verdict (fraud playbook + vendor history) |
| `gl_coding_agent` | GL accountant | 🤖 LLM | GL / cost-center / tax coding; carries the approval matrix on its wires |
| `vendor_comms_agent` | Vendor relations specialist | 🤖 LLM | Drafts exception queries TO vendors + answers vendor inquiries |
| `vendor_compliance_agent` | Compliance officer | 🤖 LLM | Banking-grade KYC + new-vendor onboarding gate (fail-closed) |
| `ap_clerk_human` | AP clerk | 👤 human | First-line review of routine invoices < $5K |
| `controller_human` | Controller | 👤 human | Regulated sign-off, exceptions, $5K–$50K, close-cycle owner |
| `cfo_human` | Chief Financial Officer | 👤 human | High-value sign-off ≥ $50K; dual-sign ≥ $500K |
| `erp_poster_agent` | AP posting specialist | ⚙️ programmatic (actuator) | Posts the approved invoice to the ERP; fail-closed |
| `payment_scheduler_agent` | Treasury analyst | 🧩 connector (MCP) | Pay-date + early-pay discount capture in your treasury system |
| `close_cycle_orchestrator_agent` | Month-end close lead | 🧩 connector (MCP) | Sweeps your ERP for unposted entries + reconciliation gaps |
| `loan_origination_agent` | Credit underwriter | ⚙️ programmatic (domain math) | FICO / DTI / DSCR underwriting matrix + tiered approval |
| `loan_servicing_agent` | Loan servicing officer | ⚙️ programmatic (domain math) | Payment posting / payoff quotes / daily delinquency sweep |
| `regulatory_monitor_agent` | Regulatory analyst | 🤖 LLM (cross-session memory) | Daily reg-feed scan; classify finance-process impact; memory-dedupe |
| `audit_logger_agent` | Compliance recorder | 🧾 external (WORM) | Hash-chained record — terminus of every flow |

> **Why three system-of-record agents are connector-only, not Python.**
> `payment_scheduler` (treasury) and `close_cycle_orchestrator` (ERP
> sweep) make decisions that belong to the customer's systems (Kyriba /
> SAP TRM / NetSuite / Bill.com; NetSuite / SAP / QuickBooks). They are
> `integration: mcp` agents — the MCP tool returns the declared yields,
> no template code owns that logic. `erp_poster`, by contrast, is kept as
> Python because the *write* is a real side effect that must fail closed.

---

## 3. Data artifacts

- **InvoiceEvent** — `{invoice_id, source, source_event_id, received_at_utc,
  vendor_id, vendor_name, amount_cents, currency, po_number, line_items[],
  due_date, raw_payload}` (canonical schema; `source_event_id` is the
  idempotency key). Declared in `data_structures` in `config.yaml`.
- **Extraction yields** (intake) — adds `tax{}`, `payment_terms`,
  `requires_po_match`, `requires_3way_match`, `anomaly_risk`.
- **Match result** (matching) — `{po_found, po_id, lines_matched,
  match_percentage, discrepancies[], three_way_matched, receipt_found,
  gaps[], po_retry_exhausted}`.
- **Risk verdict** (payment_risk) — `{fraud_score, flagged_signals[],
  is_duplicate, duplicate_of_invoice_id, recommendation, controller_briefing,
  detection_mode, risk_check_mode}`.
- **GL coding** (gl_coding) — `{gl_account, cost_center, tax_code,
  confidence}` + identity passthrough (`amount_cents`, `anomaly_risk` drive
  the approval wires).
- **ERP posting** (erp_poster) — `{erp_entry_id, posted, posted_at_utc,
  erp_type, invoice_id, error}`.
- **Audit line** (audit_logger) — `{audit_event_id, written_at_utc,
  hash_chain_prev, hash_chain_current, payload}`.

State of record lives in the customer's **ERP** (postings, POs,
goods-receipts), **treasury** (pay calendar, cash position), and **knowledge
service** (vendor master, fraud playbook, chart of accounts, sanctions
lists). The dev build ships a **seeded source store**
(`agency/_shared/store.py`) — realistic invoices + matching POs (with goods
receipts) + a vendor master + a GL chart + a posted-invoice ledger — so the
**full chain runs end-to-end with no connectors set**. Each agent pulls its
own slice via `@pre_compose` (matching → the PO + goods receipt;
payment_risk → the vendor's posted history; gl_coding → the active chart);
every read function is the contract a production connector must satisfy
(swap the body, the agents are unchanged). erp_poster writes posted
invoices back into the store's ledger so the duplicate pre-flight sees real
history accumulate. The dev build also ships an SQLite ERP ledger
(`agency/_shared/erp_poster.py`) and SQLite loan books as runnable stubs;
production swaps `ERPDispatcher.post()` and wires the MCP connectors. The
audit sink is a local hash-chained JSONL file in dev (S3 Object Lock /
Splunk HEC in prod).

---

## 4. Per-agent spec

### `invoice_intake_agent` — records intake specialist (LLM)
- **Why LLM:** judgment over heterogeneous source payloads (email OCR,
  portal JSON, mobile photo) into one schema.
- **Knowledge:** `vendor_master` (vendor-specific extraction hints + PO
  policy). **Enforce:** `enforce_yields` retry 3.
- **Deterministic floor (in prompt AND on the wires):** ≥ $1K with a PO →
  PO match; ≥ $5K → 3-way; ≥ $10K / round-thousand / first-time vendor →
  anomaly. The model may *raise* a flag the rules missed, never lower one.
- **Routes:** matching (PO/3-way), payment_risk (anomaly / ≥ $10K / empty
  vendor_id), gl_coding (`condition: true` — always coded).

### `matching_agent` — PO-matching clerk (programmatic)
- **Why Python:** ERP lookup + domain math + the loop guard.
- **`@pre_compose load_po_context`** pulls the PO (+ its goods receipt)
  and the vendor record from the source store (the ERP in prod) so
  matching fetches its own authority, not the upstream LLM's echo.
- **`@chain enforce_match_floor`** is the deterministic 3-way **tolerance**
  floor (`agency/_shared/matching_math`): qty / unit-price variance is
  checked against fixed tolerances (`QTY_TOLERANCE_UNITS`,
  `PRICE_TOLERANCE_PCT`); any line outside tolerance is a gap and fails
  the match. The body can't present a mismatch as clean.
- **Loop guard:** the intake↔matching re-extraction cycle is intentional
  (one fresh pass with PO context) but **must terminate**:
  `_MAX_REEXTRACTION_ATTEMPTS = 1`, counted in the session stash; on
  exhaustion `po_retry_exhausted = true` steers to vendor_comms.
- **Routes:** gl_coding (clean match), invoice_intake (re-extract, bounded),
  vendor_comms (partial / failed / exhausted).

### `payment_risk_agent` — payment-risk officer (LLM + micro-module)
- **Knowledge:** `fraud_playbook` (red flags + vendor history). Retry 2.
- **`@pre_compose load_dup_candidates`** pulls the vendor's prior posted
  invoices from the source store (the AP ledger in prod) so the duplicate
  check has real evidence, not a hallucinated knowledge-base recall.
- **`@chain enforce_dup_floor`** is the deterministic **duplicate-hashing**
  floor (`agency/_shared/dedupe`): strict (vendor+invoice_id) and loose
  (vendor+amount+due_date) SHA-256 keys; a hit forces `is_duplicate` +
  fraud_score ≥ 0.85 + controller_review. The floor also clamps the
  cardinal rule.
- **Cardinal rule (now floor-enforced):** never score ≥ 0.7 on one signal —
  the floor caps a single-signal score at 0.55; the sole exception is a
  confirmed duplicate, which forces ≥ 0.85 → controller_review.
- **`risk_check_mode`** (`preflight` | `inline`) is set from whether a
  `detection_mode` input was passed; it drives the clean-path edge so a
  standalone `duplicate_check` returns to intake while an inline check
  proceeds to gl_coding.
- **Routes:** controller (≥ 0.7), vendor_comms (dup or 0.4–0.7), gl_coding
  (clean inline), invoice_intake (clean preflight).

### `gl_coding_agent` — GL accountant (LLM + micro-module)
- **Knowledge:** `chart_of_accounts`. Retry 2.
- **`@pre_compose load_chart`** pulls the active chart of accounts + the
  vendor's standing default GL mapping from the source store (the ERP GL
  master in prod), so the LLM codes against the real chart even before the
  knowledge group is seeded.
- **Micro-module (`agency/gl_coding_agent.py`)** then: (1) the **GL-validity
  floor** — a coded account that is not an active account in the chart is
  rejected, forcing confidence below the 0.7 wire floor so the approval
  matrix routes it to the Controller (the LLM can't post to a non-existent
  account); (2) prefer the vendor default, else a **keyword fallback chart**
  when the LLM returns no coding (hits 0.72, misses 0.4); (3) **stash** the
  invoice + GL context per session so erp_poster recovers it after the gate.
- **The approval matrix** is explicit conditions on its three outbound
  wires — readable and tunable in YAML, no router agent.

### `vendor_comms_agent` — vendor relations specialist (LLM)
- **Mode-dispatched:** `exception_query` (called by matching / payment_risk
  / loan_servicing to ASK a vendor) and `inquiry_response` (answers
  vendor inquiries from its own four entry points). Retry 2.
- **`requires_human_review`** is forced true on fraud / dispute / aging
  / too-short replies — the only outbound route is to ap_clerk for review.

### `vendor_compliance_agent` — compliance officer (LLM)
- **Fail-closed gate:** KYC runs always; onboarding only on an approved
  KYC. `onboarded_to_master = true` ONLY when status approved AND no
  missing fields AND OFAC-clear; otherwise a human reviews. The full
  scoring matrix is stated verbatim in the prompt (regulator-readable).
- **Routes:** controller (SAR / EDD), ap_clerk (review), invoice_intake
  (clean onboarding only — mode-guarded so a pure-KYC counterparty never
  enters the invoice chain).

### Human gates — `ap_clerk_human` / `controller_human` / `cfo_human`
- **Channel-native HITL** on the Hosted Studio shared inbox
  (`human_interface: default`; `operator_ids` left unset). Default-deny on
  timeout: clerk (1h) → Controller; Controller (2h) / CFO (4h) → halt for
  review. Route on `human_message` (human yields aren't populated for
  human agents). Every rejection / halt lands in the audit.

### `erp_poster_agent` — AP posting specialist (programmatic actuator)
- **Fail-closed:** after a human gate the SDK resumes with only meta
  fields, so the invoice is recovered from the gl_coding session stash; if
  nothing is recoverable, **nothing is posted** (returns `error`). Reads
  `ERP_TYPE`; dev ledger in `_shared/erp_poster.py`. → audit_logger.

### `payment_scheduler_agent` / `close_cycle_orchestrator_agent` — connector-only (MCP)
- `integration: mcp`; the MCP tool (`schedule_payment` / `close_cycle_sweep`)
  returns the declared yields. close_cycle's cron ships **commented** —
  a cron against an unwired connector just fills the logs with errors; the
  `monthly_close` entry point still allows a manual kick.

### `loan_origination_agent` / `loan_servicing_agent` — credit roles (programmatic)
- **Domain math** that must never drift: the underwriting matrix (FICO /
  DTI / DSCR / rating + amount tiers) and the interest/principal split +
  delinquency bucketing. servicing runs a daily 05:00 sweep. Routes to
  controller / cfo / vendor_comms on the manual-review / write-off /
  collections conditions.

### `regulatory_monitor_agent` — regulatory analyst (LLM)
- **Cross-session memory** (replaces an SQLite dedup store) remembers what
  it already reported across daily runs. Daily 07:00 cron; knowledge
  `regulatory_taxonomy`. → controller on immediate/high alerts.

### `audit_logger_agent` — compliance recorder (external custom, WORM)
- `framework: custom`, registered via `mesh.intelligence("audit_logger_agent")`
  in `main.py`. Each line carries the SHA-256 of the prior line; terminal.

---

## 5. Entry points

Twenty-five entry points — 19 functional + 6 direct-call (Rule 1 — work
starts from many places):

- **Invoice ingress (5)** — `email_invoice`, `portal_invoice`,
  `erp_webhook`, `mobile_capture`, `manual_entry` → invoice_intake.
- **Scheduled / kicked (1)** — `monthly_close` → close_cycle_orchestrator.
- **Vendor-facing (4)** — `vendor_inquiry`, `payment_status_query`,
  `dispute_resolution`, `tax_form_request` → vendor_comms (inquiry_response).
- **Compliance / risk (2)** — `new_vendor_first_invoice` →
  vendor_compliance; `duplicate_check` → payment_risk (preflight).
- **Treasury (1)** — `payment_run_kickoff` → payment_scheduler.
- **Banking sub-variant (3)** — `kyc_verify` → vendor_compliance;
  `loan_application` → loan_origination; `loan_payment_posting` /
  `loan_payoff_quote` / `loan_delinquency_sweep` → loan_servicing;
  `regulatory_daily_scan` → regulatory_monitor.
- **Direct-call (6)** — every non-human, non-terminal agent is
  API-addressable for ad-hoc / debugging invocation.

---

## 6. Controls / trust

- **Approval matrix on the wires** — explicit conditions on gl_coding's
  outbound edges; the policy is the config, auditable without opening
  Python.
- **Default-deny HITL** — every gate escalates or halts on timeout; never
  auto-approves.
- **Bounded back-edge** — intake↔matching re-extraction capped via the
  session stash.
- **Fail-closed actuator** — erp_poster never posts an unrecoverable
  invoice.
- **yields↔prompt contract** — every LLM prompt carries an `OUTPUT
  CONTRACT` listing every yield key; `enforce_yields` + retry on each.
- **Hash-chained WORM audit** — terminus of every branch; tamper-evident.
- **Resilient escalation** — manager.escalation fans to a human target, a
  PagerDuty webhook, and a Slack channel so a single-channel outage
  doesn't swallow an alert.

---

## 7. Configuration pointer

Three tiers, all set in `.env` — see **README.md → Configuration**:
Tier 1 (boot: `LEAFMESH_LICENSE_KEY`, `LEAFMESH_ENV_TOKEN`,
`OPENAI_API_KEY`, Redis); Tier 2 (host / adapters: `ERP_TYPE`, the two MCP
URL/token pairs, `FINANCE_OPS_ERP_LEDGER`, `GL_DEFAULT_COST_CENTER`, and
the knowledge groups); Tier 3 (connectors / live: `PAGER_WEBHOOK`,
`SLACK_FIN_OPS_CHANNEL` live; `SLACK_BOT_TOKEN` / `SLACK_SIGNING_SECRET`
only with the commented Slack channel block; operator IDs left unset for
the shared inbox).

---

## 8. Reflection / decisions

- **No router agent.** An earlier `approval_router_agent` was removed — the
  dollar thresholds are explicit conditions on gl_coding's wires. A
  controller reads the approval policy in YAML; there is no hub everything
  returns to (forward-flow DAG, Rule 5).
- **Rule-2 merges.** `payment_risk` is one officer doing both the duplicate
  pre-flight and the fraud verdict; `vendor_comms` is one specialist for
  all vendor correspondence in both directions; `vendor_compliance` is one
  officer for KYC *and* onboarding. Each reads like a real role rather than
  a thin function.
- **System-of-record agents are connector-only.** Treasury scheduling and
  the ERP close sweep belong to the customer's systems — they are
  `integration: mcp`, not Python. The dev SQLite ledgers are runnable
  stubs, never the system of record (Rule 7).
- **Self-reliant agents + deterministic floors.** The three key
  judgment agents pull their own context via `@pre_compose` from the
  seeded source store (matching → PO + goods receipt; payment_risk →
  vendor posted history; gl_coding → active chart) and enforce their own
  floors via `@chain` that the LLM cannot waive: the 3-way **tolerance
  arithmetic** (`_shared/matching_math`), the **duplicate-hashing**
  verdict (`_shared/dedupe`), and the **GL-validity** check. The rules
  that were prose-in-the-prompt are now real Python the model can't
  argue past.
- **Python only where a role needs hands.** The ERP actuator (fail-closed
  write), matching (store-grounded PO lookup + tolerance floor + loop
  guard), payment_risk (dedupe floor), gl_coding (chart pull + validity
  floor + stash), the loan domain math, and the shared store / dedupe /
  matching-math helpers. Everything else is YAML.
- **The HITL stash bridge.** Because the SDK resumes a chain after a human
  gate with only meta fields, gl_coding stashes the invoice before the gate
  and erp_poster recovers it — a documented in-process bridge that swaps to
  the Redis session store in a multi-replica deployment.
- **Open item for the live smoke test.** The session-stash bridge across a
  real human wait, and the `mesh.intelligence()` audit registration firing
  on the terminal edge, are runtime mechanics that can't be proven from
  config-load alone — verify them in a running mesh before production.
