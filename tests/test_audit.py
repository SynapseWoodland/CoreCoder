"""TDD tests for the audit layer (item 1).

The audit log must be usable with a single switch (`CORECODER_AUDIT=1`),
independent of the general `corecoder` log level, and must never change agent
behaviour: every write failure degrades to silence, and no tool argument,
result or user input is recorded.
"""

import io
import json
import logging
import os
import sys
import time
from types import SimpleNamespace
from typing import ClassVar
from unittest import mock

import pytest
from openai import APIConnectionError, BadRequestError

from corecoder import audit
from corecoder import session as session_module
from corecoder.agent import Agent, TruncatedOutput
from corecoder.context import ContextManager
from corecoder.demo import ScriptedLLM
from corecoder.llm import LLM, LiteLLM, LLMResponse, ToolCall
from corecoder.permissions import Permission
from corecoder.session import load_session, load_session_record, save_session
from corecoder.tools.base import Tool, ToolRefused
from corecoder.tools.bash import BashTool
from corecoder.tracing import JsonlTracer, NullTracer


@pytest.fixture(autouse=True)
def audit_env(monkeypatch, tmp_path):
    monkeypatch.setenv("CORECODER_AUDIT", "1")
    monkeypatch.setenv("CORECODER_AUDIT_FILE", str(tmp_path / "audit.log"))
    monkeypatch.delenv("CORECODER_AUDIT_STREAM", raising=False)
    monkeypatch.delenv("CORECODER_AUDIT_MAX_MB", raising=False)
    audit.reset_audit()
    yield tmp_path
    audit.reset_audit()


# ─── helpers ────────────────────────────────────────────────────────────────

class SpyTool(Tool):
    name = "spy"
    description = "records execution"
    parameters: ClassVar[dict] = {"type": "object", "properties": {}}

    def __init__(self, result="executed", exc=None, delay=0.0):
        self.calls: list = []
        self.result = result
        self.exc = exc
        self.delay = delay

    def execute(self) -> str:
        self.calls.append(1)
        if self.delay:
            time.sleep(self.delay)
        if self.exc is not None:
            raise self.exc
        return self.result


class WriterTool(SpyTool):
    name = "writer"


class Hooks:
    def __init__(self, pre=None):
        self.pre_result = pre
        self.pre_calls: list = []
        self.post_calls: list = []

    def run_pre(self, name, arguments):
        self.pre_calls.append((name, arguments))
        return self.pre_result

    def run_post(self, name, arguments, result):
        self.post_calls.append((name, result))


def _tc(name="spy", call_id="c1", arguments=None, raw="", error=""):
    return ToolCall(
        id=call_id,
        name=name,
        arguments=arguments or {},
        raw_arguments=raw,
        arguments_error=error,
    )


def _turn(*tool_calls, content=""):
    return LLMResponse(content=content, tool_calls=list(tool_calls))


def _agent(tools, turns, **kwargs):
    return Agent(llm=ScriptedLLM(turns), tools=tools, **kwargs)


def _capture():
    stream = io.StringIO()
    audit.configure_audit(stream=stream)
    return stream


def _lines(stream, event=None):
    lines = [ln for ln in stream.getvalue().splitlines() if ln.startswith("event=")]
    if event is None:
        return lines
    return [ln for ln in lines if ln.startswith(f"event={event} ")]


def _fields(line):
    return dict(token.split("=", 1) for token in line.split() if "=" in token)


def _bad_request(message):
    try:
        import httpx
    except ModuleNotFoundError:
        import httpx2 as httpx
    req = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    return BadRequestError(message, response=httpx.Response(400, request=req), body=None)


