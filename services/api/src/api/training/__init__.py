"""Train the Agent module -- coverage gaps, stateless bot preview, taught answers.

Aggregates ``conversation_store`` (read, for coverage gaps) and
``ingestion``/``orchestrator`` (write/preview) through their own repositories
and functions -- owns no table but its own ``training_answers``.
- Tenant-scoped repository for ``training_answers`` (repository.py).
- Coverage-gaps + test-bot-chat + teach-an-answer endpoints (routes.py).
"""
