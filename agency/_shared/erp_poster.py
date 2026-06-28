"""ERP posting connector for finance-ops — real SQLite-backed ledger.

In dev mode the "ERP" is a local SQLite ledger at ./data/erp_postings.db.
Every approved invoice is written as a real row with full audit-able
context (vendor, amount, GL coding, payment date). Production swaps the
ERPDispatcher.post() implementation for the real ERP's REST/RPC call —
NetSuite RESTlet, SAP OData, QuickBooks API, Xero, Dynamics 365, …

The local ledger is real persistence, NOT a stub. The customer can:
  · sqlite3 ./data/erp_postings.db 'SELECT * FROM erp_postings'
  · build reports against it
  · verify reconciliation runs against it
  · diff it against their real ERP after migration to spot drift

The only thing that changes when moving to production is the body of
ERPDispatcher.post().
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from leafmesh import LeafMeshLogger

logger = LeafMeshLogger(__name__)


LEDGER_PATH = Path(os.environ.get("FINANCE_OPS_ERP_LEDGER",
                                   "./data/erp_postings.db"))
LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
_LOCK = threading.Lock()


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(str(LEDGER_PATH), timeout=10)
    c.row_factory = sqlite3.Row
    return c


def _ensure_schema() -> None:
    with _LOCK, _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS erp_postings (
                erp_entry_id   TEXT PRIMARY KEY,
                erp_type       TEXT NOT NULL,
                invoice_id     TEXT,
                vendor_id      TEXT,
                gl_account     TEXT,
                cost_center    TEXT,
                tax_code       TEXT,
                amount_cents   INTEGER,
                pay_date       TEXT,
                posted_at_utc  TEXT NOT NULL,
                payload_raw    TEXT
            )
        """)
        c.commit()


_ensure_schema()


class ERPDispatcher:
    """SQLite-backed ERP poster. Swap `post` for your real ERP API."""

    def __init__(self, erp_type: str, timeout: float = 30.0):
        self.erp_type = erp_type
        self.timeout = timeout

    def post(self, payload: dict[str, Any]) -> dict[str, Any]:
        entry_id = f"{self.erp_type}-{uuid.uuid4().hex[:12]}"
        now = datetime.now(timezone.utc).isoformat()

        with _LOCK, _conn() as c:
            c.execute("""
                INSERT INTO erp_postings (
                    erp_entry_id, erp_type, invoice_id, vendor_id,
                    gl_account, cost_center, tax_code,
                    amount_cents, pay_date, posted_at_utc, payload_raw
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                entry_id, self.erp_type,
                str(payload.get("invoice_id") or ""),
                str(payload.get("vendor_id") or ""),
                str(payload.get("gl_account") or ""),
                str(payload.get("cost_center") or ""),
                str(payload.get("tax_code") or ""),
                int(payload.get("amount_cents") or 0),
                str(payload.get("recommended_pay_date") or ""),
                now,
                json.dumps(payload, default=str),
            ))
            c.commit()

        logger.info(
            f"[erp:{self.erp_type}] posted invoice={payload.get('invoice_id', '?')} "
            f"vendor={payload.get('vendor_id', '?')} "
            f"amount=${(payload.get('amount_cents') or 0)/100:,.2f} "
            f"→ entry={entry_id}"
        )
        return {
            "erp_entry_id": entry_id,
            "posted_at_utc": now,
            "posted": True,
            "erp_type": self.erp_type,
        }


def dispatch_erp(config: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """Helper called from the agent. config wins over env wins over default."""
    erp_type = (config or {}).get("erp_type") or os.getenv("ERP_TYPE", "netsuite")
    timeout = float((config or {}).get("timeout", 30.0))
    return ERPDispatcher(erp_type=erp_type, timeout=timeout).post(payload)