def _connection_error():
    try:
        import httpx
    except ModuleNotFoundError:
        import httpx2 as httpx
    return APIConnectionError(request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions"))


def _chunk(content):
    return SimpleNamespace(
        usage=None,
        choices=[SimpleNamespace(delta=SimpleNamespace(
            content=content, tool_calls=None, reasoning_content=None))],
    )


class _DyingStream:
    def __init__(self, exc):
        self._exc = exc
        self._yielded = False

    def __iter__(self):
        return self

    def __next__(self):
        if not self._yielded:
            self._yielded = True
            return _chunk("part")
        raise self._exc


# ─── 1. the switch is enough ───────────────────────────────────────────────

def test_audit_events_require_only_the_audit_switch(monkeypatch):
    monkeypatch.delenv("CORECODER_LOG_LEVEL", raising=False)
    out = _capture()
    tool = SpyTool()
    agent = _agent([tool], [_turn(_tc()), _turn(content="done")])

    agent.chat("hi")

    decisions = _lines(out, "tool_decision")
    results = _lines(out, "tool_result")
    assert len(decisions) == 1 and _fields(decisions[0])["decision"] == "execute"
    assert len(results) == 1
    fields = _fields(results[0])
    assert fields["effective_decision"] == "execute"
    assert fields["outcome"] == "ok"
    assert fields["duration_ms"] != ""

    before = out.getvalue()
    monkeypatch.setenv("CORECODER_AUDIT", "0")
    audit.log_event("probe")
    assert out.getvalue() == before


def test_audit_switch_accepts_the_spellings_tracing_also_accepts(monkeypatch):
    """Both switches read the same helper, so their vocabularies cannot drift."""
    for value in ("1", "true", "TRUE", "yes", "on", " On "):
        monkeypatch.setenv("CORECODER_AUDIT", value)
        assert audit.audit_enabled() is True
    for value in ("", "0", "false", "no", "enabled", "2"):
        monkeypatch.setenv("CORECODER_AUDIT", value)
        assert audit.audit_enabled() is False


# ─── 2. decision vocabulary and short-circuit order ─────────────────────────

class TestDecisions:
    def test_bad_arguments_skips_hooks_and_consent(self):
        hook = Hooks(pre="blocked")
        consent = mock.Mock(return_value=None)
        agent = _agent([SpyTool()], [], hooks=hook, permission=Permission(ask=consent))
        tc = _tc(error="{'cut")

        result, decision = agent._decide(tc)

        assert decision == "bad_arguments"
        assert "not a valid JSON object" in result
        assert hook.pre_calls == []
        consent.assert_not_called()

    def test_pre_hook_blocks_before_consent(self):
        hook = Hooks(pre="blocked by hook")
        consent = mock.Mock(return_value=None)
        agent = _agent([WriterTool()], [], hooks=hook, permission=Permission(ask=consent))

        result, decision = agent._decide(_tc("writer"))

        assert (result, decision) == ("blocked by hook", "pre_hook")
        consent.assert_not_called()

    def test_plan_mode_blocks_mutating_tools(self):
        agent = _agent([WriterTool()], [])
        agent.plan_mode = True

        result, decision = agent._decide(_tc("writer"))

        assert decision == "plan_mode"
        assert "Plan mode is on" in result

    def test_denied_is_distinct_from_plan_mode(self):
        agent = _agent([WriterTool()], [], permission=Permission(ask=lambda n, a: "deny"))

        result, decision = agent._decide(_tc("writer"))

        assert decision == "denied"
        assert "Permission denied" in result

    def test_execute_when_every_gate_passes(self):
        agent = _agent([WriterTool()], [], permission=Permission(ask=lambda n, a: "once"))

        assert agent._decide(_tc("writer")) == (None, "execute")


def test_plan_mode_rule_is_implemented_once():
    agent = _agent([WriterTool()], [])
    agent.plan_mode = True
    original = agent._plan_mode_block
    agent._plan_mode_block = mock.Mock(wraps=original)

    agent._decide(_tc("writer"))
    assert agent._plan_mode_block.call_count == 1

    assert agent._permit(_tc("writer")) is not None
    assert agent._plan_mode_block.call_count == 2


# ─── 3. tool outcomes ──────────────────────────────────────────────────────

def test_tool_decision_and_result_are_logged_per_call():
    out = _capture()
    agent = _agent([SpyTool()], [_turn(_tc(call_id="abc")), _turn(content="done")])

    agent.chat("hi")

    decision = _fields(_lines(out, "tool_decision")[0])
    assert decision["tool"] == "spy"
    assert decision["call_id"] == "abc"
    assert decision["decision"] == "execute"
    assert decision["args_chars"] == "0"
    assert float(decision["decision_ms"]) >= 0  # sub-ms precision, same as duration_ms

    result = _fields(_lines(out, "tool_result")[0])
    assert result["call_id"] == "abc"
    assert result["chars_in"] == "8"
    assert result["chars_out"] == "8"
    assert result["truncated"] == "False"
    assert result["redacted"] == "False"
    assert result["error_text"] == "False"
    assert result["error_type"] == '""'  # empty values are JSON-quoted, not dropped


def test_slowest_tool_is_identifiable_and_round_is_omitted_when_unknown():
    slow = SpyTool(delay=0.12)
    fast = SpyTool()
    slow.name = "slow"
    fast.name = "fast"
    agent = _agent([slow, fast], [])
    out = _capture()
    agent._execute_round(SimpleNamespace(tool_calls=[_tc("slow", "c1"), _tc("fast", "c2")]))

    by_tool = {_fields(ln)["tool"]: _fields(ln) for ln in _lines(out, "tool_result")}
    assert float(by_tool["slow"]["duration_ms"]) >= 100
    assert float(by_tool["slow"]["duration_ms"]) > float(by_tool["fast"]["duration_ms"])
    for line in _lines(out, "tool_result") + _lines(out, "tool_decision"):
        assert " round=" not in line


def test_execute_error_is_distinguished_from_error_text():
    boom = SpyTool(exc=ValueError("kaboom"))
    boom.name = "boom"
    agent = _agent([boom], [])
    out = _capture()

    agent._execute_round(SimpleNamespace(tool_calls=[_tc("boom", "c1")]))

    result = _fields(_lines(out, "tool_result")[0])
    assert result["effective_decision"] == "execute"
    assert result["outcome"] == "error"
    assert result["error_type"] == "ValueError"
    assert result["error_text"] == "False"

    missing = SpyTool(result="Error: no such file")
    missing.name = "missing"
    agent2 = _agent([missing], [])
    out2 = _capture()
    agent2._execute_round(SimpleNamespace(tool_calls=[_tc("missing", "c2")]))

    result = _fields(_lines(out2, "tool_result")[0])
    assert result["outcome"] == "ok"
    assert result["error_text"] == "True"


def test_refused_denied_and_plan_mode_are_skipped():
    bash = BashTool()
    direct = bash.execute(command="rm -rf /")
    assert isinstance(direct, ToolRefused) and "Blocked" in direct

    out = _capture()
    agent = _agent([bash], [])
    agent._execute_round(SimpleNamespace(tool_calls=[
        _tc("bash", "c1", {"command": "rm -rf /"})]))
    fields = _fields(_lines(out, "tool_result")[0])
    assert fields["effective_decision"] == "tool_refused"
    assert fields["outcome"] == "skipped"

    denied = _agent([WriterTool()], [], permission=Permission(ask=lambda n, a: "deny"))
    out2 = _capture()
    denied._execute_round(SimpleNamespace(tool_calls=[_tc("writer", "c2")]))
    fields = _fields(_lines(out2, "tool_result")[0])
    assert (fields["effective_decision"], fields["outcome"]) == ("denied", "skipped")

    planned = _agent([WriterTool()], [])
    planned.plan_mode = True
    out3 = _capture()
    planned._execute_round(SimpleNamespace(tool_calls=[_tc("writer", "c3")]))
    fields = _fields(_lines(out3, "tool_result")[0])
    assert (fields["effective_decision"], fields["outcome"]) == ("plan_mode", "skipped")

    hooked = _agent([WriterTool()], [], hooks=Hooks(pre="no"))
    out4 = _capture()
    hooked._execute_round(SimpleNamespace(tool_calls=[_tc("writer", "c4")]))
    fields = _fields(_lines(out4, "tool_result")[0])
    assert (fields["effective_decision"], fields["outcome"]) == ("pre_hook", "skipped")


# ─── 4. backfill, truncation and context events ─────────────────────────────

def test_backfill_is_logged_only_when_it_happens():
    out = _capture()
    agent = _agent([SpyTool()], [_turn(_tc()), _turn(content="done")])
    agent.chat("hi")
    assert _lines(out, "backfill") == []

    class Killer(SpyTool):
        def execute(self):
            raise KeyboardInterrupt

    killer = Killer()
    killer.name = "killer"
    agent2 = _agent([killer], [_turn(_tc("killer", "c9"), content="")])
    out2 = _capture()
    with pytest.raises(KeyboardInterrupt):
        agent2.chat("go")

    backfills = _lines(out2, "backfill")
    assert len(backfills) == 1 and _fields(backfills[0])["count"] == "1"


def test_truncation_and_max_rounds_are_logged():
    agent = _agent([SpyTool()], [_turn(_tc(), content="")], max_rounds=3)
    agent.llm = ScriptedLLM([
        LLMResponse(content="", tool_calls=[_tc()], finish_reason="length"),
        LLMResponse(content="", tool_calls=[_tc(call_id="c2")]),
        LLMResponse(content="done"),
    ])
    out = _capture()

    assert agent.chat("hi") == "done"
    truncated = _lines(out, "output_truncated")
    assert len(truncated) == 1
    fields = _fields(truncated[0])
    assert fields["consecutive"] == "1" and fields["round"] == "1"

    stalled = _agent([SpyTool()], [])
    stalled.llm = ScriptedLLM([
        LLMResponse(content="", tool_calls=[_tc(call_id=f"c{i}")])
        for i in range(2)
    ])
    stalled.max_rounds = 2
    out2 = _capture()
    assert "maximum tool-call rounds" in stalled.chat("hi")
    assert _fields(_lines(out2, "max_rounds")[0])["rounds"] == "2"


def test_context_events_are_logged():
    out = _capture()
    ctx = ContextManager(max_tokens=100)
    messages = [{"role": "tool", "content": "\n".join(f"line {i}" for i in range(600))}]
    ctx.maybe_compress(messages)

    compressed = _lines(out, "context_compressed")
    assert len(compressed) == 1
    assert _fields(compressed[0])["layer"] == "snip"

    class FailingLLM:
        def chat(self, **kwargs):
            raise RuntimeError("summary boom")

    out2 = _capture()
    ctx._get_summary([{"role": "user", "content": "hi"}], FailingLLM())
    fallback = _lines(out2, "context_summary_fallback")
    assert len(fallback) == 1
    assert _fields(fallback[0])["error_type"] == "RuntimeError"


def test_context_overflow_recovery_is_logged():
    agent = _agent([SpyTool()], [])

    class OverflowLLM:
        def __init__(self):
            self.calls = 0

        def chat(self, messages, tools=None, on_token=None, on_reasoning=None):
            self.calls += 1
            if self.calls == 1:
                raise _bad_request("maximum context length exceeded")
            return LLMResponse(content="done")

    agent.llm = OverflowLLM()
    agent.messages = [{"role": "user", "content": "x" * 5000}] * 4
    out = _capture()

    assert agent.chat("more") == "done"
    events = _lines(out, "context_overflow")
    assert len(events) == 1
    assert _fields(events[0])["error_type"] == "BadRequestError"


def test_empty_answer_is_recorded():
    """B2: a round that was cut short and produced no text must say so.

    A provider that clips an over-long request silently never raises, so this
    path ended `outcome=answer` with an empty body and no event at all: the
    gate below only fires for truncated rounds that carry tool calls.
    """
    agent = _agent([SpyTool()], [])
    agent.llm = ScriptedLLM([LLMResponse(content="", finish_reason="length")])
    out = _capture()

    assert agent.chat("go") == ""
    events = _lines(out, "empty_response")
    assert len(events) == 1
    fields = _fields(events[0])
    assert fields["round"] == "1"
    assert fields["finish_reason"] == "length"
    assert fields["completion_tokens"] == "0"
    # nothing was cut from a tool call, so the M2 gate must stay quiet
    assert _lines(out, "output_truncated") == []
    summary = _fields(_lines(out, "run_summary")[0])
    assert summary["outcome"] == "answer"
    assert summary["empty"] == "1"


def test_silent_clip_is_recorded():
    """B2: reported usage far below our own estimate means the request was cut.

    The re-check measured an estimate of 1,457,607 against a reported 721,600
    prompt tokens with the model still answering normally: the window was
    clipped upstream and nothing in either stream named it.
    """
    agent = _agent([SpyTool()], [])
    agent.llm = ScriptedLLM([
        LLMResponse(content="short answer", finish_reason="stop",
                    prompt_tokens=200, completion_tokens=5, usage_reported=True),
    ])
    agent.messages = [{"role": "user", "content": "y" * 20000} for _ in range(4)]
    out = _capture()

    assert agent.chat("more") == "short answer"
    events = _lines(out, "context_suspected_clip")
    assert len(events) == 1
    fields = _fields(events[0])
    assert fields["prompt_tokens"] == "200"
    # the reference is the very number the round's own line reported
    assert fields["estimated_prompt"] == _fields(_lines(out, "llm_call")[0])["estimated_prompt"]
    assert int(fields["estimated_prompt"]) > 200
    assert float(fields["ratio"]) < 0.6


def test_normal_answer_stays_quiet():
    """B2 must not turn every short reply into an incident.

    A stopped round with text and usage that matches the estimate reports
    nothing: an empty answer is marked on the summary, a clip is not guessed.
    """
    agent = _agent([SpyTool()], [])
    agent.llm = ScriptedLLM([
        LLMResponse(content="fine", finish_reason="stop", prompt_tokens=100_000,
                    completion_tokens=8, usage_reported=True),
    ])
    out = _capture()

    assert agent.chat("hi") == "fine"
    assert _lines(out, "empty_response") == []
    assert _lines(out, "context_suspected_clip") == []
    assert "empty" not in _fields(_lines(out, "run_summary")[0])


# ─── 5. provider fallbacks and retries ─────────────────────────────────────

def test_provider_param_fallbacks_are_logged():
    out = _capture()
    llm = LLM(model="gpt-5", api_key="sk-test", max_tokens=1024)
    llm.client = mock.Mock()
    llm.client.chat.completions.create.side_effect = [
        _bad_request("Unsupported parameter: 'max_tokens' is not supported."),
        [_chunk("ok")],
    ]
    assert llm.chat(messages=[{"role": "user", "content": "hi"}]).content == "ok"
    fallback = _lines(out, "provider_fallback")
    assert _fields(fallback[0])["kind"] == "max_completion_tokens"

    out2 = _capture()
    llm2 = LLM(model="gpt-5", api_key="sk-test", max_tokens=1024)
    llm2.client = mock.Mock()
    llm2.client.chat.completions.create.side_effect = [
        _bad_request("Bad request"),
        [_chunk("ok")],
    ]
    assert llm2.chat(messages=[{"role": "user", "content": "hi"}]).content == "ok"
    fallback = _lines(out2, "provider_fallback")
    # B3: a nameless 400 keeps a single fallback, but the kind now says honestly
    # what was dropped and the error surface is recorded instead of guessed
    assert _fields(fallback[0])["kind"] == "unnamed_param_fallback"
    assert _fields(fallback[0])["error_type"] == "BadRequestError"


def test_mid_stream_retry_is_logged(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda *_: None)
    out = _capture()
    llm = LLM(model="gpt-4o", api_key="sk-test")
    llm.client = mock.Mock()
    llm.client.chat.completions.create.side_effect = [
        _DyingStream(_connection_error()),
        [_chunk("full")],
    ]

    assert llm.chat(messages=[{"role": "user", "content": "hi"}]).content == "full"
    retries = _lines(out, "llm_retry")
    assert len(retries) == 1
    assert _fields(retries[0])["stage"] == "stream"


def test_litellm_retry_is_logged(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda *_: None)
    out = _capture()
    attempts = {"n": 0}

    def completion(**params):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("rate_limit exceeded")
        return "stream"

    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(completion=completion))
    llm = LiteLLM(model="x")

    assert llm._call_with_retry({"model": "x"}) == "stream"
    retries = _lines(out, "llm_retry")
    assert len(retries) == 1
    assert _fields(retries[0])["stage"] == "create"


