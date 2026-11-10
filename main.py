"""finance-ops — Finance-Operations Pod (LeafMesh).

Boot order matters (SDK 2.4.131):
  1. import agency.tools  → the @global_tool decorators run BEFORE from_store/start,
     so Ivy's read_invoice_sources / read_po_context / list_prior_invoices /
     claim_next_invoice are registered.
  2. LeafMesh.from_store() → auto_discover wires agency/*_agent.py to the YAML.
  3. audit_logger.register(...) → attach the WORM sink to the custom external
     audit_logger_agent (must be after from_store, before start).
  4. leafmesh.start().
"""
import asyncio
import signal
import sys

from dotenv import load_dotenv
from leafmesh import LeafMesh, LeafMeshLogger

import agency.tools  # noqa: F401 — registers @global_tool tools before start()
from agency._shared import audit_logger as _audit_logger

load_dotenv()
logger = LeafMeshLogger(__name__)

leafmesh = LeafMesh.from_store()

# WORM audit sink — terminus for posts, approvals, and held gates.
_audit_logger.register(leafmesh, "audit_logger_agent")


async def main():
    await leafmesh.start()

    agents = leafmesh.registry.list_agents() if getattr(leafmesh, "registry", None) else []
    if agents:
        logger.info("Registered agents: " + ", ".join(f"{a.name} ({a.agent_type})" for a in agents))
    logger.info("finance-ops is running — API docs: http://127.0.0.1:18820/docs")

    # Trigger an invoice (day-0, against the seed source store — no connector needed):
    #   curl -X POST http://127.0.0.1:18820/api/mesh/request \
    #     -H "Content-Type: application/json" \
    #     -d '{"entry_point":"email_invoice","data":{"source_ref":"evt-acme-2207","message":"invoice from Acme"}}'

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

    logger.info("Shutting down...")
    await leafmesh.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
