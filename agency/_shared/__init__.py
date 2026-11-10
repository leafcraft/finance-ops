"""finance-ops — shared utilities and custom external connectors.

This package holds the two `framework: custom` ExternalConnector
implementations the YAML references:

  - audit_logger.py  → hash-chained WORM audit sink
  - erp_poster.py    → posts approved invoices to the configured ERP

They are imported from main.py BEFORE leafmesh.start() so they
register under name "custom" before agent registration.

Auto-discovery in the configuration has `recursive: false`, so this
sub-package is intentionally excluded from agent discovery.
"""
