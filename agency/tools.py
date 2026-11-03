"""Global tools for the finance-ops pod.

These are Ivy's day-0 tool surface — the tools she calls, in the turn, to PULL
an invoice and its context. Each reads the seeded source store so the pod runs
with no connector wired, and **each announces itself**: when there is no real
source and no seed, it returns `available: false` and says why — never a
synthetic success that looks like a real read. To go live, wire the ERP / mail
mcp block in config.yaml (each named action becomes its own tool); these dev
tools are the fallback contract.
"""
import os

from leafmesh import connection_rows, global_tool, LeafMeshLogger
from leafmesh.vault.refs import UnresolvedVaultRef

try:
    from ._shared import store
except ImportError:  # pragma: no cover — direct-run fallback
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _shared import store  # type: ignore

logger = LeafMeshLogger(__name__)


async def _vault_record_exists(name: str) -> bool:
    """True when the vault holds a record called `name` (Studio → Infrastructure).
    The record existing is the "real connector is wired" signal; its address
    and token stay in the vault and are never read here."""
    try:
        await connection_rows(name)
        return True
    except UnresolvedVaultRef:
        return False
    except PermissionError:  # SecretNotAllowed: the record exists; it only limits which agents read it
        return True


async def _connector_wired() -> bool:
    """True if a real invoice connector is set up (vault record 'erp_mcp' or 'mail_mcp')."""
    return await _vault_record_exists("erp_mcp") or await _vault_record_exists("mail_mcp")


@global_tool(category="intake")
async def read_invoice_sources(source_ref: str) -> dict:
    """Read one invoice from the source systems (email / portal / ERP webhook).

    Returns the invoice header + line-items, or `available: false` with a reason
    when there is genuinely nothing to read. When no real connector is wired,
    reads the seed source store.

    Args:
        source_ref: The source_event_id or invoice_id to read (e.g. "evt-acme-2207").
    """
    inv = store.find_invoice(str(source_ref).strip())
    if not inv:
        if await _connector_wired():
            return {"available": False,
                    "reason": "not in the seed store — read it with the erp_/mail_ MCP tools"}
        return {"available": False,
                "reason": (f"no seed invoice for {source_ref!r}; to read real invoices create "
                           "vault record 'erp_mcp' or 'mail_mcp' on Infrastructure → "
                           "Integrations → MCP and uncomment the mcp block")}
    logger.info(f"read_invoice_sources ref={source_ref} invoice={inv.get('invoice_id')}")
    return {"available": True, **inv}


@global_tool(category="intake")
def read_po_context(po_number: str) -> dict:
    """Read a purchase order + its goods receipt — the 3-way-match sources — so a
    line can be reconciled before it is coded.

    Args:
        po_number: The PO cited on the invoice (e.g. "PO-12345"). Empty ⇒ no PO.
    """
    po = store.get_po(str(po_number).strip())
    if not po:
        return {"available": False, "reason": f"no PO on file for {po_number!r}"}
    return {"available": True, **po}


@global_tool(category="intake")
def list_prior_invoices(vendor_id: str) -> dict:
    """List a vendor's prior POSTED invoices — the duplicate pre-flight's
    candidate set (used by Ray's dedupe floor).

    Args:
        vendor_id: The vendor whose posted history to list (e.g. "v-acme-001").
    """
    prior = store.posted_for_vendor(str(vendor_id).strip())
    return {"available": True, "vendor_id": vendor_id, "prior_posted": prior}


@global_tool(category="intake")
def claim_next_invoice() -> dict:
    """Atomically claim ONE unclaimed invoice from the month-end close backlog.

    Each parallel copy of Ivy (see `scaling.copies` in config.yaml) calls this to pull
    a DISJOINT invoice — no two copies get the same one. Returns
    `available: false` when the backlog is drained (a real, distinct outcome —
    not an empty invoice).
    """
    inv = store.claim_next_invoice()
    logger.info(f"claim_next_invoice available={inv.get('available')} "
                f"invoice={inv.get('invoice_id', '-')}")
    return inv
