"""finance-ops — agency package.

YAML-FIRST: most agents are pure YAML (the configuration) — LLM agents
get structured output derived from their yields:, and programmatic
system-of-record agents are connector-only (integration + connector_config).
The few modules here exist only where Python MUST be:

  invoice_intake_agent.py   pre-compose vendor context + completeness floor
  gl_coding_agent.py        pre-gate stash writer + keyword fallback chart
  matching_agent.py         ERP PO/3-way lookup + bounded re-extraction loop guard
  payment_risk_agent.py     pre-compose dup candidates + duplicate-hash floor
  vendor_compliance_agent.py pre-compose vendor record + sanctions/tax-form floor
  regulatory_monitor_agent.py filing-trigger floor (urgency → compliance review)
  erp_poster_agent.py       actuator — ERP write, fail-closed post-HITL recovery
  loan_origination_agent.py domain math — FICO/DSCR credit policy matrix
  loan_servicing_agent.py   domain math — amortisation / payoff / delinquency

Human agents (ap_clerk_human, controller_human, cfo_human) have NO Python
files here — the SDK's human_interface handles them.
"""
