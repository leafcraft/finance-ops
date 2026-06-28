"""loan_servicing_agent — manage already-originated loans.

KEPT AS PYTHON (class: domain math + actuator) — interest/principal
split arithmetic, payoff accrual, and delinquency bucketing against
the loan book; payment posting is a real side effect.

Day-2 operations for active credits: payment posting, delinquency
tracking, restructure offers, payoff calculations. Cron-runs the
delinquency sweep daily; direct-callable for individual loan ops.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from leafmesh import LeafMeshLogger

logger = LeafMeshLogger(__name__)

_DB = Path("./logs/loan_servicing.db")

# Delinquency buckets (days past due)
BUCKETS = {
    "current":     (0, 0),
    "early_dpd":   (1, 30),
    "delinquent":  (31, 60),
    "default":     (61, 90),
    "charge_off":  (91, 99999),
}


def _ensure_db():
    _DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS active_loans (
            loan_id TEXT PRIMARY KEY,
            borrower_id TEXT,
            principal_cents INTEGER,
            interest_rate_bps INTEGER,        -- basis points
            payment_amount_cents INTEGER,
            payment_frequency_days INTEGER,   -- 30 = monthly
            next_payment_due_utc TEXT,
            outstanding_balance_cents INTEGER,
            payments_made INTEGER,
            payments_remaining INTEGER,
            days_past_due INTEGER,
            bucket TEXT,
            status TEXT                       -- active | paid_off | charged_off | restructured
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS payment_postings (
            posting_id TEXT PRIMARY KEY,
            loan_id TEXT,
            posted_at_utc TEXT,
            amount_cents INTEGER,
            principal_cents INTEGER,
            interest_cents INTEGER,
            new_balance_cents INTEGER
        )
    """)
    return conn


def _shape(**overrides):
    """Full declared-yields shape per mode. (The SDK's lenient
    yields-fill would default missing keys; returning the full shape
    keeps direct callers and examples deterministic.)"""
    base = {
        "action": "", "loan_id": "", "posted": False, "posting_id": "",
        "amount_cents": 0, "interest_part_cents": 0,
        "principal_part_cents": 0, "new_balance_cents": 0,
        "loan_paid_off": False, "next_payment_due_utc": "",
        "outstanding_principal_cents": 0, "accrued_interest_cents": 0,
        "payoff_cents": 0, "valid_through_utc": "", "swept_at_utc": "",
        "bucket_movements": {}, "current_count": 0, "early_dpd_count": 0,
        "delinquent_count": 0, "default_count": 0, "chargeoff_count": 0,
        "requires_collections_team": False,
        "requires_writeoff_review": False,
        "reason": "",
    }
    base.update(overrides)
    return base


def _classify_dpd(dpd: int) -> str:
    for bucket, (lo, hi) in BUCKETS.items():
        if lo <= dpd <= hi:
            return bucket
    return "current"


async def loan_servicing_agent(llm_response, input_data, context):
    if isinstance(llm_response, str):
        try:
            llm_response = json.loads(llm_response)
        except (ValueError, TypeError):
            llm_response = {}
    if not isinstance(llm_response, dict):
        llm_response = {}

    mode = str(input_data.get("mode") or "delinquency_sweep").lower()
    if mode == "post_payment":
        return _post_payment(llm_response, input_data, context)
    if mode == "payoff_quote":
        return _payoff_quote(llm_response, input_data, context)
    return _delinquency_sweep(llm_response, input_data, context)


