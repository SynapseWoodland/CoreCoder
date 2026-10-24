"""TDD tests for item 5: rejected arguments show both ends of the raw JSON.

A truncated call keeps its beginning and its end; showing only the first 200
characters hides the part that usually explains the cut (an unterminated
string, a dangling brace). The refusal must stay compact for the model.
"""

from types import SimpleNamespace
from typing import ClassVar

from corecoder import observability
from corecoder.agent import Agent
from corecoder.demo import ScriptedLLM
from corecoder.llm import LLMResponse, ToolCall
from corecoder.permissions import Permission
from corecoder.tools.base import Tool
from corecoder.tracing import JsonlTracer


class EchoTool(Tool):
    name = "echo"
    description = "Echo text."
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }

    def __init__(self):
        self.calls = []

    def execute(self, text=""):
        self.calls.append(text)
        return text


def bad_call(raw: str) -> ToolCall:
    return ToolCall(id="bad-1", name="echo", arguments={},
                    raw_arguments=raw, arguments_error=raw)


def run_chat(turn, tool=None, tracer=None, max_rounds=3):
    tool = tool or EchoTool()
    agent = Agent(
        llm=ScriptedLLM([turn, LLMResponse(content="done")]),
        tools=[tool],
        permission=Permission(allow_all=True),
        max_rounds=max_rounds,
        tracer=tracer,
    )
    reply = agent.chat("go")
    return agent, tool, reply


def tool_reply(agent) -> str:
    return next(m["content"] for m in agent.messages if m.get("role") == "tool")


def pairs(messages) -> bool:
    calls = sorted(tc["id"] for m in messages if m.get("role") == "assistant"
                   for tc in (m.get("tool_calls") or []))
    replies = sorted(m["tool_call_id"] for m in messages if m.get("role") == "tool")
    return calls == replies


def test_long_raw_keeps_head_and_tail():
    head, mid, tail = "H" * 120, "M" * 200, "T" * 80
    raw = head + mid + tail
    assert len(raw) == 400

    agent = Agent(llm=ScriptedLLM([]), tools=[])
    shown = agent._bad_arguments(bad_call(raw))

    assert head in shown
    assert tail in shown
    assert mid not in shown
    assert "(400 chars total)" in shown
    assert "not a valid JSON object" in shown


def test_short_raw_is_shown_whole():
    raw = '{"text": "hi"}'
    agent = Agent(llm=ScriptedLLM([]), tools=[])

    shown = agent._bad_arguments(bad_call(raw))

    assert repr(raw) in shown


def test_arguments_error_only_still_produces_a_refusal():
    tc = SimpleNamespace(name="echo", arguments={},
                         arguments_error='{"text": "cut')
    agent = Agent(llm=ScriptedLLM([]), tools=[])

    shown, decision = agent._decide(tc)

    assert decision == "bad_arguments"
    assert shown.startswith("Error: bad arguments for echo:")
    assert repr('{"text": "cut') in shown


def test_chat_refuses_with_diagnostics_and_keeps_history_valid():
    head, mid, tail = "H" * 120, "M" * 200, "T" * 80
    agent, tool, reply = run_chat(
        LLMResponse(content="", tool_calls=[bad_call(head + mid + tail)]))

    assert reply == "done"
    assert tool.calls == []
    text = tool_reply(agent)
    assert head in text and tail in text
    assert "(400 chars total)" in text
    assert pairs(agent.messages)


def test_raw_arguments_never_reach_audit_or_trace(tmp_path):
    secret = "sk-abcdefghijklmnop1234"
    raw = '{"text": "' + secret + " " + "A" * 300 + ' TAILMARK"}'
    audit = tmp_path / "audit.log"
    trace_file = tmp_path / "trace.jsonl"
    tracer = JsonlTracer(trace_file, capture_content=True)

    observability.configure_audit(file=audit, level="INFO")
    try:
        import os
        old = os.environ.get("CORECODER_AUDIT")
        os.environ["CORECODER_AUDIT"] = "1"
        try:
            agent, tool, _ = run_chat(
                LLMResponse(content="", tool_calls=[bad_call(raw)]), tracer=tracer)
        finally:
            if old is None:
                os.environ.pop("CORECODER_AUDIT", None)
            else:
                os.environ["CORECODER_AUDIT"] = old
    finally:
        observability.reset_audit()

    assert secret in tool_reply(agent)  # the model needs to see what it sent
    assert tool.calls == []

    audit_text = audit.read_text(encoding="utf-8")
    assert "tool_result" in audit_text
    assert secret not in audit_text
    assert "TAILMARK" not in audit_text

    tracer.close()
    trace_text = trace_file.read_text(encoding="utf-8")
    assert "tool.echo" in trace_text
    assert secret not in trace_text
    assert "TAILMARK" not in trace_text
