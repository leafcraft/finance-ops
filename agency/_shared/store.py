"""Dev-mode source store for finance-ops — the system-of-record stand-in.

Stands in for the customer's ERP / vendor-master / chart-of-accounts so
the WHOLE invoice chain (intake → match → risk → code → approve → post →
audit) runs end-to-end BEFORE any connector is wired. Every read here is
the contract a production connector must satisfy; swap the body for the
real call and the agents are unchanged:

  * Vendor master      → NetSuite / SAP / Coupa vendor record
  * Purchase orders    → ERP PO + goods-receipt (the 3-way match sources)
  * Chart of accounts  → ERP GL master (the GL-validity floor reads it)
  * Posted ledger      → ERP AP ledger (the duplicate pre-flight reads it)

Setting ERP env vars alone does nothing until you wire the calls here.

This is a SEEDED dev store, never the system of record (Rule 7): it
ships realistic invoices + matching POs + a vendor master + a GL chart
so the demo works out of the box. The posted ledger is append-on-post
(erp_poster writes back here) so the duplicate pre-flight sees real
history accumulate as invoices flow.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()
# Dev store location. Override FINANCE_OPS_STORE_DIR to relocate (tests
# point it at a temp dir). Irrelevant in prod — reads hit the ERP.
_DIR = Path(os.environ.get("FINANCE_OPS_STORE_DIR", "./data"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _path(k: str) -> Path:
    _DIR.mkdir(parents=True, exist_ok=True)
    return _DIR / f"{k}.json"


def _load(k: str) -> dict[str, Any]:
    p = _path(k)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except (ValueError, OSError):
        return {}


def _save(k: str, d: dict[str, Any]) -> None:
    p = _path(k)
    t = p.with_suffix(".json.tmp")
    t.write_text(json.dumps(d, indent=2, default=str))
    t.replace(p)


# ════════════════════════════════════════════════════════════════════
# SEED DATA — realistic vendors / POs / GL chart / invoices
# ════════════════════════════════════════════════════════════════════

# Vendor master — vendor_id → record. `default_gl_account` lets gl_coding
# prefer the vendor's standing mapping; `requires_po` / `requires_3way`
# are vendor-level policy flags intake honours.
_SEED_VENDORS: dict[str, Any] = {
    "v-acme-001": {
        "vendor_id": "v-acme-001", "vendor_name": "Acme Supplies",
        "country_code": "US", "status": "active", "first_seen": "2023-02-11",
        "default_gl_account": "6500-Software-and-SaaS",
        "default_cost_center": "CC-ENG", "payment_terms": "Net30",
        "requires_po": True, "requires_3way": False,
        "historical_median_cents": 250000, "paid_invoice_count": 41,
    },
    "v-globex-002": {
        "vendor_id": "v-globex-002", "vendor_name": "Globex Logistics",
        "country_code": "US", "status": "active", "first_seen": "2022-08-03",
        "default_gl_account": "5400-Freight-and-Logistics",
        "default_cost_center": "CC-OPS", "payment_terms": "Net45",
        "requires_po": True, "requires_3way": True,
        "historical_median_cents": 1200000, "paid_invoice_count": 88,
    },
    "v-initech-003": {
        "vendor_id": "v-initech-003", "vendor_name": "Initech Consulting",
        "country_code": "US", "status": "active", "first_seen": "2024-01-19",
        "default_gl_account": "6610-Professional-Services",
        "default_cost_center": "CC-FIN", "payment_terms": "Net30",
        "requires_po": False, "requires_3way": False,
        "historical_median_cents": 480000, "paid_invoice_count": 12,
    },
    # ── Vendors referenced by the examples/ scenarios ──
    "v-acme-cloud-001": {
        "vendor_id": "v-acme-cloud-001", "vendor_name": "Acme Cloud Services Inc.",
        "country_code": "US", "status": "active", "first_seen": "2023-05-20",
        "default_gl_account": "6500-Software-and-SaaS",
        "default_cost_center": "CC-ENG", "payment_terms": "Net30",
        "requires_po": True, "requires_3way": False,
        "historical_median_cents": 420000, "paid_invoice_count": 22,
    },
    "v-quintessential-office-007": {
        "vendor_id": "v-quintessential-office-007",
        "vendor_name": "Quintessential Office Supplies Co.",
        "country_code": "US", "status": "active", "first_seen": "2022-11-02",
        "default_gl_account": "1500-Equipment",
        "default_cost_center": "CC-OPS", "payment_terms": "Net30",
        "requires_po": True, "requires_3way": False,
        "historical_median_cents": 120000, "paid_invoice_count": 31,
    },
    "v-snowmelt-data-platform-001": {
        "vendor_id": "v-snowmelt-data-platform-001",
        "vendor_name": "SnowMelt Data Platform Inc.",
        "country_code": "US", "status": "active", "first_seen": "2024-09-14",
        "default_gl_account": "6500-Software-and-SaaS",
        "default_cost_center": "CC-DATA", "payment_terms": "Net30",
        "requires_po": True, "requires_3way": False,
        "historical_median_cents": 25000000, "paid_invoice_count": 3,
    },
}

# Purchase orders — po_number → record. `goods_receipt` is the third leg
# of the 3-way match (None ⇒ no receipt booked yet). Quantities/prices
# here are the AUTHORITY the invoice is reconciled against.
_SEED_POS: dict[str, Any] = {
    "PO-12345": {
        "po_number": "PO-12345", "vendor_id": "v-acme-001", "status": "open",
        "currency": "USD",
        "lines": [
            {"sku": "SAAS-SEAT", "description": "Annual SaaS subscription seat",
             "quantity": 10, "unit_price_cents": 25000},
        ],
        "goods_receipt": {
            "receipt_id": "GR-12345", "received_at": "2026-06-10",
            "lines": [{"sku": "SAAS-SEAT", "quantity": 10}],
        },
    },
    "PO-55500": {
        "po_number": "PO-55500", "vendor_id": "v-globex-002", "status": "open",
        "currency": "USD",
        "lines": [
            {"sku": "FREIGHT-PALLET", "description": "Pallet freight, zone 4",
             "quantity": 100, "unit_price_cents": 12000},
        ],
        # Receipt SHORT by 8 units → a real 3-way quantity variance.
        "goods_receipt": {
            "receipt_id": "GR-55500", "received_at": "2026-06-12",
            "lines": [{"sku": "FREIGHT-PALLET", "quantity": 92}],
        },
    },
    "PO-77700": {
        "po_number": "PO-77700", "vendor_id": "v-initech-003", "status": "open",
        "currency": "USD",
        "lines": [
            {"sku": "ADVISORY-DAY", "description": "Advisory engagement, daily",
             "quantity": 8, "unit_price_cents": 60000},
        ],
        "goods_receipt": None,   # services — no goods receipt
    },
    # ── POs referenced by the examples/ scenarios ──
    # Happy path (example 01) — matches the invoiced line cleanly.
    "PO-2026-0142": {
        "po_number": "PO-2026-0142", "vendor_id": "v-acme-cloud-001",
        "status": "open", "currency": "USD",
        "lines": [
            {"sku": "ENTERPRISE-TIER",
             "description": "Enterprise tier — June 2026 subscription",
             "quantity": 1, "unit_price_cents": 420000},
        ],
        "goods_receipt": {"receipt_id": "GR-0142", "received_at": "2026-06-01",
                          "lines": [{"sku": "ENTERPRISE-TIER", "quantity": 1}]},
    },
    # PO mismatch (example 03) — PO authorised 10 desks; invoice bills 12.
    "PO-2026-0099": {
        "po_number": "PO-2026-0099", "vendor_id": "v-quintessential-office-007",
        "status": "open", "currency": "USD",
        "lines": [
            {"sku": "EDX-450", "description": "Ergonomic desk — model EDX-450",
             "quantity": 10, "unit_price_cents": 12000},
        ],
        "goods_receipt": {"receipt_id": "GR-0099", "received_at": "2026-05-28",
                          "lines": [{"sku": "EDX-450", "quantity": 10}]},
    },
    # High value (example 04) — multi-line enterprise licence.
    "PO-2026-0007": {
        "po_number": "PO-2026-0007", "vendor_id": "v-snowmelt-data-platform-001",
        "status": "open", "currency": "USD",
        "lines": [
            {"sku": "DW-LICENCE",
             "description": "Enterprise data warehouse — annual licence, 4 environments",
             "quantity": 1, "unit_price_cents": 23000000},
            {"sku": "PREMIUM-SUPPORT",
             "description": "Premium support — 24×7 SLA, named TAM",
             "quantity": 1, "unit_price_cents": 2000000},
        ],
        "goods_receipt": {"receipt_id": "GR-0007", "received_at": "2026-06-02",
                          "lines": [{"sku": "DW-LICENCE", "quantity": 1},
                                    {"sku": "PREMIUM-SUPPORT", "quantity": 1}]},
    },
}

# Chart of accounts — the GL-validity floor rejects any code NOT in here.
_SEED_GL_CHART: dict[str, Any] = {
    "6500-Software-and-SaaS": {"active": True, "kind": "expense"},
    "6610-Professional-Services": {"active": True, "kind": "expense"},
    "5400-Freight-and-Logistics": {"active": True, "kind": "cogs"},
    "6200-Rent-and-Facilities": {"active": True, "kind": "expense"},
    "6700-Marketing": {"active": True, "kind": "expense"},
    "6800-Travel": {"active": True, "kind": "expense"},
    "1500-Equipment": {"active": True, "kind": "asset"},
    "6999-Uncategorized": {"active": True, "kind": "suspense"},
    # An intentionally INACTIVE account — coding to it must be rejected.
    "6400-Legacy-Telecom": {"active": False, "kind": "expense"},
}

# Sample invoices keyed by source_event_id — the demo / examples ingest
# these. Amounts chosen to exercise each approval tier + the dup/3-way
# floors. (One is a deliberate re-submission of inv-acme-2207.)
_SEED_INVOICES: dict[str, Any] = {
    "evt-acme-2207": {
        "source_event_id": "evt-acme-2207", "invoice_id": "inv-acme-2207",
        "source": "email", "vendor_id": "v-acme-001",
        "vendor_name": "Acme Supplies", "amount_cents": 250000,
        "currency": "USD", "po_number": "PO-12345", "due_date": "2026-07-15",
        "line_items": [{"description": "Annual SaaS subscription seat",
                        "quantity": 10, "unit_price": 250, "total": 2500}],
    },
    "evt-globex-9001": {
        "source_event_id": "evt-globex-9001", "invoice_id": "inv-globex-9001",
        "source": "portal", "vendor_id": "v-globex-002",
        "vendor_name": "Globex Logistics", "amount_cents": 1200000,
        "currency": "USD", "po_number": "PO-55500", "due_date": "2026-07-20",
        "line_items": [{"description": "Pallet freight, zone 4",
                        "quantity": 100, "unit_price": 120, "total": 12000}],
    },
    "evt-initech-4410": {
        "source_event_id": "evt-initech-4410", "invoice_id": "inv-initech-4410",
        "source": "manual", "vendor_id": "v-initech-003",
        "vendor_name": "Initech Consulting", "amount_cents": 8000000,
        "currency": "USD", "po_number": "PO-77700", "due_date": "2026-08-01",
        "line_items": [{"description": "Advisory engagement",
                        "quantity": 8, "unit_price": 6000, "total": 48000}],
    },
}

# Prior-posted invoices — the duplicate pre-flight reads this ledger.
# Seeded with one already-paid Acme invoice so a re-submission of the
# same invoice_id (strict) OR same amount+vendor+due_date (loose) is
# caught deterministically on day 0.
_SEED_POSTED: list[dict[str, Any]] = [
    {"invoice_id": "inv-acme-2207", "vendor_id": "v-acme-001",
     "amount_cents": 250000, "due_date": "2026-07-15",
     "erp_entry_id": "netsuite-seed00000001", "posted_at_utc": "2026-06-15T09:00:00+00:00"},
]


# ── Vendor master ────────────────────────────────────────────────────
def _vendors() -> dict[str, Any]:
    s = _load("vendors")
    if not s:
        s = dict(_SEED_VENDORS)
        _save("vendors", s)
    return s


def get_vendor(vendor_id: str) -> dict[str, Any]:
    """Vendor-master record. PROD: ERP vendor read."""
    return _vendors().get(vendor_id, {})


# ── Purchase orders + goods receipts (the 3-way match sources) ───────
def _pos() -> dict[str, Any]:
    s = _load("purchase_orders")
    if not s:
        s = dict(_SEED_POS)
        _save("purchase_orders", s)
    return s


def get_po(po_number: str) -> dict[str, Any]:
    """PO with its lines + goods_receipt. PROD: ERP PO read. {} if unknown."""
    if not po_number:
        return {}
    return _pos().get(po_number.upper(), {})


# ── Chart of accounts (the GL-validity floor source) ─────────────────
def _gl_chart() -> dict[str, Any]:
    s = _load("gl_chart")
    if not s:
        s = dict(_SEED_GL_CHART)
        _save("gl_chart", s)
    return s


def gl_account_exists(gl_account: str) -> bool:
    """True only for an ACTIVE account in the chart. PROD: ERP GL master."""
    rec = _gl_chart().get(gl_account)
    return bool(rec) and bool(rec.get("active"))


def chart_accounts() -> list[str]:
    """Active GL account ids — handed to gl_coding via @pre_compose so the
    LLM codes against the real chart even before the knowledge group is
    seeded."""
    return [a for a, r in _gl_chart().items() if r.get("active")]


# ── Sample invoices (the demo ingest) ────────────────────────────────
def _invoices() -> dict[str, Any]:
    s = _load("invoices")
    if not s:
        s = dict(_SEED_INVOICES)
        _save("invoices", s)
    return s


def get_invoice(source_event_id: str) -> dict[str, Any]:
    return _invoices().get(source_event_id, {})


def sample_invoices() -> list[dict[str, Any]]:
    return list(_invoices().values())


def find_invoice(ref: str) -> dict[str, Any]:
    """Look up a seed invoice by source_event_id OR invoice_id. {} if unknown.

    This is the day-0 stand-in for the email / portal / ERP read. In production
    the body becomes the real connector call; the signature is the contract.
    """
    ref = str(ref or "").strip()
    if not ref:
        return {}
    inv = _invoices().get(ref)
    if inv:
        return inv
    for row in _invoices().values():
        if str(row.get("invoice_id") or "") == ref:
            return row
    return {}


# ── Backlog claim (the atomic claim behind Ivy's `instances` fan-out) ────────
# Dev is single-process, so a threading lock guarding an in-memory claimed set
# IS the atomic claim (each parallel instance pops a DISJOINT invoice). PROD:
# swap the body for `LPOP count` on a Redis list, or `UPDATE ... FOR UPDATE SKIP
# LOCKED RETURNING` on the ERP's unposted-invoice table — the contract (one
# caller gets each item, exactly once) is unchanged.
_CLAIMED: set[str] = set()


def _open_backlog_ids() -> list[str]:
    """source_event_ids of seed invoices not yet posted (the close-sweep backlog)."""
    posted_ids = {r.get("invoice_id") for r in _posted().get("rows", [])}
    return [eid for eid, inv in sorted(_invoices().items())
            if inv.get("invoice_id") not in posted_ids]


def claim_next_invoice() -> dict[str, Any]:
    """Atomically claim ONE unclaimed backlog invoice. available:false when the
    backlog is drained — never a synthetic 'here is an invoice' when there is
    nothing to do (a claim on empty is a real, distinct outcome)."""
    with _LOCK:
        for eid in _open_backlog_ids():
            if eid not in _CLAIMED:
                _CLAIMED.add(eid)
                inv = dict(_invoices().get(eid) or {})
                inv["available"] = True
                return inv
    return {"available": False, "reason": "close-sweep backlog is empty (nothing left to claim)"}


def reset_claims() -> None:
    """Test/dev helper — clears the in-memory claim set."""
    with _LOCK:
        _CLAIMED.clear()


# ── Posted ledger (the duplicate pre-flight source) ──────────────────
def _posted() -> dict[str, Any]:
    s = _load("posted_ledger")
    if not s:
        s = {"rows": list(_SEED_POSTED)}
        _save("posted_ledger", s)
    return s


def posted_for_vendor(vendor_id: str) -> list[dict[str, Any]]:
    """Prior posted invoices for a vendor — the dup pre-flight's candidate
    set. PROD: ERP AP-ledger query by vendor."""
    if not vendor_id:
        return []
    return [r for r in _posted().get("rows", []) if r.get("vendor_id") == vendor_id]


def record_posted(row: dict[str, Any]) -> None:
    """Append a posted invoice so the dup pre-flight sees real history
    accumulate. Called by erp_poster after a successful post. PROD:
    no-op (the ERP already holds it)."""
    if not row.get("invoice_id"):
        return
    with _LOCK:
        s = _posted()
        rows = s.setdefault("rows", [])
        rows.append({
            "invoice_id": str(row.get("invoice_id") or ""),
            "vendor_id": str(row.get("vendor_id") or ""),
            "amount_cents": int(row.get("amount_cents") or 0),
            "due_date": str(row.get("due_date") or ""),
            "erp_entry_id": str(row.get("erp_entry_id") or ""),
            "posted_at_utc": str(row.get("posted_at_utc") or _now()),
        })
        _save("posted_ledger", s)
