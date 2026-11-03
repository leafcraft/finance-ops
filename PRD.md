# finance-ops — Finance-Operations Pod — Product Requirements / Build Doc

> A pod of five digital employees + four human roles that runs a payable end to
> end: accounts payable, accounts receivable, and the month-end close. An invoice
> enters from any of seven triggers; **Ivy** (Intake & Extraction Analyst, LLM +
> super_agent) reads it with a confidence score on every field, classifies the
> lane (AP / AR / close), and runs the policy pre-check — refusing to guess.
> **Ray** (Payment-Risk Officer, LLM) runs a deterministic duplicate pre-flight +
> fraud verdict. **Mat** (Matching & GL-Coding Builder, LLM) matches the PO / goods
> receipt, codes the entry against the live chart, cross-checks the totals, and
> renders the coding sheet — the dollar-threshold **approval matrix rides its
> outbound wires**, routing to the AP clerk, Controller, or CFO by amount,
> confidence, and anomaly. On a real human sign-off, **Poe** (ERP Poster, a
> fail-closed programmatic FINISHER) re-checks the approval, posts the entry, and
> records a hash-chained WORM line. **Cora** (Coordinator) owns the case and keeps
> live status. Above $500K two people sign: the CFO, then the Controller —
> Poe enforces it. The **Manager** supervises and escalates errors to the
> **Finance-Ops Lead**.
>
> Built on LeafMesh as an **operation of self-reliant agents**: the approval
> policy is explicit on the wires (no router agent); the deterministic floors the
> model cannot waive live in `@chain` (confidence, dedupe, 3-way tolerance,
> GL-validity, the finisher's approve-token re-check); systems (email / portal /
> ERP) are tool-chips inside agents, never members; and nothing is approved or
> posted on silence.

---

## 1. The operation at a glance

```
  an invoice enters (email · portal · ERP webhook · manual · status ask · close sweep · HITL reply)
        │
        ▼
   Cora — Coordinator (opens the case, keeps status)
        │  action == open_case
        ▼
   Ivy — Intake & Extraction Analyst  (LLM + super_agent + playbook + scaling.copies)
        │   @chain confidence floor — a low-confidence field is a QUESTION, not a guess
        ├── extract_clean == false & question ─────► AP Clerk (answer)
        ├── policy_flags != [] ────────────────────► Finance-Ops Lead
        ├── extract_clean & anomaly_risk ──────────► Ray  (risk screen)
        └── extract_clean & no anomaly ────────────► Mat  (match + code)
                                                       ▲
   Ray — Payment-Risk Officer (LLM)                    │ fraud_score < 0.4 & not duplicate
        │   @chain dedupe floor — a confirmed dup ─► Controller (forced review)
        ├── fraud_score >= 0.7 or is_duplicate ────► Controller
        ├── 0.4 <= fraud_score < 0.7 ──────────────► Finance-Ops Lead
        └── clean ─────────────────────────────────► Mat
                                                       │
   Mat — Matching & GL-Coding Builder (LLM)            │
        │   @chain cross-check floor — PO/3-way + GL-validity + tie-out; renders ./out sheet
        ├── cross_check_ok == false & !exhausted ──► Ivy   (bounded back-edge, 1 round)
        ├── cross_check_ok == false & exhausted ───► Finance-Ops Lead (terminal)
        │   ── APPROVAL MATRIX on the wires ──
        ├── < $5K, conf >= 0.7, no anomaly ────────► AP Clerk
        ├── < $50K, or >= $5K / low-conf / anomaly ► Controller
        └── >= $50K ───────────────────────────────► CFO
                                                       │ human_message == approve
                                                       ▼
   Poe — ERP Poster & Ledger Liaison (programmatic FINISHER)
        │   @chain posting floor — approve-token re-check → invariants → dual sign-off → post → render → delete-on-block
        ├── > $500K with only the CFO's approval ──► Controller (second signer) ─ approve ─► Poe
        └── otherwise (posted / blocked) ──────────► audit_logger (WORM terminus)

   every human gate: reject / hold → audit_logger. No answer within the reply
   window → the Inbox card closes and the ticket records that no reply came;
   nothing is approved or posted.
```

One human, one agent, one system per stage: **the agent produces, the human
decides, the system records. No stage advances on an agent's own authority.**

## 2. Members (and why each is a member, not a tool)

| Member | Kind | One-sentence job (the hiring test — no "and") |
|---|---|---|
| Cora `fin_coordinator_agent` | AI (llm, chain) | Owns each invoice case and keeps its live status. |
| Ivy `invoice_intake_agent` | AI (llm, super_agent) | Turns a raw invoice into confidence-scored, source-traced line-items. |
| Ray `payment_risk_agent` | AI (llm, chain) | Decides whether an invoice is a duplicate or a fraud risk. |
| Mat `matching_gl_agent` | AI (llm, chain) | Matches and GL-codes an invoice into an approvable entry. |
| Poe `erp_poster_agent` | AI (programmatic, chain) | Posts an approved entry to the ERP. |
| Finance-Ops Lead `finance_ops_lead_human` | Human | Owns every exception and policy call that leaves the pod. |
| AP Clerk `ap_clerk_human` | Human | Answers extraction questions and signs off routine entries. |
| Controller `controller_human` | Human | Signs off exceptions and mid-value entries. |
| CFO `cfo_human` | Human | Signs off high-value entries. |
| Audit `audit_logger_agent` | External (custom) | Records every outcome, immutably. |

**Systems are tools, not members.** Email, the vendor portal, and the ERP cannot
act on an intent given context — they are tool-chips inside Ivy (reads), Mat (PO /
goods-receipt reads), and Poe (the ERP post). Data never leaves them as a member;
it is pulled by the agent that operates them.

