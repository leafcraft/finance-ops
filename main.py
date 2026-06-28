"""finance-ops — Finance Operations (AP / AR / close)

A LeafMesh deployment that runs finance back-office as a peer-to-peer
TEAM of agents — see SKILL.md Rule 1. The Manager hovers above and
intervenes only on narration hints, escalation, and contract
violations; the agents talk to each other directly via `can_call`
with `condition:` on every wire.

Mesh shape (see configs/config.yaml for the full team roster):
  any of [email, portal, ERP webhook, mobile, manual, monthly-close]
    → invoice_intake (LLM — normalize + extract, yields-driven enforcement)
        ├── matching            (programmatic — PO match, then 3-way
        │                        match when required)
        └── payment_risk        (LLM — if anomaly risk; duplicate +
                                 fraud verdict)
        → gl_coding (LLM, knowledge-grounded, fan-in; the approval
          matrix rides its outbound wires as explicit YAML conditions)
            ├── ap_clerk_human   (HITL — under $5K, no exceptions)
            ├── controller_human (HITL — under $50K OR exceptions)
            └── cfo_human        (HITL — >= $50K, dual-sign >= $500K)
        → erp_poster (programmatic — posts to NetSuite/SAP/Quickbooks/…)
        → audit_logger (hash-chained WORM sink)

Peer collaboration:
  - matching ↔ invoice_intake (re-extract on PO miss)
  - payment_risk → controller_human directly on score >= 0.7
  - vendor_comms is callable as a side-arm from any specialist
    (exception_query mode) and answers vendor inquiries
    (inquiry_response mode)

Trust mechanisms:
  - the approval matrix is explicit conditions on gl_coding's outbound
    wires — deterministic, auditable by reading configs/config.yaml
  - audit sink is external + hash-chained — Redis crash does not lose audit
  - HITL fallback_response is escalation, never auto-approval
  - enforce_yields with retry on every LLM agent — bad shape triggers
    Summarizer-driven correction, not silent fallback
  - knowledge-grounded LLMs (AP runbook, vendor master, fraud playbook,
    chart of accounts) — policy updates propagate without retraining
  - Self-reliant agents: matching / payment_risk / gl_coding pull their
    own context via @pre_compose (PO + goods receipt / vendor posted
    history / chart of accounts) from the seeded source store
    (agency/_shared/store.py — the ERP in prod)
  - Deterministic floors the LLM can't waive, in @chain: 3-way tolerance
    arithmetic (matching), duplicate hashing (payment_risk), GL-validity
    (gl_coding), and the approval matrix on gl_coding's wires
  - YAML-first: Python remains only where it must (ERP actuator with
    fail-closed post-HITL recovery, matching's PO lookup + tolerance
    floor + loop guard, payment_risk's dedupe floor, gl_coding's chart
    pull + validity floor + stash, loan domain math); the
    system-of-record agents (payment_scheduler, close_cycle) are
    connector-only (integration: mcp → your treasury / ERP)

For full architecture: see PRD.md.
For setup + the three configuration tiers: see README.md.
"""
import asyncio
import signal
import sys

from dotenv import load_dotenv
from leafmesh import EventType, LeafMesh, LeafMeshLogger

from agency._shared import audit_logger as _audit_logger

load_dotenv()
logger = LeafMeshLogger(__name__)

leafmesh = LeafMesh.from_yaml("configs/config.yaml")

# Register the WORM audit sink as the intelligence function for the
# custom external audit_logger_agent (SDK 2.4+ API). Must happen after
# from_yaml() and before start().
_audit_logger.register(leafmesh, "audit_logger_agent")


# ── Event-bus subscribers ─────────────────────────────────────────────
# Compliance + SOX audit surface. Each transition fires a typed event;
# subscribers run in their own consumer group and never block the
# invoice path.

async def _on_agent_error(event):
    logger.warning(
        f"[fin-audit] AGENT_ERROR session={event.session_id} "
        f"data={(event.data or {})!r}"
    )

async def _on_manager_intervention(event):
    logger.warning(
        f"[fin-ops] MANAGER_INTERVENTION session={event.session_id} "
        f"data={(event.data or {})!r}"
    )

async def _on_intervention_needed(event):
    logger.error(
        f"[fin-ops] INTERVENTION_NEEDED session={event.session_id} "
        f"data={(event.data or {})!r}"
    )

async def _on_human_input_timeout(event):
    logger.warning(
        f"[fin-audit] HITL TIMEOUT session={event.session_id} "
        f"data={(event.data or {})!r}"
    )


async def main():
    await leafmesh.start()

    # Wire event-bus subscribers — compliance / SOX audit surface.
    leafmesh.event_bus.subscribe(EventType.AGENT_ERROR,           _on_agent_error)
    leafmesh.event_bus.subscribe(EventType.MANAGER_INTERVENTION,  _on_manager_intervention)
    leafmesh.event_bus.subscribe(EventType.INTERVENTION_NEEDED,   _on_intervention_needed)
    leafmesh.event_bus.subscribe(EventType.HUMAN_INPUT_TIMEOUT,   _on_human_input_timeout)

    # Self-healing — graduated recovery on agent fault.
    try:
        await leafmesh.enable_self_healing()
        logger.info("Self-healing enabled (6 graduated recovery actions)")
    except AttributeError:
        pass

    agents = (
        leafmesh.registry.list_agents()
        if hasattr(leafmesh, "registry") and leafmesh.registry
        else []
    )
    if agents:
        summary = ", ".join(f"{a.name} ({a.agent_type})" for a in agents)
        logger.info(f"Registered agents: {summary}")

    logger.info("finance-ops — Finance Ops mesh running")
    logger.info("API docs (synthetic testing): http://127.0.0.1:18820/docs")
    logger.info("Production setup + config tiers: see README.md")

    # ── Triggering a synthetic invoice (uncomment to demo) ─────────
    # result = await leafmesh.mesh_call(
    #     "manual_entry",
    #     input_data={
    #         "source": "manual",
    #         "vendor_id": "v-acme-001",
    #         "vendor_name": "Acme Supplies",
    #         "amount_cents": 250000,        # $2,500
    #         "currency": "USD",
    #         "po_number": "PO-12345",
    #         "due_date": "2026-07-15",
    #     },
    # )
    # logger.info(f"Result: {result}")

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    if sys.platform != "win32":
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop_event.set)
        await stop_event.wait()
    else:
        try:
            await stop_event.wait()
        except KeyboardInterrupt:
            pass

    logger.info("Shutting down (draining in-flight invoices)...")
    await leafmesh.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
