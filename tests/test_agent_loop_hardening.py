"""TDD tests for M2: truncated tool-call detection & execution guard.

Written BEFORE the fix, against the truncation spec that motivated M2.
Groups 1, 2, 4 and 5 pin that spec (finish_reason propagation, the truncation
gate, backfilling every tool_call id, and not false-positiving on zero-arg
tools). Groups 3 and 6-9 pin the P1 follow-ups the review turned up: an
unparseable argument string must never reach a tool, and the stop must be
visible to the REPL, to one-shot mode and to a sub-agent.
"""

from types import SimpleNamespace
from typing import ClassVar
from unittest import mock

import pytest

from corecoder.agent import Agent, TruncatedOutput
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


# ─── 3. consecutive truncation terminates loudly (P1-2) ────────────────────

class TestDoubleTruncation:
    """Two consecutive truncated rounds must raise TruncatedOutput: a return
    value would be dropped by the REPL, ignored by one-shot mode and treated
    as success by sub-agents."""

    def test_two_consecutive_truncations_raise(self):
        spy = SpyTool()
        script = [
            LLMResponse(
                finish_reason="length",
                tool_calls=[ToolCall(id="t1", name="spy", arguments={})],
            ),
            LLMResponse(
                finish_reason="length",
                tool_calls=[ToolCall(id="t2", name="spy", arguments={})],
            ),
        ]
        agent = Agent(llm=ScriptedLLM(script), tools=[spy], max_rounds=10)
        with pytest.raises(TruncatedOutput):
            agent.chat("go")

        assert len(spy.calls) == 0
        # the history must still be valid for a later retry or /save
        assert_tool_calls_answered(agent.messages)


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


# ─── 6. P1-1: unparseable arguments never reach a tool ──────────────────────

class TestInvalidJsonGuard:
    """`json.loads` failing outside a `length` truncation used to substitute {}
    silently. The MCP-shaped tool binds {} happily and would run with defaults,
    so the loop must reject the call itself, not just rely on signature binding."""

    def test_invalid_json_with_stop_reason_is_not_executed(self):
        tool = KwargsSpyTool()
        llm = _mock_llm(
            [
                _chunk(tool_calls=[_tool_call_delta(
                    name="kwspy", arguments='{"text": "abc')]),
                _chunk(finish_reason="stop"),
            ],
            [_chunk(content="done", finish_reason="stop")],
        )
        agent = Agent(llm=llm, tools=[tool])
        answer = agent.chat("go")

        assert tool.calls == [], "a call with broken JSON was executed!"
        replies = [m["content"] for m in agent.messages if m.get("role") == "tool"]
        assert any("not a valid JSON object" in r for r in replies)
        assert_tool_calls_answered(agent.messages)
        assert answer == "done"

    def test_json_array_arguments_are_rejected(self):
        # valid JSON, but not an object: **kwargs cannot take it either
        tool = KwargsSpyTool()
        llm = _mock_llm(
            [
                _chunk(tool_calls=[_tool_call_delta(name="kwspy", arguments='["t1"]')]),
                _chunk(finish_reason="stop"),
            ],
            [_chunk(content="done", finish_reason="stop")],
        )
        agent = Agent(llm=llm, tools=[tool])
        agent.chat("go")

        assert tool.calls == []

    def test_empty_arguments_string_still_runs_zero_arg_tool(self):
        # `arguments: ""` is how providers express "no parameters": not an error
        tool = KwargsSpyTool()
        llm = _mock_llm(
            [
                _chunk(tool_calls=[_tool_call_delta(name="kwspy", arguments="")]),
                _chunk(finish_reason="tool_calls"),
            ],
            [_chunk(content="done", finish_reason="stop")],
        )
        agent = Agent(llm=llm, tools=[tool])
        agent.chat("go")

        assert len(tool.calls) == 1, "zero-arg tool was falsely blocked!"

    def test_malformed_arguments_do_not_reach_hooks_or_permission(self):
        called = []
        tool = KwargsSpyTool()
        llm = _mock_llm(
            [
                _chunk(tool_calls=[_tool_call_delta(
                    name="kwspy", arguments='{"text": "abc')]),
                _chunk(finish_reason="stop"),
            ],
            [_chunk(content="done", finish_reason="stop")],
        )
        agent = Agent(
            llm=llm,
            tools=[tool],
            hooks=mock.Mock(run_pre=lambda name, args: called.append(name) or None,
                            run_post=lambda *a: None),
        )
        agent.chat("go")

        # a call whose arguments are known to be broken must not be offered to
        # hooks or the consent layer: nobody should approve it
        assert called == []


