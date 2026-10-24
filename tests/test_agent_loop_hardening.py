"""TDD tests for M2: truncated tool-call detection & execution guard.

Written BEFORE the fix, per the optimize/agent-loop-must-optimize.md §4.d spec.
Groups 1, 2, 4 and 5 pin that spec (finish_reason propagation, the truncation
gate, backfilling every tool_call id, and not false-positiving on zero-arg
tools). Groups 3 and 6-9 arrive with the P1 follow-ups.
"""

from types import SimpleNamespace
from typing import ClassVar
from unittest import mock

from corecoder.agent import Agent
from corecoder.demo import ScriptedLLM
from corecoder.llm import LLM, LLMResponse, ToolCall
from corecoder.tools.base import Tool

# ─── helpers ────────────────────────────────────────────────────────────────

def assert_tool_calls_answered(messages: list[dict]):
    """Every assistant tool_call id must have exactly one matching tool reply."""
    ids = [
        tc["id"]
        for m in messages
        if m.get("role") == "assistant"
        for tc in (m.get("tool_calls") or [])
    ]
    answers = [m["tool_call_id"] for m in messages if m.get("role") == "tool"]
    assert sorted(ids) == sorted(answers), f"dangling: ids={ids} answers={answers}"


def _chunk(*, content=None, tool_calls=None, finish_reason=None, usage=None):
    """One streamed chunk shaped like the OpenAI SDK's."""
    return SimpleNamespace(
        usage=usage,
        choices=[SimpleNamespace(
            delta=SimpleNamespace(content=content, tool_calls=tool_calls),
            finish_reason=finish_reason,
        )],
    )


def _tool_call_delta(name="spy", arguments="", index=0, call_id="t1"):
    return SimpleNamespace(
        index=index,
        id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def _mock_llm(*streams):
    """A real LLM whose HTTP layer replays one scripted stream per call."""
    llm = LLM(model="gpt-4o", api_key="sk-test")
    llm.client = mock.Mock()
    llm.client.chat.completions.create.side_effect = list(streams)
    return llm


class SpyTool(Tool):
    """A zero-arg tool that records how many times it was executed."""

    name = "spy"
    description = "records execution count"
    parameters: ClassVar[dict] = {"type": "object", "properties": {}}

    def __init__(self):
        self.calls: list = []

    def execute(self) -> str:
        self.calls.append(1)
        return "executed"


class KwargsSpyTool(Tool):
    """MCP-shaped tool: `execute(**kwargs)` binds anything, including {}."""

    name = "kwspy"
    description = "records the kwargs it was called with"
    parameters: ClassVar[dict] = {"type": "object", "properties": {}}

    def __init__(self):
        self.calls: list = []

    def execute(self, **kwargs) -> str:
        self.calls.append(kwargs)
        return "executed"


# ─── 1. test_drain_records_finish_reason_length ─────────────────────────────

class TestDrainFinishReason:
    """_drain must propagate finish_reason from the last chunk into LLMResponse."""

    @staticmethod
    def _stream_with_finish_reason(reason: str):
        """Build a mock stream that carries finish_reason on the final chunk."""
        return [
            SimpleNamespace(
                usage=None,
                choices=[SimpleNamespace(
                    delta=SimpleNamespace(content="hello", tool_calls=None),
                    finish_reason=None,
                )],
            ),
            SimpleNamespace(
                usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
                choices=[SimpleNamespace(
                    delta=SimpleNamespace(content=None, tool_calls=None),
                    finish_reason=reason,
                )],
            ),
        ]

    def test_drain_records_finish_reason_length(self):
        llm = LLM(model="gpt-4o", api_key="sk-test")
        llm.client = mock.Mock()
        llm.client.chat.completions.create.return_value = (
            self._stream_with_finish_reason("length")
        )
        resp = llm.chat(messages=[{"role": "user", "content": "hi"}])
        assert resp.finish_reason == "length"

    def test_drain_finish_reason_stop(self):
        llm = LLM(model="gpt-4o", api_key="sk-test")
        llm.client = mock.Mock()
        llm.client.chat.completions.create.return_value = (
            self._stream_with_finish_reason("stop")
        )
        resp = llm.chat(messages=[{"role": "user", "content": "hi"}])
        assert resp.finish_reason == "stop"


# ─── 2. test_truncated_tool_call_is_not_executed ────────────────────────────

class TestTruncationGate:
    """When finish_reason == 'length' and tool_calls are present, agent must
    NOT execute any tool and must backfill a truncation message per id."""

    def test_truncated_tool_call_is_not_executed(self):
        spy = SpyTool()
        script = [
            LLMResponse(
                finish_reason="length",
                tool_calls=[ToolCall(id="t1", name="spy", arguments={})],
            ),
            LLMResponse(content="done"),
        ]
        agent = Agent(llm=ScriptedLLM(script), tools=[spy])
        answer = agent.chat("go")

        # the tool must NOT have been executed
        assert len(spy.calls) == 0, "truncated tool call was executed!"
        # the tool reply must mention truncation
        tool_msgs = [m for m in agent.messages if m.get("role") == "tool"]
        assert any("truncated" in (m.get("content") or "") for m in tool_msgs)
        # history is valid: every tool_call id has a reply
        assert_tool_calls_answered(agent.messages)
        # final answer is the text from the second turn
        assert answer == "done"


# ─── 4. test_zero_arg_tool_with_empty_arguments_still_runs ─────────────────

class TestZeroArgGuard:
    """A normal (finish_reason='stop') call with arguments={} must still
    execute — the guard must not false-positive on legitimate zero-arg tools."""

    def test_zero_arg_tool_with_empty_arguments_still_runs(self):
        spy = SpyTool()
        script = [
            LLMResponse(
                finish_reason="stop",
                tool_calls=[ToolCall(id="t1", name="spy", arguments={})],
            ),
            LLMResponse(content="done"),
        ]
        agent = Agent(llm=ScriptedLLM(script), tools=[spy])
        answer = agent.chat("go")

        assert len(spy.calls) == 1, "zero-arg tool was falsely blocked!"
        assert answer == "done"


# ─── 5. test_text_truncation_without_tool_calls_returns_content ─────────────

class TestTextOnlyTruncation:
    """finish_reason='length' with NO tool_calls: just return the text as-is;
    no special handling needed, history stays valid."""

    def test_text_truncation_without_tool_calls_returns_content(self):
        script = [
            LLMResponse(content="partial answer", finish_reason="length"),
        ]
        agent = Agent(llm=ScriptedLLM(script), tools=[])
        answer = agent.chat("go")

        assert answer == "partial answer"
        # history should only have user + assistant text, no tool messages
        assert not any(m.get("role") == "tool" for m in agent.messages)

