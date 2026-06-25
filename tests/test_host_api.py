# SPDX-FileCopyrightText: 2026 Gary Frattarola <garyf@parkviewlab.ai>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Unit tests for `cobalt_grinding.host.api` — the public `Host.run_agent` surface.

Composes the four host pieces with fakes so we can verify wiring
without spawning subprocesses or hitting the LLM:

  - `_FakeToolsRetriever` records search calls and returns canned descriptors.
  - `_ScriptedProvider` returns a single `end_turn` so the loop returns.
  - A fake McpClientManager satisfies the loop's interface (it'll never
    actually be called because the scripted provider doesn't emit
    tool_use blocks).

The integration of all four pieces against real LanceDB + real LLM +
real subprocess lives in `test_host_integration.py`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from cobalt_grinding.daemon.mcp_clients import CallResult, ToolDescriptor
from cobalt_grinding.host.api import DEFAULT_TOP_K, Host, _retrieval_query

# ---- fakes ----


@dataclass
class _FakeRetriever:
    canned: list[ToolDescriptor] = field(default_factory=list)
    calls: list[tuple[str, int]] = field(default_factory=list)

    def search(self, query: str, *, top_k: int = DEFAULT_TOP_K) -> list[ToolDescriptor]:
        self.calls.append((query, top_k))
        return self.canned


@dataclass
class _Msg:
    content: list[Any]
    stop_reason: str


@dataclass
class _ScriptedProvider:
    response: _Msg
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def complete(self, **kwargs: Any) -> _Msg:
        self.calls.append(kwargs)
        return self.response


class _FakeManager:
    async def call_tool(
        self,
        client_name: str,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> CallResult:
        return CallResult(ok=True, is_error=False, content=[], error_text=None)


def _td(prefixed: str) -> ToolDescriptor:
    return ToolDescriptor(
        prefixed_name=prefixed,
        raw_name=prefixed.split(".", 1)[1],
        description=f"do {prefixed}",
        input_schema={"type": "object"},
        owning_child=prefixed.split(".", 1)[0],
    )


def _host(provider: _ScriptedProvider, retriever: _FakeRetriever) -> Host:
    return Host(
        provider=provider,
        mcp_clients=_FakeManager(),  # type: ignore[arg-type]
        tools_index=retriever,
    )


# ---- run_agent wiring ----


async def test_run_agent_uses_latest_user_message_as_query() -> None:
    retriever = _FakeRetriever(canned=[])
    provider = _ScriptedProvider(_Msg(content=[], stop_reason="end_turn"))
    host = _host(provider, retriever)

    await host.run_agent(
        system="s",
        messages=[
            {"role": "user", "content": "first question"},
            {"role": "assistant", "content": "first answer"},
            {"role": "user", "content": "second question, latest"},
        ],
    )

    assert retriever.calls == [("second question, latest", DEFAULT_TOP_K)]


async def test_run_agent_passes_retrieved_tools_to_provider() -> None:
    retriever = _FakeRetriever(canned=[_td("codeparse.parse_file"), _td("pdf.extract_text")])
    provider = _ScriptedProvider(_Msg(content=[], stop_reason="end_turn"))
    host = _host(provider, retriever)

    await host.run_agent(system="s", messages=[{"role": "user", "content": "anything"}])

    tools_arg = provider.calls[0]["tools"]
    assert {t["name"] for t in tools_arg} == {"codeparse.parse_file", "pdf.extract_text"}


async def test_run_agent_top_k_override_threads_through_to_search() -> None:
    retriever = _FakeRetriever(canned=[])
    provider = _ScriptedProvider(_Msg(content=[], stop_reason="end_turn"))
    host = _host(provider, retriever)

    await host.run_agent(
        system="s",
        messages=[{"role": "user", "content": "x"}],
        top_k=3,
    )
    assert retriever.calls[0] == ("x", 3)


async def test_run_agent_with_no_tools_indexed_still_runs() -> None:
    """Empty tools list shouldn't break the loop — the LLM just answers
    without tool access."""
    retriever = _FakeRetriever(canned=[])
    provider = _ScriptedProvider(_Msg(content=[], stop_reason="end_turn"))
    host = _host(provider, retriever)

    result = await host.run_agent(system="s", messages=[{"role": "user", "content": "hi"}])
    assert result.stop_reason == "end_turn"
    # `tools=None` (or omitted) is OK; loop converts empty list to None.
    assert provider.calls[0].get("tools") is None


# ---- _retrieval_query helper ----


def test_retrieval_query_string_user_content() -> None:
    assert _retrieval_query([{"role": "user", "content": "hello"}]) == "hello"


def test_retrieval_query_walks_back_past_assistant_turns() -> None:
    msgs = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reply"},
        {"role": "user", "content": "second"},
    ]
    assert _retrieval_query(msgs) == "second"


def test_retrieval_query_extracts_text_blocks() -> None:
    msgs = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "find the bug"},
                {"type": "text", "text": "in this file"},
            ],
        },
    ]
    assert _retrieval_query(msgs) == "find the bug in this file"


def test_retrieval_query_skips_non_text_blocks() -> None:
    msgs = [
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "content": "..."},
                {"type": "text", "text": "actual question"},
            ],
        },
    ]
    assert _retrieval_query(msgs) == "actual question"


def test_retrieval_query_empty_when_no_user_message() -> None:
    msgs = [{"role": "assistant", "content": "no user yet"}]
    assert _retrieval_query(msgs) == ""


def test_retrieval_query_empty_when_only_tool_results() -> None:
    msgs = [{"role": "user", "content": [{"type": "tool_result", "content": "..."}]}]
    assert _retrieval_query(msgs) == ""
