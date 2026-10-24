"""TDD tests for the trace layer (item 3).

Tracing is optional infrastructure: the default is off, content is not
captured unless asked for, and every backend failure must degrade to a
no-op without changing the agent's answer.
"""

import json
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import ClassVar

import pytest

from corecoder import tracing
from corecoder.agent import Agent
from corecoder.demo import ScriptedLLM
from corecoder.llm import LLMResponse, ToolCall
from corecoder.permissions import Permission
from corecoder.tools.base import Tool
from corecoder.tracing import (
    JsonlTracer,
    LangfuseTracer,
    NullSpan,
    NullTracer,
    SafeTracer,
    create_tracer,
)


@pytest.fixture(autouse=True)
def trace_env(monkeypatch):
    for key in (
        "CORECODER_TRACE",
        "CORECODER_TRACE_FILE",
        "CORECODER_TRACE_CONTENT",
        "CORECODER_TRACE_MAX_MB",
        "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_SECRET_KEY",
        "LANGFUSE_HOST",
        "LANGFUSE_BASE_URL",
    ):
        monkeypatch.delenv(key, raising=False)


class SpyTool(Tool):
    name = "spy"
    description = "Return a fixed string."
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }

    def __init__(self, result: str = "ok", delay: float = 0.0):
        self.result = result
        self.delay = delay

    def execute(self, text: str = "") -> str:
        if self.delay:
            time.sleep(self.delay)
        return self.result


def rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def scripted(*turns: LLMResponse) -> ScriptedLLM:
    return ScriptedLLM(list(turns))


def one_tool_round(result: str = "ok", delay: float = 0.0):
    tool = SpyTool(result=result, delay=delay)
    llm = scripted(
        LLMResponse(content="", tool_calls=[
            ToolCall(id="c1", name="spy", arguments={"text": "ping"})]),
        LLMResponse(content="done"),
    )
    return tool, llm


def make_agent(tracer, tool=None, llm=None) -> Agent:
    tool = tool or SpyTool()
    llm = llm or one_tool_round()[1]
    return Agent(llm=llm, tools=[tool], permission=Permission(allow_all=True),
                 tracer=tracer, max_rounds=6)


# ─── 1. JSONL tree ─────────────────────────────────────────────────────────

def test_jsonl_tree_links_root_generation_and_tool(tmp_path):
    path = tmp_path / "trace.jsonl"
    tracer = JsonlTracer(path)
    tool, llm = one_tool_round(delay=0.002)
    agent = make_agent(tracer, tool=tool, llm=llm)

    agent.chat("go")
    with pytest.raises(RuntimeError):
        agent.chat("again")  # runs out of scripted turns after the LLM call

    records = rows(path)
    roots = [r for r in records if r["name"] == "agent.chat"]
    assert len(roots) == 2
    first_root, second_root = roots
    assert first_root["parent_id"] is None
    assert first_root["session_id"] == agent.session_id
    assert first_root["attrs"]["model"] == "scripted-demo"
    assert first_root["attrs"]["max_rounds"] == 6
    assert first_root["trace_id"] != second_root["trace_id"]
    assert first_root["end"] >= first_root["start"]

    first_trace = [r for r in records if r["trace_id"] == first_root["trace_id"]]
    for record in first_trace:
        assert record["session_id"] == agent.session_id
    generations = [r for r in first_trace if r["type"] == "generation"]
    assert len(generations) == 2
    assert sorted(r["round"] for r in generations) == [1, 2]
    assert all(r["parent_id"] == first_root["span_id"] for r in generations)

    tools = [r for r in first_trace if r["type"] == "tool"]
    assert len(tools) == 1
    assert tools[0]["name"] == "tool.spy"
    assert tools[0]["parent_id"] == first_root["span_id"]
    assert tools[0]["round"] == 1
    assert tools[0]["attrs"]["decision"] == "execute"
    assert tools[0]["attrs"]["effective_decision"] == "execute"
    assert tools[0]["attrs"]["outcome"] == "ok"
    assert tools[0]["attrs"]["duration_ms"] > 0


def test_round_is_omitted_when_unknown(tmp_path):
    path = tmp_path / "trace.jsonl"
    tracer = JsonlTracer(path)
    with tracer.start("plain"):
        pass
    assert "round" not in rows(path)[0]


# ─── 2. content capture ────────────────────────────────────────────────────

def test_content_is_absent_by_default(tmp_path):
    path = tmp_path / "trace.jsonl"
    tracer = JsonlTracer(path)
    tool, llm = one_tool_round(result="secret sk-abcdefghijklmnop1234")
    make_agent(tracer, tool=tool, llm=llm).chat("go")

    for record in rows(path):
        assert "input" not in record["attrs"]
        assert "output" not in record["attrs"]


