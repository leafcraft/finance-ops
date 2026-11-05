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
approval matrix routes it to the *right* signer by amount; above $500K it needs
two signers, the CFO and then the Controller. Only after a real human
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
**Silence never advances an invoice.** If nobody answers a gate within its reply
window, the request waits no longer: its Inbox card closes and the ticket records
that no reply came. Nothing is approved or posted on silence, and nobody is
chased automatically. **Above $500K two people sign** — Poe records the CFO's
approval, routes the entry to the Controller, and posts only with both
signatures from two different, identified people. Name each seat's people
(`operator_ids` on `cfo_human` and `controller_human`, their Studio logins) so
the Inbox records who signed; if it can't tell who signed, Poe refuses to post.
A rejection or hold voids the earlier signature. Every decision, post, and approval
is hash-chained into a WORM ledger a regulator can replay. ROI: the AP clerk's
keying and chasing time collapses, mis-codes and double-pays are caught before
the money moves, and month-end close stops being a fire drill.

## The team (10 members)

| Agent | Role (human job title) | Responsibilities | Can perform |
|---|---|---|---|
| `fin_coordinator_agent` | Finance-Ops Coordinator | Owns the case end to end; opens it, keeps live status, is the requester's one contact | LLM; orchestrates Ivy→Ray→Mat→Poe; routes exceptions to the Lead; **never extracts, codes, or approves** |
| `invoice_intake_agent` | Invoice Intake & Extraction Analyst | Reads each invoice with a confidence score, classifies AP/AR/close, runs the policy pre-check | LLM + **super_agent** + **playbook** (AP/AR/close lanes) + **scaling.copies** (3 copies, backlog fan-out with an atomic claim tool) + intake tools; `@chain` **confidence floor** (fail closed) |
| `payment_risk_agent` | Payment-Risk Officer | Duplicate ledger pre-flight + fraud/anomaly verdict from vendor history | LLM; `@chain` **dedupe floor** — a confirmed duplicate is forced to controller review |
| `matching_gl_agent` | Matching & GL-Coding Builder | PO/3-way match + GL coding; cross-checks totals tie out; renders the coding sheet | LLM + **playbook**; `@chain` **cross-check floor**; bounded back-edge to Ivy; renders the draft to `./out`; approval matrix on its wires |
| `erp_poster_agent` | ERP Poster & Ledger Liaison | Re-checks approval, posts the approved entry to the ERP, records the ledger | Programmatic **finisher**; approve-token re-check → invariants → post + render pack → **delete-on-block** → WORM ledger |
| `finance_ops_lead_human` | Accountable owner (Human) | Reviews exceptions, owns policy calls, decides every gate | Human gate — everything that leaves the pod |
| `ap_clerk_human` | AP Clerk (Human) | Answers Ivy's questions; signs off routine coding sheets (< $5K) | Human gate |
| `controller_human` | Controller (Human) | Regulated sign-off for exceptions, $5K–$50K, low-confidence coding, flagged risk; second signer above $500K | Human gate |
| `cfo_human` | CFO (Human) | High-value sign-off (≥ $50K); first of two signers above $500K (enforced by Poe) | Human gate |
| `audit_logger_agent` | WORM audit | Hash-chains every decision, post, approval, and held gate | External (custom sink); terminus |

Reply windows (`human_timeout_seconds`): AP Clerk 4 working hours, Controller
8 working hours (one business day), CFO 24 hours, Finance-Ops Lead 3 days. When a
window passes with no answer, the Inbox card closes and the ticket records that
no reply came; nothing is approved, posted or escalated on silence.

The **LeafMesh Manager** supervises. It escalates to the Finance-Ops Lead when an
agent fails with an error a retry cannot fix (auth, bad request, credit, bad
config), when 5 different agents fail in one session, or when restarts run out.
A broken hand-off contract raises an alert only. Policy flags, possible
duplicates and an exhausted re-match reach the Lead on the agents' own wires.

Ivy's three copies split the close backlog with her own atomic claim tool
(`claim_next_invoice`). leafmesh 2.4.484's `scaling.copies.mode: auto` with
`claim_tool:` could replace this manual split (the mesh would claim each item and
start a copy only when there is one); that switch is the owner's decision and is
not made in this template.

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
| *(no setting)* — human members | Human approvals | Answer in Studio's Inbox. To notify an external tool instead, create a vault connection (an "Add custom" record on the Infrastructure page, service type 'Outbound webhook') and set `webhook_config: {connection: <name>}` on the seat. |

### Tier 2 — real invoice connectors (a vault connection, then uncomment the `mcp` block)
| Vault connection (Infrastructure → Integrations → MCP) | Enables |
|---|---|
| `erp_mcp` — your ERP's MCP server (address + token) | Real invoice / PO / goods-receipt reads from your ERP |
| `mail_mcp` — your mailbox's MCP server (address + token) | Real invoice emails + attachments |

