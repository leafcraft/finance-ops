"""regulatory_monitor_agent — feed scan with a deterministic filing floor.

The LLM (configs/config.yaml) pulls the feeds, classifies each item into
process areas, dedupes against its cross-session memory, and writes the
alert objects. This micro-module owns the one thing that must NOT be
trusted to the model: the FILING / ESCALATION TRIGGER.

@chain  enforce_urgency_floor — the deterministic threshold the prompt
        states ("requires_compliance_review = true when ANY new alert is
        immediate or high"). The escalation to controller_human rides on
        requires_compliance_review, so the LLM cannot quietly summarise a
        high-urgency regulatory change without tripping the review gate.
        The floor recomputes, from the actual new_alerts list:
          · alerts_by_urgency — the per-level counts (so the dashboard
            can't disagree with the alert list);
          · new_alerts_count — len(new_alerts) (the count can't lie);
          · requires_compliance_review — forced True if ANY new alert is
            "immediate" or "high". The LLM may RAISE it, never LOWER it.

No @pre_compose: the dedup source of truth is the SDK's cross-session
memory (configured in YAML), which this module cannot read deterministically
here — pulling it in Python would duplicate, not ground, that state. The
genuine deterministic need is the filing trigger, so that is the only
floor added (no decorator theater).
"""
from __future__ import annotations

import json
from typing import Any

from leafmesh import LeafMeshLogger, chain

logger = LeafMeshLogger(__name__)

_ESCALATING_URGENCIES = frozenset({"immediate", "high"})
_KNOWN_URGENCIES = ("immediate", "high", "medium", "low")


def _parse(resp: Any) -> dict[str, Any]:
    if isinstance(resp, dict):
        return resp
    if isinstance(resp, str):
        try:
            return json.loads(resp)
        except (ValueError, TypeError):
            return {}
    return {}


# ── @chain — the deterministic FILING / ESCALATION trigger floor ──────
async def enforce_urgency_floor(result: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """Recompute the count + the urgency histogram + the review trigger
    from the actual alert list. The LLM cannot under-report a high-urgency
    regulatory change to dodge the controller escalation."""
    if not isinstance(result, dict):
        result = {}

    alerts = result.get("new_alerts")
    if not isinstance(alerts, list):
        alerts = []
        result["new_alerts"] = alerts

    # Count + histogram derived from the list, never trusted from the LLM.
    histogram: dict[str, int] = {u: 0 for u in _KNOWN_URGENCIES}
    escalate = False
    for a in alerts:
        urg = str((a or {}).get("urgency") or "").strip().lower() if isinstance(a, dict) else ""
        if urg in histogram:
            histogram[urg] += 1
        else:
            histogram[urg if urg else "low"] = histogram.get(urg or "low", 0) + 1
        if urg in _ESCALATING_URGENCIES:
            escalate = True

    result["new_alerts_count"] = len(alerts)
    result["alerts_by_urgency"] = histogram

    # Floor: ANY immediate/high alert forces the compliance-review gate.
    # The LLM may RAISE it (e.g. a medium it judges material); never lower.
    result["requires_compliance_review"] = bool(escalate or result.get("requires_compliance_review"))

    if escalate:
        logger.warning(
            f"[regulatory] FILING floor: {histogram.get('immediate', 0)} immediate / "
            f"{histogram.get('high', 0)} high alert(s) → compliance review forced"
        )
    return result


@chain(enforce_urgency_floor)
async def regulatory_monitor_agent(
    llm_response: dict[str, Any] | str | None,
    input_data: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    """Surface the LLM's deduped alert scan; the chain enforces the
    filing/escalation trigger floor."""
    result = _parse(llm_response)
    if not isinstance(result, dict):
        result = {}
    return dict(result)