def test_capture_content_records_redacted_tool_io(tmp_path):
    path = tmp_path / "trace.jsonl"
    tracer = JsonlTracer(path, capture_content=True)
    tool, llm = one_tool_round(result="secret sk-abcdefghijklmnop1234")
    make_agent(tracer, tool=tool, llm=llm).chat("go")

    records = rows(path)
    root = next(r for r in records if r["name"] == "agent.chat")
    generation = [r for r in records if r["type"] == "generation"][-1]
    tool_span = next(r for r in records if r["type"] == "tool")

    assert root["attrs"]["input"] == "go"
    assert generation["attrs"]["output"] == "done"
    assert generation["attrs"]["input"][0]["role"] == "system"
    assert "ping" in tool_span["attrs"]["input"]
    assert "[REDACTED:api_key]" in tool_span["attrs"]["output"]
    assert "sk-abcdefghijklmnop1234" not in json.dumps(tool_span)


# ─── 3. Langfuse pseudo client ─────────────────────────────────────────────

class FakeObservation:
    def __init__(self, name, as_type, log):
        self.name = name
        self.as_type = as_type
        self.log = log
        self.updates = []
        self.events = []

    def update(self, **kwargs):
        self.updates.append(kwargs)

    def create_event(self, name, **kwargs):
        self.events.append((name, kwargs))


class FakeCM:
    def __init__(self, observation, log):
        self.observation = observation
        self.log = log

    def __enter__(self):
        self.log["entered"].append(self.observation.name)
        return self.observation

    def __exit__(self, exc_type, exc, tb):
        self.log["exited"].append(self.observation.name)
        return False


class FakeLangfuse:
    def __init__(self):
        self.log = {"entered": [], "exited": []}
        self.observations = []
        self.trace_updates = []
        self.flushed = False

    def start_as_current_observation(self, name, as_type):
        observation = FakeObservation(name, as_type, self.log)
        self.observations.append(observation)
        return FakeCM(observation, self.log)

    def update_current_trace(self, **kwargs):
        self.trace_updates.append(kwargs)

    def flush(self):
        self.flushed = True


def test_langfuse_pseudo_client_receives_spans_and_flush():
    client = FakeLangfuse()
    tracer = LangfuseTracer(client=client)
    tool, llm = one_tool_round()
    agent = make_agent(SafeTracer(tracer), tool=tool, llm=llm)

    assert agent.chat("go") == "done"

    types = [o.as_type for o in client.observations]
    names = [o.name for o in client.observations]
    assert "span" in types and "generation" in types and "tool" in types
    assert names[0] == "agent.chat"
    assert "llm.round" in names and "tool.spy" in names
    assert client.trace_updates == [{"session_id": agent.session_id}]

    generation = next(o for o in client.observations if o.name == "llm.round")
    metadata = [u.get("metadata", {}) for u in generation.updates]
    assert any(m.get("round") == 1 for m in metadata)
    assert any("finish_reason" in m for m in metadata)

    tracer.flush()
    assert client.flushed is True


# ─── 4/5. factory degradation ──────────────────────────────────────────────

def test_langfuse_without_keys_degrades_to_null(caplog):
    caplog.set_level(logging.WARNING)
    tracer = create_tracer("langfuse")
    assert isinstance(tracer, NullTracer)
    assert any("event=trace_disabled" in r.getMessage() for r in caplog.records)


def test_langfuse_without_sdk_degrades_to_null(monkeypatch, caplog):
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    monkeypatch.setitem(sys.modules, "langfuse", None)
    caplog.set_level(logging.WARNING)

    tracer = create_tracer("langfuse")

    assert isinstance(tracer, NullTracer)
    assert any("event=trace_disabled" in r.getMessage() for r in caplog.records)


def test_unknown_backend_degrades_to_null(caplog):
    caplog.set_level(logging.WARNING)
    assert isinstance(create_tracer("otlp"), NullTracer)
    assert any("event=trace_disabled" in r.getMessage() for r in caplog.records)


# ─── 6. write failure ──────────────────────────────────────────────────────