def _post_payment(llm_response, input_data, context) -> dict[str, Any]:
    upstream = input_data.get("upstream_yields") or {}
    loan_id = str(upstream.get("loan_id") or input_data.get("loan_id") or "")
    amount = int(upstream.get("payment_amount_cents")
                  or input_data.get("payment_amount_cents") or 0)
    if not loan_id or amount <= 0:
        return _shape(action="post_payment", reason="missing_inputs")

    now = datetime.now(timezone.utc)
    conn = _ensure_db()
    try:
        row = conn.execute("""
            SELECT outstanding_balance_cents, interest_rate_bps,
                   payment_frequency_days, payments_remaining
            FROM active_loans WHERE loan_id = ?
        """, (loan_id,)).fetchone()
        if not row:
            return _shape(action="post_payment", loan_id=loan_id,
                          reason="loan_not_found")
        balance, rate_bps, freq, remaining = row

        # Simple interest split — period interest = balance × (rate/365) × freq
        period_interest = int(balance * (rate_bps / 10000) * (freq / 365))
        interest_part = min(amount, period_interest)
        principal_part = amount - interest_part
        new_balance = max(0, balance - principal_part)

        posting_id = f"pmt-{loan_id}-{uuid.uuid4().hex[:10]}"
        conn.execute("""
            INSERT INTO payment_postings
                (posting_id, loan_id, posted_at_utc, amount_cents,
                 principal_cents, interest_cents, new_balance_cents)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (posting_id, loan_id, now.isoformat(), amount,
              principal_part, interest_part, new_balance))

        next_due = (now + timedelta(days=freq)).isoformat()
        new_status = "paid_off" if new_balance == 0 else "active"
        new_remaining = max(0, remaining - 1)
        conn.execute("""
            UPDATE active_loans
            SET outstanding_balance_cents = ?, payments_made = payments_made + 1,
                payments_remaining = ?, next_payment_due_utc = ?,
                days_past_due = 0, bucket = 'current', status = ?
            WHERE loan_id = ?
        """, (new_balance, new_remaining, next_due, new_status, loan_id))
        conn.commit()
    finally:
        conn.close()

    logger.info(
        f"[servicing] paid loan={loan_id} amt={amount/100:.2f} "
        f"interest={interest_part/100:.2f} principal={principal_part/100:.2f} "
        f"new_balance={new_balance/100:.2f}"
    )
    return _shape(
        action="post_payment", posted=True,
        loan_id=loan_id, posting_id=posting_id, amount_cents=amount,
        interest_part_cents=interest_part,
        principal_part_cents=principal_part,
        new_balance_cents=new_balance,
        loan_paid_off=new_balance == 0,
        next_payment_due_utc=next_due,
    )


def _payoff_quote(llm_response, input_data, context) -> dict[str, Any]:
    loan_id = str(input_data.get("loan_id") or "")
    conn = _ensure_db()
    try:
        row = conn.execute("""
            SELECT outstanding_balance_cents, interest_rate_bps,
                   next_payment_due_utc, payment_frequency_days
            FROM active_loans WHERE loan_id = ?
        """, (loan_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return _shape(action="payoff_quote", loan_id=loan_id,
                      reason="loan_not_found")

    balance, rate_bps, next_due, freq_days = row
    freq_days = int(freq_days or 30)
    now = datetime.now(timezone.utc)
    try:
        next_due_dt = datetime.fromisoformat(next_due.replace("Z", "+00:00"))
        if next_due_dt.tzinfo is None:
            next_due_dt = next_due_dt.replace(tzinfo=timezone.utc)
        # Accrue from the START of the current period (next_due − freq)
        days_since_last_due = max(
            0, (now - (next_due_dt - timedelta(days=freq_days))).days,
        )
    except Exception:
        days_since_last_due = 0
    accrued_interest = int(balance * (rate_bps / 10000) * (days_since_last_due / 365))
    payoff = balance + accrued_interest
    valid_through = (now + timedelta(days=7)).isoformat()

    return _shape(
        action="payoff_quote", loan_id=loan_id,
        outstanding_principal_cents=balance,
        accrued_interest_cents=accrued_interest,
        payoff_cents=payoff, valid_through_utc=valid_through,
    )


def _delinquency_sweep(llm_response, input_data, context) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    conn = _ensure_db()
    try:
        rows = list(conn.execute("""
            SELECT loan_id, next_payment_due_utc FROM active_loans
            WHERE status = 'active'
        """))
        bucket_movements: dict[str, list[str]] = {b: [] for b in BUCKETS}
        for loan_id, next_due in rows:
            try:
                due_dt = datetime.fromisoformat(next_due.replace("Z", "+00:00"))
            except Exception:
                continue
            dpd = max(0, (now - due_dt).days)
            new_bucket = _classify_dpd(dpd)
            conn.execute("""
                UPDATE active_loans
                SET days_past_due = ?, bucket = ?
                WHERE loan_id = ?
            """, (dpd, new_bucket, loan_id))
            bucket_movements[new_bucket].append(loan_id)

            # Charge-off transition
            if new_bucket == "charge_off":
                conn.execute(
                    "UPDATE active_loans SET status = 'charged_off' "
                    "WHERE loan_id = ?",
                    (loan_id,),
                )
        conn.commit()
    finally:
        conn.close()

    new_defaults = bucket_movements["default"]
    new_chargeoffs = bucket_movements["charge_off"]
    requires_collections = bool(bucket_movements["delinquent"]
                                  or new_defaults)
    requires_writeoff_review = bool(new_chargeoffs)

    logger.info(
        f"[servicing] sweep delinquent={len(bucket_movements['delinquent'])} "
        f"default={len(new_defaults)} chargeoff={len(new_chargeoffs)}"
    )

    return _shape(
        action="delinquency_sweep",
        swept_at_utc=now.isoformat(),
        bucket_movements=bucket_movements,
        current_count=len(bucket_movements["current"]),
        early_dpd_count=len(bucket_movements["early_dpd"]),
        delinquent_count=len(bucket_movements["delinquent"]),
        default_count=len(new_defaults),
        chargeoff_count=len(new_chargeoffs),
        requires_collections_team=requires_collections,
        requires_writeoff_review=requires_writeoff_review,
    )
