"""The ``email`` channel executor (A2). Importing this module registers it with the
A1 dispatcher. The gate has already passed when this runs."""
from __future__ import annotations

from common.auth import AuthClaims
from common.db import Database

from api.agents import executors
from api.agents.executors import ExecutorResult
from api.agents.outreach_repository import ClaimedTask
from api.email_agent.service import prepare_and_maybe_send

# Prepared.status -> task status. A draft parks the task in ``pending`` (awaiting a human).
_TASK_STATUS = {"sent": "completed", "draft": "pending", "escalated": "escalated"}


async def email_executor(db: Database, claims: AuthClaims, task: ClaimedTask,
                         run_id: str) -> ExecutorResult:
    prepared = await prepare_and_maybe_send(db, claims, task, run_id)
    status = _TASK_STATUS.get(prepared.status)
    if status is None:  # a redelivered task whose message was already discarded/failed
        return ExecutorResult(status="cancelled", error=f"EMAIL_{prepared.status.upper()}")
    return ExecutorResult(status=status,
                          result={"message_id": prepared.message_id, "email": prepared.status},
                          error=prepared.reason)


executors.register("email", email_executor)