# ─── 6. configure_audit behaviour ──────────────────────────────────────────

def test_configure_audit_is_idempotent_and_falls_back_to_info():
    stream = io.StringIO()
    audit.configure_audit(stream=stream, level="LOUD")
    audit.configure_audit(stream=stream)

    handlers = [h for h in audit.audit_logger.handlers if getattr(h, "_cc_audit", False)]
    assert len(handlers) == 1
    assert handlers[0].level == logging.INFO


def test_audit_stream_must_be_explicitly_opted_in(monkeypatch):
    monkeypatch.delenv("CORECODER_AUDIT_FILE")
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stderr", buf)
    monkeypatch.setenv("CORECODER_AUDIT_STREAM", "stderr")

    audit.configure_audit()
    audit.log_event("probe")

    assert "event=probe" in buf.getvalue()


def test_default_file_name_is_per_process():
    path = audit._default_audit_path()
    assert path.name == f"audit.{os.getpid()}.log"
    assert path.parent.name == ".corecoder"


def test_unwritable_target_warns_once_and_never_raises(monkeypatch, tmp_path, caplog):
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    monkeypatch.setenv("CORECODER_AUDIT_FILE", str(blocker / "audit.log"))

    with caplog.at_level(logging.WARNING, logger="corecoder"):
        audit.configure_audit()

    assert "event=audit_disabled" in caplog.text
    audit.log_event("probe")  # no handler, no raise


