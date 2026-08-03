"""Unified customer-360 timeline (SR-9.3).

Fans out to the conversation-store, lead-capture-crm, scheduling-service,
and notification-service modules through their own repository contracts,
merges the results into one time-ordered, tenant-isolated view for a Contact
or a not-yet-converted Lead. Read-only surface; see the sprint spec
(``dev_plan/sprints/SR-9.3-unified-customer-360-timeline.md``) for the full
set of locked decisions this module implements.
"""
from __future__ import annotations