> The server address and token are entered **only in the vault** — the SDK
> refuses a `url:` / `token:` written in the config (even as `${ENV_VAR}`). The
> `mcp` block ships **commented** on `invoice_intake_agent` in
> `configs/config.yaml`, already picking `connection: erp_mcp` /
> `connection: mail_mcp`: create the connections, then uncomment — in the file
> before the first start, in Studio's YAML editor after it (applies live, no
> restart; see *Changing the configuration after the first start*). Nothing goes
> in `.env`: the dev tools in `agency/tools.py` check whether the vault record
> `erp_mcp` or `mail_mcp` exists and, once it does, stop pointing at the seed store.

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
Ray's dedupe floor force it to a controller review rather than pay it twice. The
approval request lands in **Studio → Inbox** — answer it there. On approval, Poe posts to the dev ERP ledger and renders the pack to `./out/`.

Other seed invoices to try (`source_ref`), one per route:

| `source_ref` | Amount | What happens |
|---|---|---|
| `evt-acmecloud-0142` | $4,200 | Clean match → AP Clerk → posted |
| `evt-acme-2291` | $980 | No PO cited, vendor requires one → back to Ivy once → Finance-Ops Lead |
| `evt-quint-0099` | $1,440 | Bills 12 desks, PO says 10 → match fails → Ivy → Lead |
| `evt-globex-9001` | $12,000 | Goods receipt short 8 pallets → 3-way match fails |
| `evt-initech-4410` | $80,000 | 16 advisory days, service entry sheet accepted → CFO |
| `evt-snowmelt-0007` | $250,000 | Two-line licence + support → risk screen → CFO |
| `evt-kestrel-0210` | $642,500 | Above $500K → CFO, then Controller as second signer → posted |

> Packaging note: `pip install leafmesh` (core) includes all LLM providers and
> runs the pod. For the real ERP/mail connectors add `leafmesh[mcp]`. Avoid
> `leafmesh[all]` on Python 3.12+ (it pulls `cchardet`, which has no wheel) — add
> only the specific extras you need.

---

*Built with `create-leafmesh`. The config is the department; the PRD is the brief.*
*Design doctrine: `.claude/skills/leafmesh/building-agents-thoroughly.md`.*

## Changing the configuration after the first start

`configs/config.yaml` is only the first-start seed. On the first start (an empty store) the agency imports it as version 1 and from then on keeps its configuration in the store. After that the file is not read again: an edit to it is **not** applied, even after a restart (the boot log says "that edit was NOT applied").

To change a running agency:

- **Studio** — its settings pages, or the raw YAML editor.
- **API** — `PUT /api/yaml/raw` with an `X-API-Key` header and the body `{"yaml_content": "<the whole YAML>", "save_to_disk": true}`, or `PATCH /api/yaml/config` with `{"updates": {...}, "save_to_disk": true}`.

Each save is a new version and applies live on every pod — no restart. (A few settings, such as brokers, make the agency reload itself in place within seconds.) So wherever this README or a comment says "uncomment X in `configs/config.yaml`", do it in the file before the first start, or in Studio's YAML editor after it.

## Changing a running project (Docker)

What reaches a project that is already running (leafmesh 2.4.497). The
image holds the Python code and the first-start `configs/config.yaml`;
`.env` is passed in when the container is created; the store (Redis) keeps
its data on its own volume.

| You change | When it applies |
|---|---|
| An agent in Studio (prompt, model, tools, routes, human seats, schedules, …) | At once, no restart |
| A human seat's `channels` (Slack, email, …) | The agency reloads itself in place within seconds |
| Add or delete an agent in Studio | At once, including routes and schedules |
| A connection in the vault (model keys, MCP, webhooks, connectors, Slack, email) | At once. An agent switched off for a missing connection or address switches back on by itself |
| Anything in `.env` (licence, agency name, store, ports) | After `docker compose up -d` (the container is recreated) |
| Python code (`agency/*.py`: programmatic agents, tools, `@pre_compose`) | After a rebuild: `docker compose up -d --build` |
| `configs/config.yaml` | Never after the first start. It is read only when the store is empty |
| Several app containers on one store | A Studio save reaches every one within about 15 seconds |

- Keep model keys in the vault. A key there wins over the same key in
  `.env` (the log warns), and a change applies at once. From 2.4.497 that
  holds for every value the vault holds (endpoints, organisation / project
  ids, cloud identities); the `.env` value returns when the record goes.