def test_oversized_log_rotates_at_startup(monkeypatch, tmp_path):
    target = tmp_path / "audit.log"
    target.write_text("x" * (1024 * 1024 + 10), encoding="utf-8")
    monkeypatch.setenv("CORECODER_AUDIT_FILE", str(target))
    monkeypatch.setenv("CORECODER_AUDIT_MAX_MB", "1")

    audit.configure_audit()

    assert (tmp_path / "audit.log.1").exists()
    assert target.stat().st_size < 1024 * 1024


def test_one_os_write_per_line(monkeypatch, tmp_path):
    target = tmp_path / "audit.log"
    audit.configure_audit(file=target)
    with mock.patch("corecoder.audit.os.write", return_value=1) as write:
        audit.log_event("probe", tool="spy")

    assert write.call_count == 1
    payload = write.call_args[0][1]
    assert b"event=probe tool=spy" in payload and payload.endswith(b"\n")


def test_default_logs_are_pruned_to_ten_but_explicit_paths_are_kept(monkeypatch, tmp_path):
    target = tmp_path / f"audit.{os.getpid()}.log"
    monkeypatch.setattr(audit, "_default_audit_path", lambda: target)
    monkeypatch.delenv("CORECODER_AUDIT_FILE")
    for i in range(12):
        path = tmp_path / f"audit.{1000 + i}.log"
        path.write_text("x", encoding="utf-8")
        os.utime(path, (1000 + i, 1000 + i))

    audit.configure_audit()

    remaining = list(tmp_path.glob("audit.*.log"))
    assert len(remaining) == 10
    assert target in remaining

    explicit_dir = tmp_path / "explicit"
    explicit_dir.mkdir()
    for i in range(12):
        (explicit_dir / f"audit.{2000 + i}.log").write_text("x", encoding="utf-8")
    audit.configure_audit(file=explicit_dir / "audit.other.log")
    assert len(list(explicit_dir.glob("audit.*.log"))) == 13


