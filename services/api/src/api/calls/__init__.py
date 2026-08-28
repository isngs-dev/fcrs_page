"""Missed-call text-back module.

Owns ``tenant_call_configs`` (which Twilio number to watch, on/off, the
admin's text-back message). Consumes ``api.notifications`` (enqueue +
Celery-dispatch the SMS) and ``api.notifications.config_repository`` (reads
the tenant's already-configured Twilio Auth Token to verify inbound webhook
signatures) through their own public functions -- never reaches into their
tables directly.
- Tenant-scoped + claims-less repository for ``tenant_call_configs`` (repository.py).
- Public Twilio call-status webhook (webhook.py).
- Admin config CRUD (admin_routes.py).
"""
