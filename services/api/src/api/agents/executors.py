"""Channel executor registry (A1 D9).

The task dispatcher only claims tasks whose channel has a registered executor,
so in A1 (none registered) planned tasks stay ``scheduled`` -- visible, never
dropped, never faked. A2 registers ``email``; A6 registers ``call``.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from common.auth import AuthClaims
from common.db import Database

from api.agents.outreach_repository import ClaimedTask


@dataclass(frozen=True)
class ExecutorResult:
    status: str  # completed | failed | escalated
    result: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


class TransientExecutorError(Exception):
    """Retry later (provider timeout, 5xx). Anything deterministic returns ``failed``."""


class DeferTask(Exception):  # noqa: N818 -- a control signal, not an error
    """Not now, and not the task's fault (e.g. no free call slot): reschedule after
    ``delay`` without using an attempt -- the same as a non-permanent gate block."""

    def __init__(self, code: str, delay: timedelta) -> None:
        super().__init__(code)
        self.code = code
        self.delay = delay


Executor = Callable[[Database, AuthClaims, ClaimedTask, str], Awaitable[ExecutorResult]]

_REGISTRY: dict[str, Executor] = {}

# Per-channel capacity checks (A6 L9): ``True`` = room for one more right now.
CapacityCheck = Callable[[Database, AuthClaims], Awaitable[bool]]
_CAPACITY: dict[str, CapacityCheck] = {}


def register_capacity(channel: str, check: CapacityCheck) -> None:
    _CAPACITY[channel] = check


def get_capacity(channel: str) -> CapacityCheck | None:
    return _CAPACITY.get(channel)


def register(channel: str, executor: Executor) -> None:
    _REGISTRY[channel] = executor


def unregister(channel: str) -> None:
    _REGISTRY.pop(channel, None)
    _CAPACITY.pop(channel, None)


def get_executor(channel: str) -> Executor | None:
    return _REGISTRY.get(channel)


def registered_channels() -> list[str]:
    return sorted(_REGISTRY)