- Code the image does not have: an agent marked pure Python
  (`integration: python`) is switched off until its function is in the image;
  any other programmatic agent stays on, and each call to code it does not
  have fails. Rebuild the image with the code first.
- A new Redis volume is an empty store, so `configs/config.yaml` is imported
  again as version 1.
- **If the agency refuses to start after an upgrade** (a new leafmesh pin,
  then a rebuild: "Configuration has N error(s) in the stored
  configuration"), repair the stored configuration with the agency stopped:

  ```bash
  docker compose run --rm finance-ops leafmesh config check      # each problem, with its line
  docker compose run --rm finance-ops leafmesh config versions   # kept versions: which pass, which is in use
  docker compose run --rm finance-ops leafmesh config rollback   # back to the newest version that passes (saved as a new version)
  ```

  Or `leafmesh config export > config.yaml`, fix it by hand, then
  `leafmesh config import config.yaml` (it is checked before it is saved).

## Where the data store is set

From leafmesh 2.4.482 the store (Redis or Postgres) is set **only in `.env`** — never in `configs/config.yaml`. A `persistence:` block in the YAML is ignored with a warning on leafmesh 2.4.482–2.4.485; from 2.4.486 the agency refuses to start if that block differs from `.env`, and names each `.env` line to add. The project's `.env` sets the store explicitly (`LEAFMESH_STORE`, `LEAFMESH_AGENCY_NAME`, `REDIS_*`).

- Redis (default): `REDIS_HOST`, `REDIS_PORT`, `REDIS_PASSWORD`, `REDIS_DB`; optional `REDIS_DEFAULT_TTL`, `REDIS_SESSION_TTL`, `REDIS_SSL*`, `REDIS_CLUSTER_NODES`.
- Postgres instead: `LEAFMESH_STORE=postgres` plus `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` (optional `POSTGRES_SSLMODE`).
- Or the whole server in one value: `REDIS_URL=redis://user:pw@host:6379/0` (`rediss://` turns TLS on) or `DATABASE_URL=postgresql://user:pw@host:5432/db`. The single names fill in what the URL leaves out.
- With Docker Compose the app reaches Redis as `redis` (the service name); compose sets that for you.
- Check it in Studio → **Infrastructure → Datastores**: it shows whether the store is reachable (never the address or password).

## This mesh's sign-in (leafmesh 2.4.497)

From leafmesh 2.4.497 each mesh signs in to LeafCraft with its own key, not
a key shared by every install.

- **First start.** The mesh makes a key pair and registers it with
  `LEAFMESH_LICENSE_KEY` and `LEAFMESH_ENV_TOKEN` from `.env` (copy the
  environment token from Studio — a placeholder such as `development` is
  refused). With both valid it works at once. If your organisation turned on
  "require approval", the first start stops and says so: approve it in
  Studio → **Keys → SDK access**, then start again. Organisation admins get an
  email for every new registration.
- **Where the key lives.** In the vault, in the store, encrypted with
  `LEAFMESH_VAULT_KEY`: it survives rebuilds and every copy of the app shares
  it. A new Redis volume, or a new vault key, makes a new key pair that
  registers again — revoke the old one in Studio.
- **Check or change it** (in Docker: `docker compose run --rm finance-ops leafmesh auth status`):
  - `leafmesh auth status` — credential id, key fingerprint, registration and
    mode, never a key; `--check` signs in now and reports the tokens.
  - `leafmesh auth register` — register now.
  - `leafmesh auth rotate` — a new key pair: once it is active the mesh
    switches to it and revokes the old one. A revoked or expired key is never
    revived; rotate instead.
- **Lost the environment token?** From 5 Dec 2026 Studio shows the licence
  key and environment token only once, when they are made (then only a hint).
  Rotate the environment token in Studio and put the new one in `.env`.
  Rotating it changes where the agency's data is kept in the store, so do it
  before the agency holds data.
- **An existing project whose `.env` says `LEAFMESH_ENV_TOKEN=development`.**
  2.4.497 needs the real token. The token also names where the agency's data
  sits in the store, so with the real token the agency starts on an empty
  store (vault, sessions, board) — the old data stays under the old name. Set
  the real token before you put data in the agency, or contact us to move it.

## Your vault key — back it up

`create-leafmesh` put a `LEAFMESH_VAULT_KEY` in your `.env` when it made this project. It is the master key that encrypts every connection and credential you store in the vault (in Redis / Postgres).

- **Back it up like a master password.** Lose it and every stored connection has to be entered again.
- **Never change or regenerate it** for an existing project — a new key locks the existing vault. To change it, use `leafmesh vault rotate-key`.
- Every copy of the app (every replica) must get the **same** value. Docker Compose passes it in from `.env` as an environment variable.
- Never commit `.env` (it is already in `.gitignore`).
