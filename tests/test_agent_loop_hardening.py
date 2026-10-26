"""TDD tests for M2: truncated tool-call detection & execution guard.

Written BEFORE the fix, against the truncation spec that motivated M2.
Groups 1, 2, 4 and 5 pin that spec (finish_reason propagation, the truncation
gate, backfilling every tool_call id, and not false-positiving on zero-arg
tools). Groups 3 and 6-9 pin the P1 follow-ups the review turned up: an
unparseable argument string must never reach a tool, and the stop must be
visible to the REPL, to one-shot mode and to a sub-agent. Group 10 pins M1:
any exception escaping a round must still leave every tool_call answered.
Group 11 pins M3: a provider context-length 400 collapses the history once
and retries once, and the local estimate counts the fixed request overhead.
Group 12 pins M4: every tool result is clamped and redacted before it enters
the history, and read_file refuses oversized files.
"""

import json
from types import SimpleNamespace
from typing import ClassVar
from unittest import mock

import pytest
from openai import BadRequestError

from corecoder.agent import Agent, TruncatedOutput
from corecoder.context import ContextManager, estimate_tokens
from corecoder.demo import ScriptedLLM
from corecoder.llm import LLM, LLMResponse, ToolCall
from corecoder.permissions import Permission
from corecoder.tools.base import Tool
from corecoder.tools.write import WriteFileTool
from corecoder.tracing import JsonlTracer

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


# ─── 10. M1: an escaping exception must not tear the history ────────────────

class ExitTool(Tool):
    """A tool that dies with a BaseException subclass, like SystemExit."""

    name = "exiter"
    description = "raises SystemExit"
    parameters: ClassVar[dict] = {"type": "object", "properties": {}}

    def execute(self):
        raise SystemExit(3)


class TestExceptionBackfill:
    """Only Ctrl+C used to be backfilled. Every other escape (a crashing UI
    callback, a crashing consent callback, a BaseException from a tool) left
    an assistant tool_calls message without replies, so every later request
    was rejected by the provider."""

    def test_on_tool_callback_exception_backfills_then_raises(self):
        spy = SpyTool()
        agent = Agent(
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[ToolCall(id="t1", name="spy", arguments={})]),
            ]),
            tools=[spy],
        )

        def boom(name, arguments):
            raise RuntimeError("UI callback crashed")

        with pytest.raises(RuntimeError, match="UI callback crashed"):
            agent.chat("go", on_tool=boom)

        assert_tool_calls_answered(agent.messages)
        replies = [(m["tool_call_id"], m["content"])
                   for m in agent.messages if m.get("role") == "tool"]
        assert replies == [("t1", "[interrupted]")]
        # the callback crashed before the tool ran, so nothing executed
        assert spy.calls == []

    def test_permission_ask_exception_backfills_then_raises(self, tmp_path):
        target = tmp_path / "a.txt"

        def raiser(name, arguments):
            raise PermissionError("consent callback crashed")

        agent = Agent(
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[ToolCall(
                    id="t1", name="write_file",
                    arguments={"file_path": str(target), "content": "x\n"},
                )]),
            ]),
            tools=[WriteFileTool()],
            permission=Permission(ask=raiser),
        )

        with pytest.raises(PermissionError):
            agent.chat("go")

        assert_tool_calls_answered(agent.messages)
        replies = [m["content"] for m in agent.messages if m.get("role") == "tool"]
        assert replies == ["[interrupted]"]
        assert not target.exists()

    def test_tool_baseexception_backfills_then_raises(self):
        agent = Agent(
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[ToolCall(id="t1", name="exiter", arguments={})]),
            ]),
            tools=[ExitTool()],
        )

        with pytest.raises(SystemExit):
            agent.chat("go")

        assert_tool_calls_answered(agent.messages)
        replies = [m["content"] for m in agent.messages if m.get("role") == "tool"]
        assert replies == ["[interrupted]"]

    def test_parallel_worker_exception_backfills_whole_batch(self):
        spy = SpyTool()
        agent = Agent(
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[
                    ToolCall(id="t1", name="spy", arguments={}),
                    ToolCall(id="t2", name="exiter", arguments={}),
                ]),
            ]),
            tools=[spy, ExitTool()],
        )

        with pytest.raises(SystemExit):
            agent.chat("go")

        # the sibling of the crashed call must be answered too
        assert_tool_calls_answered(agent.messages)

    def test_success_path_backfill_is_noop(self):
        spy = SpyTool()
        agent = Agent(
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[ToolCall(id="t1", name="spy", arguments={})]),
                LLMResponse(content="done"),
            ]),
            tools=[spy],
        )

        assert agent.chat("go") == "done"

        # the backfill must not duplicate the reply the round already wrote
        assert len(spy.calls) == 1
        replies = [(m["tool_call_id"], m["content"])
                   for m in agent.messages if m.get("role") == "tool"]
        assert replies == [("t1", "executed")]


