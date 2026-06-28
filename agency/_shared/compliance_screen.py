"""Deterministic compliance screen for new counterparties / vendors.

The floor the compliance LLM cannot waive. Three deterministic checks a
regulator reads, none of them trusted to a model:

  * sanctions   — any name on a sanctions list (OFAC SDN / EU CONSOL /
                  UK HMT / UN / DFAT / OSFI) forces rejection + SAR.
  * geography   — a high-risk jurisdiction (the FATF-style call list)
                  forces enhanced due diligence (compliance review).
  * tax form    — onboarding requires a tax form on file (W-9 / W-8…);
                  a missing or absent-id required field fails closed.

Production swaps the seeded lists for the customer's screening provider
(Refinitiv World-Check, Dow Jones, the OFAC SDN API) and their tax-form
system; the floor logic — what forces review and what can never be
auto-cleared — is unchanged.
"""
from __future__ import annotations

from typing import Any

# High-risk jurisdictions (ISO-3166 alpha-2). Mirrors the prompt's matrix;
# kept here so the floor and the prompt cannot drift apart. PROD: your
# sanctions/geography policy feed.
HIGH_RISK_COUNTRIES: frozenset[str] = frozenset(
    {"IR", "KP", "SY", "CU", "RU", "BY", "MM"}
)

# Seeded sanctioned names (lower-cased substrings). PROD: replace with a
# real screening-provider lookup. Substring match models the conservative
# "any partial hit is a hit, a human clears it" screening posture.
_SANCTIONED_NAMES: tuple[str, ...] = (
    "sanctioned holdings",
    "blocked entity",
    "ofac test corp",
    "specially designated",
)

# Tax forms a vendor may legitimately have on file.
_VALID_TAX_FORMS: frozenset[str] = frozenset(
    {"W-9", "W-8BEN", "W-8BEN-E", "W-8", "1099"}
)


def _norm(s: Any) -> str:
    return str(s or "").strip().lower()


def sanctions_hits(legal_name: Any, declared_hits: Any = None) -> list[str]:
    """Return the union of any LLM-declared hits and a deterministic
    name screen. A deterministic hit can never be argued away by the LLM
    omitting it."""
    hits: list[str] = []
    if isinstance(declared_hits, list):
        hits.extend(str(h) for h in declared_hits if str(h).strip())
    name = _norm(legal_name)
    if name:
        for bad in _SANCTIONED_NAMES:
            if bad in name:
                hits.append("DETERMINISTIC_SDN_NAME_MATCH")
                break
    # de-dupe, preserve order
    seen: set[str] = set()
    out: list[str] = []
    for h in hits:
        if h not in seen:
            seen.add(h)
            out.append(h)
    return out


def is_high_risk_country(country_code: Any) -> bool:
    return str(country_code or "").strip().upper() in HIGH_RISK_COUNTRIES


def tax_form_ok(tax_form_kind: Any) -> bool:
    """True only when a recognised tax form is on file (onboarding floor)."""
    return str(tax_form_kind or "").strip().upper().replace(" ", "") in {
        t.upper().replace(" ", "") for t in _VALID_TAX_FORMS
    }


def screen_counterparty(
    legal_name: Any,
    country_code: Any,
    *,
    declared_sanctions_hits: Any = None,
    tax_form_kind: Any = None,
    onboarding: bool = False,
) -> dict[str, Any]:
    """Run the deterministic compliance floor.

    Returns the screen verdict. `forces_compliance_review` is True on ANY
    hit (sanctions / geography / missing tax form in onboarding mode) —
    the compliance LLM cannot clear a flagged counterparty.
    """
    hits = sanctions_hits(legal_name, declared_sanctions_hits)
    high_risk = is_high_risk_country(country_code)
    missing_tax_form = onboarding and not tax_form_ok(tax_form_kind)

    forces = bool(hits) or high_risk or missing_tax_form
    return {
        "sanctions_hits": hits,
        "has_sanctions_hit": bool(hits),
        "high_risk_country": high_risk,
        "missing_tax_form": missing_tax_form,
        "forces_compliance_review": forces,
        "requires_filing_sar": bool(hits),
    }
