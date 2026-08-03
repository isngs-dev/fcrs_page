"""MANDATORY multi-tenant isolation test for the orchestrator turn pipeline (S10.2).

Asserts every downstream call in ``answer_turn`` receives ``claims`` scoped to
the visitor's own tenant (including the new ``get_orchestrator_config`` and
``provider.classify``), and that a ``conversation_id`` belonging to another
tenant/visitor is invisible -> 404 CONVERSATION_NOT_FOUND, with classify,
retrieve_hybrid, and generate never reached.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from common.auth import AuthClaims, Role
from common.errors import NotFoundError

from api.llm.config_repository import LLMConfig
from api.llm.provider import Chunk, Completion
from api.orchestrator.config_repository import OrchestratorConfig
from api.orchestrator.service import _NO_ANSWER_SENTINEL, answer_turn, answer_turn_stream
from api.rag.service import HybridMatch, HybridResult

_TENANT_A = "tenant-a"


def _stream_stub(chunks: list[str]) -> MagicMock:
    """A ``provider.stream`` stand-in -- a plain (sync) callable returning a
    fresh async generator per call, mirroring the real provider shape (NOT a
    coroutine -- see test_orchestrator_service.py's identical stub)."""

    def _side_effect(*args: Any, **kwargs: Any) -> AsyncIterator[Chunk]:
        async def _gen() -> AsyncIterator[Chunk]:
            for text in chunks:
                yield Chunk(text=text)

        return _gen()

    return MagicMock(side_effect=_side_effect)


def _claims_a(subject: str = "visitor-a") -> AuthClaims:
    return AuthClaims(subject=subject, role=Role.VISITOR, tenant_id=_TENANT_A)


def _config() -> LLMConfig:
    return LLMConfig(
        provider="anthropic", model="claude-opus-4-8", api_key="sk-test",
        embedding_model="nomic-embed-text",
    )


async def test_every_downstream_call_carries_tenant_a_claims() -> None:
    """A visitor of tenant A drives a turn -- every downstream call receives
    claims with tenant_id == tenant A, including get_orchestrator_config,
    count_messages, get_last_assistant_decision, and provider.classify
    (called with tenant A's own model)."""
    get_llm_config = AsyncMock(return_value=_config())
    get_orchestrator_config = AsyncMock(
        return_value=OrchestratorConfig(answer_threshold=0.5, escalate_threshold=0.35, turn_cap=6)
    )
    create_conversation = AsyncMock(return_value="conv-a")
    get_message = AsyncMock(return_value=None)
    append_message = AsyncMock(return_value="msg-1")
    get_working_memory = AsyncMock(return_value={"summary": None, "summary_message_count": 0, "messages": []})
    count_messages = AsyncMock(return_value=1)
    get_last_assistant_decision = AsyncMock(return_value=None)
    get_availability = AsyncMock(return_value=None)
    retrieve_hybrid = AsyncMock(
        return_value=HybridResult(
            chunks=[
                HybridMatch(
                    doc_id="d1", chunk_id="c1", content="text", score=0.9,
                    rrf_score=0.5, matched_by=["vector"],
                )
            ],
            confidence=0.9,
        )
    )
    provider = AsyncMock()
    provider.classify = AsyncMock(return_value="question")
    provider.generate = AsyncMock(
        return_value=Completion(
            text=_NO_ANSWER_SENTINEL,
            model="claude-opus-4-8",
            input_tokens=1,
            output_tokens=1,
        )
    )
    provider_for = lambda cfg: provider  # noqa: E731

    with (
        patch("api.orchestrator.service.get_llm_config", get_llm_config),
        patch("api.orchestrator.service.get_orchestrator_config", get_orchestrator_config),
        patch("api.orchestrator.service.create_conversation", create_conversation),
        patch("api.orchestrator.service.get_message", get_message),
        patch("api.orchestrator.service.append_message", append_message),
        patch("api.orchestrator.service.get_working_memory", get_working_memory),
        patch("api.orchestrator.service.count_messages", count_messages),
        patch("api.orchestrator.service.get_last_assistant_decision", get_last_assistant_decision),
        patch("api.orchestrator.service.get_availability", get_availability),
        patch("api.orchestrator.service.retrieve_hybrid", retrieve_hybrid),
        patch("api.orchestrator.service.provider_for", provider_for),
    ):
        result = await answer_turn(db=object(), claims=_claims_a(), message="hello", message_id="turn-1")

    for mock in (
        get_llm_config,
        get_orchestrator_config,
        create_conversation,
        append_message,
        get_working_memory,
        count_messages,
        get_last_assistant_decision,
        get_availability,
        retrieve_hybrid,
    ):
        for call in mock.await_args_list:
            claims_arg = call.args[1] if len(call.args) > 1 else call.kwargs.get("claims")
            assert claims_arg is not None
            assert claims_arg.tenant_id == _TENANT_A
            assert claims_arg.role == Role.VISITOR

    provider.classify.assert_awaited_once()
    assert provider.classify.await_args.kwargs["model"] == "claude-opus-4-8"
    # count_messages is called twice per turn (SR-14 D10): once for the raw
    # role="user" turn count, once for role="bot" decision="identity_gate" to
    # net out gate turns from the turn-cap budget.
    assert count_messages.await_count == 2
    get_last_assistant_decision.assert_awaited_once()
    get_availability.assert_not_awaited()

    # The clarify outcome exposes/stores no tenant or visitor identifier.
    assert result.decision == "clarify"
    assert _TENANT_A not in result.reply
    assert "visitor-a" not in result.reply
    assistant_kwargs = append_message.await_args_list[1].kwargs
    assert "tenant_id" not in assistant_kwargs
    assert "visitor_id" not in assistant_kwargs


async def test_guardrail_block_still_carries_tenant_a_claims_scan_output_tenant_agnostic() -> None:
    """A guardrail block still carries tenant-A claims into append_message
    (the stored blocked turn is tenant-scoped), and scan_output receives
    ONLY the reply text -- never claims/tenant data (MANDATORY assertion)."""
    get_llm_config = AsyncMock(return_value=_config())
    get_orchestrator_config = AsyncMock(
        return_value=OrchestratorConfig(answer_threshold=0.5, escalate_threshold=0.35, turn_cap=6)
    )
    create_conversation = AsyncMock(return_value="conv-a")
    get_message = AsyncMock(return_value=None)
    append_message = AsyncMock(return_value="msg-1")
    get_working_memory = AsyncMock(return_value={"summary": None, "summary_message_count": 0, "messages": []})
    count_messages = AsyncMock(return_value=1)
    get_last_assistant_decision = AsyncMock(return_value=None)
    get_availability = AsyncMock(return_value=None)
    retrieve_hybrid = AsyncMock(
        return_value=HybridResult(
            chunks=[
                HybridMatch(
                    doc_id="d1", chunk_id="c1", content="text", score=0.9,
                    rrf_score=0.5, matched_by=["vector"],
                )
            ],
            confidence=0.9,
        )
    )
    provider = AsyncMock()
    provider.classify = AsyncMock(return_value="question")
    provider.generate = AsyncMock(
        return_value=Completion(
            text="i am a human", model="claude-opus-4-8", input_tokens=1, output_tokens=1,
        )
    )
    provider_for = lambda cfg: provider  # noqa: E731

    from api.orchestrator.guardrails import scan_output as real_scan_output

    with (
        patch("api.orchestrator.service.get_llm_config", get_llm_config),
        patch("api.orchestrator.service.get_orchestrator_config", get_orchestrator_config),
        patch("api.orchestrator.service.create_conversation", create_conversation),
        patch("api.orchestrator.service.get_message", get_message),
        patch("api.orchestrator.service.append_message", append_message),
        patch("api.orchestrator.service.get_working_memory", get_working_memory),
        patch("api.orchestrator.service.count_messages", count_messages),
        patch("api.orchestrator.service.get_last_assistant_decision", get_last_assistant_decision),
        patch("api.orchestrator.service.get_availability", get_availability),
        patch("api.orchestrator.service.retrieve_hybrid", retrieve_hybrid),
        patch("api.orchestrator.service.provider_for", provider_for),
        patch("api.orchestrator.service.scan_output", wraps=real_scan_output) as spy_scan,
    ):
        await answer_turn(db=object(), claims=_claims_a(), message="hello", message_id="turn-1")

    # append_message (the guardrail_flag write) is tenant/visitor-scoped --
    # the assistant append call is the 2nd call.
    assistant_call = append_message.await_args_list[1]
    claims_arg = assistant_call.args[1] if len(assistant_call.args) > 1 else assistant_call.kwargs.get("claims")
    assert claims_arg is not None
    assert claims_arg.tenant_id == _TENANT_A
    assert assistant_call.kwargs["guardrail_flag"] == "human_impersonation"

    # blocked NEVER checks availability (S10.4 decision 4) -- MANDATORY.
    get_availability.assert_not_awaited()

    # scan_output is tenant-agnostic: called with ONLY the reply text -- no
    # claims/tenant_id/visitor_id ever passed to it.
    spy_scan.assert_called_once()
    call = spy_scan.call_args
    assert call.args == ("i am a human",)
    assert call.kwargs == {}
    for arg in call.args:
        assert not isinstance(arg, AuthClaims)


async def test_cross_tenant_conversation_id_invisible_404_no_classify_rag_or_generate() -> None:
    """A conversation_id belonging to another tenant/visitor is invisible ->
    the store raises NotFoundError -> 404 CONVERSATION_NOT_FOUND; classify,
    retrieve_hybrid, and generate are never reached."""
    get_llm_config = AsyncMock(return_value=_config())
    get_orchestrator_config = AsyncMock(
        return_value=OrchestratorConfig(answer_threshold=0.5, escalate_threshold=0.35, turn_cap=6)
    )
    create_conversation = AsyncMock(return_value="conv-a")
    get_message = AsyncMock(return_value=None)
    append_message = AsyncMock(
        side_effect=NotFoundError("Conversation not found.", code="CONVERSATION_NOT_FOUND"),
    )
    get_working_memory = AsyncMock()
    count_messages = AsyncMock()
    get_availability = AsyncMock()
    retrieve_hybrid = AsyncMock()
    provider = AsyncMock()
    provider.classify = AsyncMock()
    provider.generate = AsyncMock()
    provider_for = lambda cfg: provider  # noqa: E731

    with (
        patch("api.orchestrator.service.get_llm_config", get_llm_config),
        patch("api.orchestrator.service.get_orchestrator_config", get_orchestrator_config),
        patch("api.orchestrator.service.create_conversation", create_conversation),
        patch("api.orchestrator.service.get_message", get_message),
        patch("api.orchestrator.service.append_message", append_message),
        patch("api.orchestrator.service.get_working_memory", get_working_memory),
        patch("api.orchestrator.service.count_messages", count_messages),
        patch("api.orchestrator.service.get_availability", get_availability),
        patch("api.orchestrator.service.retrieve_hybrid", retrieve_hybrid),
        patch("api.orchestrator.service.provider_for", provider_for),
    ):
        with pytest.raises(NotFoundError) as exc_info:
            await answer_turn(
                db=object(), claims=_claims_a(), message="hello",
                conversation_id="conv-belongs-to-tenant-b",
            )

    assert exc_info.value.code == "CONVERSATION_NOT_FOUND"
    provider.classify.assert_not_awaited()
    retrieve_hybrid.assert_not_awaited()
    provider.generate.assert_not_awaited()
    get_working_memory.assert_not_awaited()
    count_messages.assert_not_awaited()
    get_availability.assert_not_awaited()


# =====================================================================================
# S10.5: the STREAMING path carries the same tenant-A claims + isolation
# =====================================================================================


async def test_stream_every_downstream_call_carries_tenant_a_claims() -> None:
    """The streaming path (answer_turn_stream) carries tenant-A claims into
    every downstream call in _resolve_turn AND into the provider.stream
    call for the generate branch."""
    get_llm_config = AsyncMock(return_value=_config())
    get_orchestrator_config = AsyncMock(
        return_value=OrchestratorConfig(answer_threshold=0.5, escalate_threshold=0.35, turn_cap=6)
    )
    create_conversation = AsyncMock(return_value="conv-a")
    get_message = AsyncMock(return_value=None)
    append_message = AsyncMock(return_value="msg-1")
    get_working_memory = AsyncMock(return_value={"summary": None, "summary_message_count": 0, "messages": []})
    count_messages = AsyncMock(return_value=1)
    get_last_assistant_decision = AsyncMock(return_value=None)
    get_availability = AsyncMock(return_value=None)
    retrieve_hybrid = AsyncMock(
        return_value=HybridResult(
            chunks=[
                HybridMatch(
                    doc_id="d1", chunk_id="c1", content="text", score=0.9,
                    rrf_score=0.5, matched_by=["vector"],
                )
            ],
            confidence=0.9,
        )
    )
    provider = AsyncMock()
    provider.classify = AsyncMock(return_value="question")
    provider.stream = _stream_stub(["NO_ANSWER", "_FOUND"])
    provider_for = lambda cfg: provider  # noqa: E731

    with (
        patch("api.orchestrator.service.get_llm_config", get_llm_config),
        patch("api.orchestrator.service.get_orchestrator_config", get_orchestrator_config),
        patch("api.orchestrator.service.create_conversation", create_conversation),
        patch("api.orchestrator.service.get_message", get_message),
        patch("api.orchestrator.service.append_message", append_message),
        patch("api.orchestrator.service.get_working_memory", get_working_memory),
        patch("api.orchestrator.service.count_messages", count_messages),
        patch("api.orchestrator.service.get_last_assistant_decision", get_last_assistant_decision),
        patch("api.orchestrator.service.get_availability", get_availability),
        patch("api.orchestrator.service.retrieve_hybrid", retrieve_hybrid),
        patch("api.orchestrator.service.provider_for", provider_for),
    ):
        events = [
            event
            async for event in answer_turn_stream(
                db=object(), claims=_claims_a(), message="hello", message_id="turn-1",
            )
        ]

    assert any(e.type == "delta" for e in events)
    assert events[-1].type == "done"

    for mock in (
        get_llm_config,
        get_orchestrator_config,
        create_conversation,
        append_message,
        get_working_memory,
        count_messages,
        get_last_assistant_decision,
        get_availability,
        retrieve_hybrid,
    ):
        for call in mock.await_args_list:
            claims_arg = call.args[1] if len(call.args) > 1 else call.kwargs.get("claims")
            assert claims_arg is not None
            assert claims_arg.tenant_id == _TENANT_A
            assert claims_arg.role == Role.VISITOR

    provider.classify.assert_awaited_once()
    assert provider.classify.await_args.kwargs["model"] == "claude-opus-4-8"
    provider.stream.assert_called_once()
    assert provider.stream.call_args.kwargs["model"] == "claude-opus-4-8"
    # count_messages is called twice per turn (SR-14 D10) -- see the
    # non-streaming twin above.
    assert count_messages.await_count == 2
    get_last_assistant_decision.assert_awaited_once()
    get_availability.assert_not_awaited()  # answer branch never checks availability

    done = events[-1]
    assert done.data["decision"] == "clarify"
    assert "tenant_id" not in done.data
    assert "visitor_id" not in done.data
    assert _TENANT_A not in done.data["reply"]
    assert "visitor-a" not in done.data["reply"]


async def test_stream_cross_tenant_conversation_id_invisible_404_before_any_stream() -> None:
    """A conversation_id belonging to another tenant/visitor is invisible ->
    NotFoundError (404 CONVERSATION_NOT_FOUND) raised during _resolve_turn,
    BEFORE any delta/stream/store; classify, retrieve_hybrid, and stream are
    never reached."""
    get_llm_config = AsyncMock(return_value=_config())
    get_orchestrator_config = AsyncMock(
        return_value=OrchestratorConfig(answer_threshold=0.5, escalate_threshold=0.35, turn_cap=6)
    )
    create_conversation = AsyncMock(return_value="conv-a")
    get_message = AsyncMock(return_value=None)
    append_message = AsyncMock(
        side_effect=NotFoundError("Conversation not found.", code="CONVERSATION_NOT_FOUND"),
    )
    get_working_memory = AsyncMock()
    count_messages = AsyncMock()
    get_availability = AsyncMock()
    retrieve_hybrid = AsyncMock()
    provider = AsyncMock()
    provider.classify = AsyncMock()
    provider.stream = _stream_stub([])
    provider_for = lambda cfg: provider  # noqa: E731

    with (
        patch("api.orchestrator.service.get_llm_config", get_llm_config),
        patch("api.orchestrator.service.get_orchestrator_config", get_orchestrator_config),
        patch("api.orchestrator.service.create_conversation", create_conversation),
        patch("api.orchestrator.service.get_message", get_message),
        patch("api.orchestrator.service.append_message", append_message),
        patch("api.orchestrator.service.get_working_memory", get_working_memory),
        patch("api.orchestrator.service.count_messages", count_messages),
        patch("api.orchestrator.service.get_availability", get_availability),
        patch("api.orchestrator.service.retrieve_hybrid", retrieve_hybrid),
        patch("api.orchestrator.service.provider_for", provider_for),
    ):
        with pytest.raises(NotFoundError) as exc_info:
            events = [
                event
                async for event in answer_turn_stream(
                    db=object(), claims=_claims_a(), message="hello",
                    conversation_id="conv-belongs-to-tenant-b",
                )
            ]
            del events

    assert exc_info.value.code == "CONVERSATION_NOT_FOUND"
    provider.classify.assert_not_awaited()
    retrieve_hybrid.assert_not_awaited()
    provider.stream.assert_not_called()
    get_working_memory.assert_not_awaited()
    count_messages.assert_not_awaited()
    get_availability.assert_not_awaited()