# ─── 11. M3: context-length rejection collapses once, retries once ──────────

def _bad_request(message: str) -> BadRequestError:
    try:
        import httpx
    except ModuleNotFoundError:
        # openai>=3.14 moved its HTTP layer from httpx to httpx2
        import httpx2 as httpx

    req = httpx.Request("POST", "https://example.invalid/v1/chat/completions")
    return BadRequestError(message, response=httpx.Response(400, request=req), body=None)


class CountingLLM:
    """Minimal LLM double: counts calls, raises failures, then returns a reply."""

    model = "fake"
    total_prompt_tokens = 0
    total_completion_tokens = 0
    estimated_cost = None

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def chat(self, messages, tools=None, on_token=None, on_reasoning=None):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class TestContextOverflowRecovery:
    """`maybe_compress` only measured `self.messages`, so the system prompt and
    tool schemas could push a 90%-full request over the window. When the
    provider rejects it anyway, the loop must collapse once and retry once."""

    @staticmethod
    def _agent(llm, history: int = 6, **kwargs) -> Agent:
        agent = Agent(llm=llm, tools=[], max_context_tokens=200_000, **kwargs)
        agent.messages = [{"role": "user", "content": f"m{i}"} for i in range(history)]
        return agent

    def test_context_overflow_triggers_force_collapse_and_retries(self):
        llm = CountingLLM(
            _bad_request("This model's maximum context length is 8192 tokens"),
            LLMResponse(content="recovered"),
        )
        agent = self._agent(llm)

        with mock.patch.object(agent.context, "_get_summary", return_value="summary"), \
                mock.patch.object(agent.context, "_hard_collapse",
                                  wraps=agent.context._hard_collapse) as collapse:
            answer = agent.chat("go")

        assert answer == "recovered"
        assert collapse.call_count == 1
        assert llm.calls == 2

    def test_context_overflow_retries_exactly_once(self):
        # the second message is upper-case: the predicate must be case-insensitive
        llm = CountingLLM(
            _bad_request("maximum context length exceeded"),
            _bad_request("MAXIMUM CONTEXT LENGTH exceeded again"),
        )
        agent = self._agent(llm)

        with mock.patch.object(agent.context, "_get_summary", return_value="summary"), \
                mock.patch.object(agent.context, "_hard_collapse",
                                  wraps=agent.context._hard_collapse) as collapse, \
                pytest.raises(BadRequestError):
            agent.chat("go")

        assert collapse.call_count == 1
        assert llm.calls == 2

    def test_non_context_400_is_not_retried_at_agent_level(self):
        llm = CountingLLM(_bad_request("Unsupported parameter: 'response_format'"))
        agent = self._agent(llm)

        with mock.patch.object(agent.context, "_hard_collapse",
                               wraps=agent.context._hard_collapse) as collapse, \
                pytest.raises(BadRequestError):
            agent.chat("go")

        assert collapse.call_count == 0
        assert llm.calls == 1

    def test_short_history_overflow_is_not_collapsed(self):
        # 2 stored + the new user message = 3: collapsing cannot free enough
        llm = CountingLLM(_bad_request("maximum context length is 8192 tokens"))
        agent = self._agent(llm, history=2)

        with mock.patch.object(agent.context, "_hard_collapse",
                               wraps=agent.context._hard_collapse) as collapse, \
                pytest.raises(BadRequestError):
            agent.chat("go")

        assert collapse.call_count == 0
        assert llm.calls == 1

    def test_compression_counts_fixed_overhead(self):
        ctx = ContextManager(max_tokens=2000)
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "tool", "tool_call_id": "t1",
             "content": "\n".join(["y" * 200] * 10)},
        ]

        # ~591 tokens on its own: under the 1000-token snip threshold
        assert ctx.maybe_compress(messages, None) is False
        # the fixed request overhead pushes it over
        assert ctx.maybe_compress(messages, None, fixed_tokens=500) is True
        assert "snipped" in messages[1]["content"]

    def test_agent_counts_fixed_overhead_in_compression(self):
        agent = Agent(llm=ScriptedLLM([LLMResponse(content="ok")]), tools=[SpyTool()])

        with mock.patch.object(agent.context, "maybe_compress",
                               return_value=False) as compress:
            assert agent.chat("hi") == "ok"

        assert compress.call_count == 1
        assert compress.call_args.kwargs["fixed_tokens"] > 0

    def test_estimated_prompt_counts_fixed_overhead_once(self, tmp_path):
        """`estimated_prompt` must be the history plus the fixed overhead once.

        `_full_messages()` already carries the system message and the fixed
        overhead counts it again, so adding both reported the system prompt
        twice (M + 2F + T instead of M + F + T) - and that inflated number was
        what got compared against the provider's prompt_tokens.
        """
        trace_path = tmp_path / "llm-rounds.jsonl"
        llm = ScriptedLLM([LLMResponse(content="ok")])
        agent = Agent(llm=llm, tools=[SpyTool()], tracer=JsonlTracer(trace_path))

        # the estimate is taken before the reply joins the history, so pin
        # what the round actually saw, not the post-chat list
        seen: list[tuple[list[dict], list[dict]]] = []
        original_chat = llm.chat

        def recording_chat(messages, tools=None, on_token=None, on_reasoning=None):
            seen.append((list(agent.messages), agent._full_messages()))
            return original_chat(messages, tools=tools, on_token=on_token,
                                 on_reasoning=on_reasoning)

        llm.chat = recording_chat
        assert agent.chat("hi") == "ok"

        records = [json.loads(line) for line in
                   trace_path.read_text(encoding="utf-8").splitlines()]
        [round_span] = [r for r in records if r["name"] == "llm.round"]
        estimated = round_span["attrs"]["estimated_prompt"]
        agent.tracer.close()

        history, full = seen[0]
        fixed = agent._fixed_prompt_tokens()
        assert estimated == estimate_tokens(history) + fixed

        # the shape this test forbids: `full` already holds the system message,
        # so the old formula overshot by exactly one system prompt
        double_counted = estimate_tokens(full) + fixed
        assert double_counted != estimated
        system_only = [{"role": "system", "content": full[0]["content"]}]
        assert double_counted - estimated == estimate_tokens(system_only)

    def test_fixed_overhead_ignores_history(self):
        """A growing history must never move the fixed part.

        `fixed_tokens` is added on top of the history by `maybe_compress`; if
        the helper ever absorbed history tokens, every request would be counted
        twice and compression would fire early.
        """
        agent = Agent(llm=ScriptedLLM([LLMResponse(content="ok")]), tools=[SpyTool()])
        before = agent._fixed_prompt_tokens()
        agent.messages.extend(
            {"role": "user", "content": "x" * 4000} for _ in range(3))

        assert agent._fixed_prompt_tokens() == before

        with mock.patch.object(agent.context, "maybe_compress",
                               return_value=False) as compress:
            assert agent.chat("hi") == "ok"

        # the term handed to compression is the fixed part alone, never the
        # history it is added to
        assert compress.call_count == 1
        assert compress.call_args.kwargs["fixed_tokens"] == before