## 3. The four modern capabilities (fitted to the domain)

- **playbook** — Ivy carries `ap_intake` / `ar_intake` / `close_sweep` /
  `policy_precheck` lanes; Mat carries `code_entry`. Only the `description` rides
  in the prompt; the `responsibility` (the craft) loads on demand when the model
  recognises the lane. The prompt never restates the playbook body — that would
  kill it (a resident copy always wins over a tool call).
- **super_agent** (dict form) — Ivy plans/executes/verifies across a dense
  multi-line invoice under a `cost_ceiling` + `wall_clock` budget.
- **scaling.copies** — one activation of Ivy → 3 parallel copies
  (`scaling: {copies: {mode: fixed, count: 3, handoff: each}}`). On the month-end
  `daily_close_sweep`, each copy calls the **atomic claim tool**
  (`claim_next_invoice`) to pull a DISJOINT invoice from the backlog;
  `copies.handoff: each` routes every copy on its own yields, so N invoices
  flow independently. (`copies.count: 1` for a pure one-at-a-time deployment.)
- **mcp** — the ERP + mailbox connector shape is declared but **commented** on Ivy
  (the SDK rejects an empty-URL http mcp at load); the dev-store tools are the
  day-0 fallback. **timezone** is set next to Ivy's `wake_up` (crons fire in UTC
  otherwise).

## 4. The deterministic floors (what a prompt can't waive)

| Agent | Floor (`@chain`) | Fails closed by… |
|---|---|---|
| Ivy | `enforce_intake_floor` | forcing the extract un-clean on any low-confidence / missing field, turning it into ONE question; re-asserting the anomaly-risk policy flags (≥ $10K, round thousand, unknown vendor, or already in the posted ledger — model may raise, never lower), so every possible duplicate reaches Ray. |
| Ray | `enforce_dup_floor` | SHA-256 dedupe (strict + loose) vs the posted ledger; a hit forces `is_duplicate` + `fraud_score ≥ 0.85` + `controller_review`; clamps the no-≥0.7-on-one-signal rule. |
| Mat | `enforce_cross_check_floor` | PO/3-way tolerance arithmetic; GL-validity (inactive account → confidence < 0.7 → Controller); tie-out; a fail is a **bounded** back-edge to Ivy (1 round → then the Lead). |
| Poe | `enforce_posting_floor` | approve-token allowlist + block-token denylist (unknown → blocked); invariants (invoice / GL / positive amount / not already in the posted ledger); **dual sign-off above $500K** — CFO approval alone → `awaiting_second_signature` → Controller; posts only with both, from two different people (sign-off register in the dev store); **deletes the partial pack** on a trip; posts nothing on failure. |

## 5. Trust mechanisms (the pod discipline)

- **Every wire is conditional** on the producer's declared yields — routing is
  readable in `configs/config.yaml`, never hidden in Python.
- **Nothing moves on silence** — a reject or hold goes to the audit sink. If
  nobody answers within the reply window (AP Clerk 4 h, Controller 8 h, CFO
  24 h, Lead 3 days), the Inbox card closes and the ticket records that no
  reply came; nobody is chased automatically. `fallback_on_timeout` +
  `fallback_response → held_for_review` stay in the config as the declared
  intent; on leafmesh 2.4.486 a dual seat's timeout does not route it yet.
- **`dual` only on the human boundary** — the AI specialists are all `chain`
  (dual→dual is a warned footgun). Humans never call other humans; tier selection
  happens on Mat's matrix, and the second signer above $500K is reached through
  Poe. The Manager escalates errors only (a non-retryable error, 5 failed agents
  in a session, restarts used up).
- **Bounded back-edges** — Mat→Ivy re-extraction counts its round in the session
  stash; on exhaustion it terminates to the Lead.
- **WORM terminus** — `audit_logger_agent` (external + custom) hash-chains every
  post, approval, and held gate; registered via `sdk.intelligence()` in `main.py`.
- **dev-store that announces itself** — every intake tool returns
  `available: false` with a reason when it has no real source; Poe posts a
  verifiable dev-ledger row, never a synthetic ack.

## 6. Going live (swap points, agents unchanged)

| Day-0 dev stand-in | Production swap |
|---|---|
| Seed source store (`agency/_shared/store.py`) | ERP vendor master / PO / goods-receipt / GL chart / AP ledger reads (create the vault connection `erp_mcp` on Infrastructure → Integrations → MCP and uncomment Ivy's `mcp` block — in `configs/config.yaml` before the first start, in Studio's YAML editor after it — or swap the function bodies) |
| Atomic claim (in-memory lock) | `LPOP count` on a Redis list, or `UPDATE … FOR UPDATE SKIP LOCKED RETURNING` on the ERP's unposted table |
| SQLite ERP ledger (`agency/_shared/erp_poster.py`) | NetSuite RESTlet / SAP OData / QuickBooks / Xero / Dynamics — swap `ERPDispatcher.post()` |
| Local-file WORM sink | S3 Object Lock / Datadog Audit / Splunk HEC — swap `_dispatch_local` |
| Session stash (in-process) | Redis session store — same get/put interface |

## 7. Verification

- `python validate_config.py` — structure (wiring, yields↔prompt contract,
  timeout routes, cron shape, dead-code). Must print **all checks pass**.
- Day-0 dev-store run — the chain, against the seed store, no connector.
- `tests/` — config-shape assertions + the finisher/floor fail-closed unit tests
  (no LLM / Redis needed). `examples/` — end-to-end scenario payloads.
- State which layer you verified: `validate_config` proves structure, not runtime.