# ─── 7. P1-2: the failure is visible to every caller ────────────────────────

class TestTruncationIsLoud:
    def test_one_shot_truncation_exits_nonzero(self):
        from corecoder import cli

        agent = mock.Mock()
        agent.permission = None
        agent.chat.side_effect = TruncatedOutput("model output truncated twice")

        with pytest.raises(SystemExit) as exc:
            cli._run_once(agent, "go")
        assert exc.value.code == 1

    def test_repl_reports_truncation_and_stays_alive(self, monkeypatch, capsys):
        from tests.conftest import repl_with

        agent = Agent(
            llm=ScriptedLLM([
                LLMResponse(finish_reason="length",
                            tool_calls=[ToolCall(id="t1", name="spy", arguments={})]),
                LLMResponse(finish_reason="length",
                            tool_calls=[ToolCall(id="t2", name="spy", arguments={})]),
            ]),
            tools=[SpyTool()],
        )

        repl_with(monkeypatch, agent, ["go", "quit"])

        assert "truncated twice" in capsys.readouterr().out

    def test_subagent_truncation_is_not_reported_as_completed(self, monkeypatch):
        from corecoder.tools.agent import AgentTool

        tool = AgentTool()
        parent = mock.Mock()
        parent.llm = ScriptedLLM([])
        parent.tools = []
        parent.context.max_tokens = 1000
        parent.permission = None
        parent.hooks = None
        tool._parent_agent = parent

        def boom(self, task):
            raise TruncatedOutput("model output truncated twice")

        monkeypatch.setattr(Agent, "chat", boom)
        out = tool.execute("do the thing")

        assert out.startswith("Sub-agent error")
        assert "[Sub-agent completed]" not in out


# ─── 8. review follow-up: the parallel batch is not a blind spot ────────────

class GoodSpyTool(KwargsSpyTool):
    """The well-behaved half of a mixed batch."""

    name = "good"


class TestParallelBatch:
    """A batch with one fine call and one broken call is the only path that
    reaches the parallel short-circuit; a single-call round never does."""

    def test_mixed_batch_runs_the_good_call_and_refuses_the_broken_one(self):
        good, bad = GoodSpyTool(), KwargsSpyTool()
        pre, post = [], []
        llm = _mock_llm(
            [
                _chunk(tool_calls=[
                    _tool_call_delta(name="good", arguments='{"text": "ok"}',
                                     index=0, call_id="t1"),
                    _tool_call_delta(name="kwspy", arguments='{"text": "ab',
                                     index=1, call_id="t2"),
                ]),
                _chunk(finish_reason="tool_calls"),
            ],
            [_chunk(content="done", finish_reason="stop")],
        )
        agent = Agent(
            llm=llm,
            tools=[good, bad],
            hooks=mock.Mock(run_pre=lambda name, args: pre.append(name) or None,
                            run_post=lambda name, *a: post.append(name)),
        )
        agent.chat("go")

        assert good.calls == [{"text": "ok"}]
        assert bad.calls == []
        # refused before hooks: the broken call is never offered for approval
        assert pre == ["good"]
        # and it is never submitted to the pool, so it gets no post hook either
        assert post == ["good"]
        replies = {m["tool_call_id"]: m["content"]
                   for m in agent.messages if m.get("role") == "tool"}
        assert replies["t1"] == "executed"
        assert "not a valid JSON object" in replies["t2"]


# ─── 9. review follow-up: a tiny max_rounds must not silence the stop ──────

class TestRoundCapTruncation:
    def test_truncation_at_the_round_cap_is_still_loud(self):
        agent = Agent(
            llm=ScriptedLLM([LLMResponse(
                finish_reason="length",
                tool_calls=[ToolCall(id="t1", name="spy", arguments={})])]),
            tools=[SpyTool()],
            max_rounds=1,
        )

        # the cap runs out before a re-issue can happen, but the run still
        # failed: returning the max-rounds string would exit 0 in one-shot mode
        with pytest.raises(TruncatedOutput):
            agent.chat("go")