# ─── 12. M4: tool results are clamped and redacted before history ───────────

class FixedOutputTool(Tool):
    """A tool that returns one fixed string, so tests can size the result."""

    description = "returns a fixed string"
    parameters: ClassVar[dict] = {"type": "object", "properties": {}}

    def __init__(self, name: str, output: str):
        self.name = name
        self.output = output

    def execute(self) -> str:
        return self.output


class McpShapedTool(Tool):
    """MCP tools take **kwargs, which binds anything, including {}."""

    name = "mcp__fake__big"
    description = "returns a fixed large string"
    parameters: ClassVar[dict] = {"type": "object", "properties": {}}

    def execute(self, **kwargs) -> str:
        return "m" * 100_000


def _tool_replies(messages: list[dict]) -> list[str]:
    return [m["content"] for m in messages if m.get("role") == "tool"]


class TestToolResultGovernance:
    """Tool output used to enter the history verbatim: read_file and MCP had no
    size cap at all, and no path redacted secrets. The loop exit must clamp and
    redact every result, whichever tool produced it."""

    def test_long_result_is_clamped_before_history(self):
        agent = Agent(
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[ToolCall(id="t1", name="big", arguments={})]),
                LLMResponse(content="done"),
            ]),
            tools=[FixedOutputTool("big", "x" * 100_000)],
        )

        assert agent.chat("go") == "done"

        [reply] = _tool_replies(agent.messages)
        assert len(reply) <= 15_200
        assert "truncated:" in reply

    def test_clamp_preserves_head_and_tail(self):
        agent = Agent(
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[ToolCall(id="t1", name="big", arguments={})]),
                LLMResponse(content="done"),
            ]),
            tools=[FixedOutputTool("big", "HEAD\n" + "x" * 100_000 + "\nTAIL")],
        )

        assert agent.chat("go") == "done"

        [reply] = _tool_replies(agent.messages)
        assert reply.startswith("HEAD")
        assert reply.endswith("TAIL")
        assert "truncated:" in reply

    def test_secret_is_redacted_in_tool_message(self):
        openai_key = "sk-proj-abcdefghijklmnopqrstuvwxyz0123456789"
        bearer = "Bearer abcdefghij"
        aws_key = "AKIAABCDEFGHIJKLMNOP"
        private_key = (
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIEsecretmaterialthatmustnotreachdisk\n"
            "-----END RSA PRIVATE KEY-----"
        )
        output = f"{openai_key}\n{bearer}\n{aws_key}\n{private_key}"
        agent = Agent(
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[ToolCall(id="t1", name="secrets", arguments={})]),
                LLMResponse(content="done"),
            ]),
            tools=[FixedOutputTool("secrets", output)],
        )

        assert agent.chat("go") == "done"

        [reply] = _tool_replies(agent.messages)
        for raw in (openai_key, "abcdefghij", aws_key, "MIIEsecretmaterialthatmustnotreachdisk"):
            assert raw not in reply
        assert reply.count("[REDACTED") == 4

    def test_read_file_rejects_oversized_file(self, tmp_path, monkeypatch):
        from corecoder.tools.read import ReadFileTool

        monkeypatch.setattr("corecoder.tools.read._MAX_FILE_BYTES", 1024, raising=False)
        path = tmp_path / "big.txt"
        path.write_bytes(b"x" * 2048)

        result = ReadFileTool().execute(file_path=str(path))

        assert "Error: file too large" in result

    def test_mcp_tool_result_is_clamped(self):
        agent = Agent(
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[ToolCall(
                    id="t1", name="mcp__fake__big", arguments={},
                )]),
                LLMResponse(content="done"),
            ]),
            tools=[McpShapedTool()],
        )

        assert agent.chat("go") == "done"

        [reply] = _tool_replies(agent.messages)
        assert len(reply) <= 15_200
        assert "truncated:" in reply

    def test_parallel_results_are_clamped_independently(self):
        agent = Agent(
            llm=ScriptedLLM([
                LLMResponse(tool_calls=[
                    ToolCall(id="t1", name="big", arguments={}),
                    ToolCall(id="t2", name="big", arguments={}),
                ]),
                LLMResponse(content="done"),
            ]),
            tools=[FixedOutputTool("big", "x" * 100_000)],
        )

        assert agent.chat("go") == "done"

        replies = _tool_replies(agent.messages)
        assert len(replies) == 2
        for reply in replies:
            assert len(reply) <= 15_200
            assert "truncated:" in reply