def test_write_failure_keeps_the_answer_and_warns_once(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    monkeypatch.setattr(tracing, "_os_write", lambda fd, data: (_ for _ in ()).throw(OSError("disk full")))

    tracer = create_tracer("jsonl", file=str(tmp_path / "trace.jsonl"))
    assert isinstance(tracer, SafeTracer)
    tool, llm = one_tool_round()
    agent = make_agent(tracer, tool=tool, llm=llm)

    assert agent.chat("go") == "done"
    errors = [r for r in caplog.records if "event=trace_error" in r.getMessage()]
    assert len(errors) == 1


# ─── 7. parallel tools ─────────────────────────────────────────────────────

def test_parallel_tools_get_spans_and_positive_durations(tmp_path):
    path = tmp_path / "trace.jsonl"
    tracer = JsonlTracer(path)
    tool = SpyTool(delay=0.02)
    llm = scripted(
        LLMResponse(content="", tool_calls=[
            ToolCall(id="a", name="spy", arguments={"text": "a"}),
            ToolCall(id="b", name="spy", arguments={"text": "b"}),
        ]),
        LLMResponse(content="done"),
    )
    agent = make_agent(tracer, tool=tool, llm=llm)

    assert agent.chat("go") == "done"

    records = rows(path)
    root = next(r for r in records if r["name"] == "agent.chat")
    tools = [r for r in records if r["type"] == "tool"]
    assert len(tools) == 2
    assert all(r["parent_id"] == root["span_id"] for r in tools)
    assert all(r["attrs"]["duration_ms"] > 0 for r in tools)
    assert {r["attrs"]["outcome"] for r in tools} == {"ok"}

    call_ids = [tc["id"] for m in agent.messages if m.get("role") == "assistant"
                for tc in (m.get("tool_calls") or [])]
    replies = [m["tool_call_id"] for m in agent.messages if m.get("role") == "tool"]
    assert sorted(call_ids) == sorted(replies)


# ─── 8. rotation ───────────────────────────────────────────────────────────

def test_jsonl_rotates_when_file_exceeds_max_bytes(tmp_path):
    path = tmp_path / "trace.jsonl"
    path.write_bytes(b"x" * 100)
    JsonlTracer(path, max_bytes=10)

    rotated = Path(str(path) + ".1")
    assert rotated.exists()
    assert rotated.stat().st_size == 100
    assert path.exists() and path.stat().st_size == 0


# ─── 9. injection beats env ────────────────────────────────────────────────

class RecordingTracer:
    capture_content = False

    def __init__(self):
        self.started = []

    def start(self, name, as_type="span", **attrs):
        self.started.append((name, as_type, attrs))
        return NullSpan()

    def flush(self):
        pass


def test_injected_tracer_is_used_and_env_off_creates_no_file(tmp_path, monkeypatch):
    recorder = RecordingTracer()
    tool, llm = one_tool_round()
    make_agent(recorder, tool=tool, llm=llm).chat("go")
    assert recorder.started[0][0] == "agent.chat"

    path = tmp_path / "never.jsonl"
    monkeypatch.setenv("CORECODER_TRACE_FILE", str(path))
    assert isinstance(create_tracer(), NullTracer)
    assert not path.exists()


# ─── 10/11. default path and retention ─────────────────────────────────────

def test_default_path_contains_pid_and_each_line_is_one_write(tmp_path, monkeypatch):
    assert str(os.getpid()) in tracing._default_trace_path().name

    writes = []

    def fake_write(fd, data):
        writes.append(data)
        return len(data)

    monkeypatch.setattr(tracing, "_os_write", fake_write)
    tracer = JsonlTracer(tmp_path / "trace.jsonl")
    with tracer.start("solo", as_type="span", round=3, session_id="s1") as span:
        span.update(answer=42)
    tracer.close()

    assert len(writes) == 1
    assert writes[0].count(b"\n") == 1
    record = json.loads(writes[0])
    assert record["name"] == "solo"
    assert record["round"] == 3
    assert record["session_id"] == "s1"
    assert record["parent_id"] is None
    assert record["attrs"]["answer"] == 42


def test_default_trace_files_keep_newest_ten(tmp_path):
    now = time.time()
    for i in range(11):
        old = tmp_path / f"traces.{1000 + i}.jsonl"
        old.write_text("{}\n", encoding="utf-8")
        os.utime(old, (now - i * 10, now - i * 10))

    current = tmp_path / f"traces.{os.getpid()}.jsonl"
    JsonlTracer(current, prune=True)

    kept = sorted(tmp_path.glob("traces.*.jsonl"))
    assert len(kept) == 10
    assert current in kept
    assert not (tmp_path / "traces.1010.jsonl").exists()


# ─── Langfuse nested context is entered/exited in order ────────────────────

def test_langfuse_spans_are_nested_in_order():
    client = FakeLangfuse()
    tracer = LangfuseTracer(client=client)
    with tracer.start("root", session_id="s"), tracer.start("child"):
        pass

    assert client.log["entered"] == ["root", "child"]
    assert client.log["exited"] == ["child", "root"]
    assert client.trace_updates == [{"session_id": "s"}]


def test_span_is_thread_safe_for_parallel_workers(tmp_path):
    path = tmp_path / "trace.jsonl"
    tracer = JsonlTracer(path)

    def worker(i):
        with tracer.start(f"worker.{i}"):
            time.sleep(0.001)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    records = rows(path)
    assert len(records) == 4
    assert all(r["parent_id"] is None for r in records)
    assert len({r["trace_id"] for r in records}) == 4