def test_prune_count_is_configurable_and_a_bad_value_falls_back(monkeypatch, tmp_path):
    """One day of real runs already outgrows the hard-coded cap of ten.

    Ten pid files is under a day of dogfood, so the retention cap has to be a
    setting rather than a literal — otherwise the audit trail cannot serve as
    the second copy of a run history the trace backend already keeps.
    """
    target = tmp_path / f"audit.{os.getpid()}.log"
    monkeypatch.setattr(audit, "_default_audit_path", lambda: target)
    monkeypatch.delenv("CORECODER_AUDIT_FILE", raising=False)

    def seed(count, base=3000):
        for i in range(count):
            path = tmp_path / f"audit.{base + i}.log"
            path.write_text("x", encoding="utf-8")
            os.utime(path, (2000 + i, 2000 + i))

    monkeypatch.setenv("CORECODER_AUDIT_KEEP", "3")
    seed(6)
    audit.configure_audit()
    assert len(list(tmp_path.glob("audit.*.log"))) == 3

    # the env is read per configure and not cached at import: change it and the
    # next call must honour the new cap over the files left behind
    monkeypatch.setenv("CORECODER_AUDIT_KEEP", "5")
    seed(6)
    audit.configure_audit()
    assert len(list(tmp_path.glob("audit.*.log"))) == 5

    # a value that is not a number must not switch retention off altogether
    monkeypatch.setenv("CORECODER_AUDIT_KEEP", "not-a-number")
    seed(15, base=4000)
    audit.configure_audit()
    assert len(list(tmp_path.glob("audit.*.log"))) == 10


def test_an_empty_audit_file_setting_still_prunes_the_pid_logs(monkeypatch, tmp_path):
    """``CORECODER_AUDIT_FILE=`` is the pid-suffixed default, so it keeps the
    default's retention.

    The path is picked by truthiness while retention used to be keyed on
    ``is None``, so an empty setting landed on ``audit.<pid>.log`` and never
    pruned: the per-process files piled up with retention silently off.
    """
    target = tmp_path / f"audit.{os.getpid()}.log"
    monkeypatch.setattr(audit, "_default_audit_path", lambda: target)
    monkeypatch.setenv("CORECODER_AUDIT_FILE", "")
    monkeypatch.setenv("CORECODER_AUDIT_KEEP", "3")
    for i in range(6):
        path = tmp_path / f"audit.{5000 + i}.log"
        path.write_text("x", encoding="utf-8")
        os.utime(path, (3000 + i, 3000 + i))

    audit.configure_audit()
    audit.log_event("probe")

    remaining = list(tmp_path.glob("audit.*.log"))
    assert len(remaining) == 3
    assert target in remaining
    assert "event=probe" in target.read_text(encoding="utf-8")


# ─── 7. degradation and privacy ────────────────────────────────────────────

class _BrokenStream:
    def __init__(self, fail_times=None):
        self.writes = 0
        self.fail_times = fail_times

    def write(self, text):
        self.writes += 1
        if self.fail_times is None or self.writes <= self.fail_times:
            raise OSError("disk full")

    def flush(self):
        pass


