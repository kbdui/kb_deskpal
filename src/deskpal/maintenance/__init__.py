"""Explicit local setup and data maintenance, independent of Agent Core.

Import public commands from ``deskpal.maintenance.service``. Keeping package
initialization empty lets the production restore barrier load without the UI or
maintenance command adapters.
"""
