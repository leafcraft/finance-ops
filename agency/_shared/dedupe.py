"""Deterministic duplicate-detection hashing for AP invoices.

The duplicate floor the LLM cannot waive. Two keys, two strengths:

  * strict — same vendor + same invoice_id. A re-submitted invoice
    number against the same vendor is a duplicate, full stop.
  * loose  — same vendor + same amount_cents + same due_date. Catches a
    re-submission under a fresh invoice number (a common AP fraud /
    error pattern).

Both are SHA-256 over a normalised tuple so the comparison is stable
across runs and storage backends. Production compares these keys
against the ERP AP ledger; dev compares against the seeded posted
ledger in store.py.
"""
from __future__ import annotations

import hashlib
from typing import Any


def _norm(s: Any) -> str:
    return str(s or "").strip().lower()


def strict_key(vendor_id: Any, invoice_id: Any) -> str:
    body = f"strict|{_norm(vendor_id)}|{_norm(invoice_id)}"
    return hashlib.sha256(body.encode()).hexdigest()


def loose_key(vendor_id: Any, amount_cents: Any, due_date: Any) -> str:
    try:
        amt = int(amount_cents or 0)
    except (TypeError, ValueError):
        amt = 0
    body = f"loose|{_norm(vendor_id)}|{amt}|{_norm(due_date)}"
    return hashlib.sha256(body.encode()).hexdigest()


def find_duplicate(invoice: dict[str, Any], prior: list[dict[str, Any]],
                   detection_mode: str = "strict") -> dict[str, Any]:
    """Compare `invoice` against `prior` posted rows. Returns the match
    descriptor (is_duplicate False when none).

    strict mode checks the strict key only; loose mode checks strict
    first, then the loose key. A strict hit always wins (it is the
    stronger signal).
    """
    mode = (detection_mode or "strict").strip().lower()
    if mode not in ("strict", "loose"):
        mode = "strict"

    inv_strict = strict_key(invoice.get("vendor_id"), invoice.get("invoice_id"))
    inv_loose = loose_key(invoice.get("vendor_id"), invoice.get("amount_cents"),
                          invoice.get("due_date"))

    miss = {"is_duplicate": False, "duplicate_match_kind": "",
            "duplicate_of_invoice_id": "", "prior_post_record_id": "",
            "prior_post_at_utc": "", "detection_mode": mode}

    for row in prior or []:
        if strict_key(row.get("vendor_id"), row.get("invoice_id")) == inv_strict:
            # Don't flag an invoice as a duplicate of *itself* unless the
            # caller is re-ingesting a posted id — that IS the duplicate
            # case here (a re-submission), so we keep it.
            return {
                "is_duplicate": True,
                "duplicate_match_kind": "strict_invoice_id_match",
                "duplicate_of_invoice_id": str(row.get("invoice_id") or ""),
                "prior_post_record_id": str(row.get("erp_entry_id") or ""),
                "prior_post_at_utc": str(row.get("posted_at_utc") or ""),
                "detection_mode": mode,
            }

    if mode == "loose":
        for row in prior or []:
            if loose_key(row.get("vendor_id"), row.get("amount_cents"),
                         row.get("due_date")) == inv_loose:
                return {
                    "is_duplicate": True,
                    "duplicate_match_kind": "loose_amount_due_date_match",
                    "duplicate_of_invoice_id": str(row.get("invoice_id") or ""),
                    "prior_post_record_id": str(row.get("erp_entry_id") or ""),
                    "prior_post_at_utc": str(row.get("posted_at_utc") or ""),
                    "detection_mode": mode,
                }

    return miss