def test_runtime_write_failures_warn_once_then_disable(caplog):
    stream = _BrokenStream()
    audit.configure_audit(stream=stream)

    with caplog.at_level(logging.WARNING, logger="corecoder"):
        for i in range(5):
            audit.log_event(f"probe{i}")

    warnings = [r for r in caplog.records if "audit_write_failed" in r.getMessage()]
    assert len(warnings) == 1
    assert stream.writes == 3  # the fourth call never reached the handler


def test_a_successful_write_resets_the_failure_streak(caplog):
    stream = _BrokenStream(fail_times=1)
    audit.configure_audit(stream=stream)

    with caplog.at_level(logging.WARNING, logger="corecoder"):
        for i in range(5):
            audit.log_event(f"probe{i}")

    assert stream.writes == 5
    assert len([r for r in caplog.records if "audit_write_failed" in r.getMessage()]) == 1


def _sink_with_exploding_formatter():
    """Put a raising formatter on the audit sink and return how to undo it.

    Only the sink this module installed: pytest attaches its own capture
    handlers to every logger that does not propagate, so
    ``audit.audit_logger.handlers`` also holds objects that later tests still
    need in working order.
    """
    sink = next(h for h in audit.audit_logger.handlers if getattr(h, "_cc_audit", False))

    class Exploding(logging.Formatter):
        def format(self, record):
            raise RuntimeError("format boom")

    original = sink.formatter
    sink.setFormatter(Exploding())
    return sink, original


def test_log_event_never_raises_even_when_the_formatter_does():
    audit.configure_audit(stream=io.StringIO())
    sink, original = _sink_with_exploding_formatter()

    try:
        audit.log_event("probe")
    finally:
        sink.setFormatter(original)


def test_a_crashing_audit_backend_does_not_replace_keyboardinterrupt():
    audit.configure_audit(stream=io.StringIO())
    sink, original = _sink_with_exploding_formatter()

    class Killer(SpyTool):
        def execute(self):
            raise KeyboardInterrupt

    killer = Killer()
    killer.name = "killer"
    agent = _agent([killer], [_turn(_tc("killer", "c1"))])

    try:
        with pytest.raises(KeyboardInterrupt):
            agent.chat("go")
        replied = {m["tool_call_id"] for m in agent.messages if m.get("role") == "tool"}
        assert replied == {"c1"}
    finally:
        sink.setFormatter(original)


def test_audit_never_records_prompts_arguments_or_results():
    sentinel = "SENTINEL_SECRET_VALUE"
    tool = SpyTool(result=f"output {sentinel}")
    agent = _agent([tool], [
        _turn(_tc()),
        _turn(_tc(call_id="c2", raw="{broken", error=sentinel)),
        _turn(content="done"),
    ])
    out = _capture()

    agent.chat(f"please use {sentinel}")

    assert sentinel not in out.getvalue()
    assert "please use" not in out.getvalue()


# ─── 8. cost and run summary ───────────────────────────────────────────────

def _stream(prompt=10, completion=5, content="done", with_usage=True):
    chunks = [SimpleNamespace(
        usage=None,
        choices=[SimpleNamespace(
            delta=SimpleNamespace(content=content, tool_calls=None, reasoning_content=None),
            finish_reason="stop",
        )],
    )]
    usage = (SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion)
             if with_usage else None)
    chunks.append(SimpleNamespace(usage=usage, choices=[]))
    return chunks


def _real_llm(model="gpt-4o", **stream_kwargs):
    llm = LLM(model=model, api_key="sk-test")
    llm.client = mock.Mock()
    llm.client.chat.completions.create.return_value = _stream(**stream_kwargs)
    return llm


def test_unreported_usage_is_logged_as_unknown_not_zero():
    llm = _real_llm(with_usage=False)
    resp = llm.chat(messages=[{"role": "user", "content": "hi"}])
    assert resp.prompt_tokens == 0 and resp.completion_tokens == 0
    assert resp.usage_reported is False

    out = _capture()
    agent = Agent(llm=llm, tools=[])
    agent.chat("hi")

    call = _fields(_lines(out, "llm_call")[0])
    assert call["prompt_tokens"] == "None"
    assert call["completion_tokens"] == "None"
    assert call["usage_reported"] == "False"
    summary = _fields(_lines(out, "run_summary")[0])
    assert summary["usage_reported"] == "False"
    assert summary["tokens_in"] == "0"


def test_cost_is_reported_for_priced_models_and_none_for_unknown_ones():
    out = _capture()
    Agent(llm=_real_llm(prompt=1000, completion=500), tools=[]).chat("hi")

    call = _fields(_lines(out, "llm_call")[0])
    summary = _fields(_lines(out, "run_summary")[0])
    assert float(call["cost_usd"]) > 0
    assert float(summary["cost_usd"]) > 0
    assert float(summary["cost_usd"]) == pytest.approx(0.0075, abs=1e-6)

    out2 = _capture()
    Agent(llm=_real_llm(model="unknown-model"), tools=[]).chat("hi")
    assert _fields(_lines(out2, "llm_call")[0])["cost_usd"] == "None"
    assert _fields(_lines(out2, "run_summary")[0])["cost_usd"] == "None"


def test_run_summary_covers_answer_truncated_and_interrupted():
    out = _capture()
    Agent(llm=ScriptedLLM([LLMResponse(content="done")]), tools=[]).chat("hi")
    assert _fields(_lines(out, "run_summary")[0])["outcome"] == "answer"

    out2 = _capture()
    truncated = ScriptedLLM([
        LLMResponse(content="", tool_calls=[_tc()], finish_reason="length"),
        LLMResponse(content="", tool_calls=[_tc(call_id="c2")], finish_reason="length"),
    ])
    with pytest.raises(TruncatedOutput):
        Agent(llm=truncated, tools=[SpyTool()]).chat("hi")
    assert _fields(_lines(out2, "run_summary")[0])["outcome"] == "truncated"

    out3 = _capture()

    class Killer(SpyTool):
        def execute(self):
            raise KeyboardInterrupt

    killer = Killer()
    killer.name = "killer"
    with pytest.raises(KeyboardInterrupt):
        Agent(llm=ScriptedLLM([_turn(_tc("killer", "c1"))]), tools=[killer]).chat("go")
    assert _fields(_lines(out3, "run_summary")[0])["outcome"] == "interrupted"


def test_run_summary_never_files_a_crash_as_round_exhaustion():
    """A run that died must not be counted as one that ran out of rounds.

    `_RunStats.outcome` used to default to "max_rounds" and `chat` caught only
    `(Exception, KeyboardInterrupt)`, so a BaseException that is neither (a tool
    calling `sys.exit`, a GeneratorExit) slipped past the relabeling while
    `finally` still wrote the summary: the dashboard reported round exhaustion
    for a crash, with no error type anywhere.
    """
    out = _capture()
    # the cap genuinely runs out: this is the only run allowed to say so
    filler = ScriptedLLM([_turn(_tc(call_id="c1")), _turn(_tc(call_id="c2")),
                          _turn(_tc(call_id="c3"))])
    assert Agent(llm=filler, tools=[SpyTool()], max_rounds=2).chat("go") == \
        "(reached maximum tool-call rounds)"
    exhausted = _fields(_lines(out, "run_summary")[0])
    assert exhausted["outcome"] == "max_rounds"
    # the label must be backed by the warning line, or the two sinks drift apart
    assert len(_lines(out, "max_rounds")) == 1

    out2 = _capture()
    with pytest.raises(SystemExit):
        Agent(llm=ScriptedLLM([_turn(_tc())]), tools=[SpyTool(exc=SystemExit(3))],
              max_rounds=2).chat("go")
    crashed = _fields(_lines(out2, "run_summary")[0])
    assert crashed["outcome"] == "error", crashed["outcome"]
    assert not _lines(out2, "max_rounds"), "a crash is not an exhaustion"


def test_run_summary_names_the_error_that_ended_the_run():
    """`outcome=error` on its own cannot be acted on (3.2 defect 2).

    A provider 400, a crashed UI callback and a tool that dies with `SystemExit`
    all collapse into the same word. The exception type is already known where
    the summary is written, so the audit line must carry it too - not only the
    root span, which the Langfuse side drops anyway.
    """
    out = _capture()

    def boom(name, arguments):
        raise RuntimeError("UI callback crashed")

    with pytest.raises(RuntimeError):
        Agent(llm=ScriptedLLM([_turn(_tc())]), tools=[SpyTool()]).chat(
            "go", on_tool=boom)

    summary = _fields(_lines(out, "run_summary")[0])
    assert summary["outcome"] == "error"
    assert summary["error_type"] == "RuntimeError"

    # a run that ended cleanly must not look like it carried an error; `None` is
    # how this audit line already spells "no value" (see cost_usd)
    out2 = _capture()
    Agent(llm=ScriptedLLM([LLMResponse(content="done")]), tools=[]).chat("hi")
    assert _fields(_lines(out2, "run_summary")[0])["error_type"] == "None"


def test_run_summary_carries_version_and_build(monkeypatch):
    from corecoder import __version__

    monkeypatch.setenv("CORECODER_BUILD", "abc123")
    out = _capture()
    Agent(llm=ScriptedLLM([LLMResponse(content="done")]), tools=[]).chat("hi")

    summary = _fields(_lines(out, "run_summary")[0])
    assert summary["version"] == __version__
    assert summary["build"] == "abc123"


def test_token_totals_come_only_from_reported_rounds():
    tool = SpyTool()
    llm = ScriptedLLM([
        LLMResponse(content="", tool_calls=[_tc()], prompt_tokens=10,
                    completion_tokens=2, usage_reported=True),
        LLMResponse(content="done", prompt_tokens=999, completion_tokens=999,
                    usage_reported=False),
    ])
    out = _capture()
    Agent(llm=llm, tools=[tool]).chat("hi")

    summary = _fields(_lines(out, "run_summary")[0])
    assert summary["tokens_in"] == "10"
    assert summary["tokens_out"] == "2"
    assert summary["usage_reported"] == "False"
    assert summary["rounds"] == "2"
    assert summary["tool_calls"] == "1"
    assert summary["tools_executed"] == "1"


def test_tools_executed_excludes_refusals():
    agent = Agent(llm=ScriptedLLM([
        _turn(_tc("bash", "c1", {"command": "rm -rf /"})),
        LLMResponse(content="done"),
    ]), tools=[BashTool()])
    out = _capture()

    agent.chat("hi")

    summary = _fields(_lines(out, "run_summary")[0])
    assert summary["tool_calls"] == "1"
    assert summary["tools_executed"] == "0"


def test_session_id_is_stable_across_save_and_resume(monkeypatch, tmp_path):
    monkeypatch.setattr(session_module, "SESSIONS_DIR", tmp_path)
    from corecoder.session import new_session_id

    assert new_session_id().startswith("session_")

    out = _capture()
    agent = Agent(llm=ScriptedLLM([LLMResponse(content="done")]), tools=[],
                  session_id="s1")
    agent.chat("hi")
    assert _fields(_lines(out, "run_summary")[0])["session"] == "s1"

    assert save_session(agent.messages, "gpt-4o", session_id=agent.session_id) == "s1"
    record = load_session_record("s1")
    assert record["id"] == "s1"
    assert load_session("s1") == (record["messages"], "gpt-4o")

    out2 = _capture()
    resumed = Agent(llm=ScriptedLLM([LLMResponse(content="again")]), tools=[],
                    session_id=record["id"])
    resumed.chat("again")
    assert _fields(_lines(out2, "run_summary")[0])["session"] == "s1"


# ─── 9. CLI wiring ─────────────────────────────────────────────────────────

def test_main_configures_logging(monkeypatch):
    from corecoder import cli, demo

    calls = []
    monkeypatch.setattr(cli, "configure_logging", lambda: calls.append(1))
    monkeypatch.setattr(sys, "argv", ["corecoder", "--demo"])
    monkeypatch.setattr(demo, "run_demo", lambda: 0)

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 0
    assert calls


# ─── 10. joining the audit log with the trace ──────────────────────────────

def _trace_records(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def test_audit_lines_name_the_span_that_was_open(tmp_path):
    """One run writes two files. Every audit line must say which span it
    belongs to, or the same work is described twice without ever meeting."""
    trace_path = tmp_path / "trace.jsonl"
    out = _capture()
    agent = _agent([SpyTool()], [_turn(_tc(call_id="c1")), _turn(content="done")],
                   tracer=JsonlTracer(trace_path))

    agent.chat("hi")

    records = _trace_records(trace_path)
    trace_ids = {r["trace_id"] for r in records}
    by_span = {r["span_id"]: r for r in records}
    lines = _lines(out)
    assert lines
    for line in lines:
        fields = _fields(line)
        assert fields["trace_id"] in trace_ids
        assert fields["span_id"] in by_span

    assert by_span[_fields(_lines(out, "run_summary")[0])["span_id"]]["name"] == "agent.chat"
    assert by_span[_fields(_lines(out, "llm_call")[0])["span_id"]]["name"] == "llm.round"

    result = _fields(_lines(out, "tool_result")[0])
    tool_span = by_span[result["span_id"]]
    assert tool_span["name"] == "tool.spy"
    assert tool_span["attrs"]["call_id"] == result["call_id"] == "c1"


def test_parallel_audit_lines_map_to_their_own_spans(tmp_path):
    """A parallel worker cannot open its span on the tracer's thread-local
    stack, so the span is created late; the audit line written for it must
    still name that span, not the root."""
    trace_path = tmp_path / "trace.jsonl"
    out = _capture()
    llm = ScriptedLLM([
        _turn(_tc("spy", "ok"),
              _tc("spy", "bad", raw="{broken", error="expecting ','")),
        _turn(content="done"),
    ])
    agent = Agent(llm=llm, tools=[SpyTool()], tracer=JsonlTracer(trace_path))

    agent.chat("go")

    by_span = {r["span_id"]: r for r in _trace_records(trace_path) if r["type"] == "tool"}
    results = {_fields(line)["call_id"]: _fields(line) for line in _lines(out, "tool_result")}
    assert set(results) == {"ok", "bad"}
    assert {results[c]["outcome"] for c in results} == {"ok", "skipped"}
    assert results["ok"]["span_id"] != results["bad"]["span_id"]
    for call_id, fields in results.items():
        span = by_span[fields["span_id"]]
        assert span["attrs"]["call_id"] == call_id
        assert span["trace_id"] == fields["trace_id"]


def test_audit_lines_carry_no_ids_when_tracing_is_off():
    """Tracing off means there is no span to name. Inventing ids would make a
    broken join look successful, so the fields are simply absent."""
    out = _capture()

    Agent(llm=ScriptedLLM([LLMResponse(content="done")]), tools=[],
          tracer=NullTracer()).chat("hi")

    summary = _fields(_lines(out, "run_summary")[0])
    assert "trace_id" not in summary
    assert "span_id" not in summary


def test_interrupted_tool_call_closes_its_span(tmp_path):
    """A call that never returns skips _log_result, which is what ends the
    span. Left open, the trace loses the call and every later audit line keeps
    naming a span that is already dead."""
    trace_path = tmp_path / "trace.jsonl"
    tracer = JsonlTracer(trace_path)

    class Killer(SpyTool):
        def execute(self):
            raise KeyboardInterrupt

    killer = Killer()
    killer.name = "killer"
    agent = Agent(llm=ScriptedLLM([_turn(_tc("killer", "c1"))]), tools=[killer],
                  tracer=tracer)
    with pytest.raises(KeyboardInterrupt):
        agent.chat("go")

    record = next(r for r in _trace_records(trace_path) if r["name"] == "tool.killer")
    assert "KeyboardInterrupt" in record["error"]

    out = _capture()
    Agent(llm=ScriptedLLM([LLMResponse(content="done")]), tools=[],
          tracer=tracer).chat("hi")
    assert _fields(_lines(out, "run_summary")[0])["span_id"] != record["span_id"]
