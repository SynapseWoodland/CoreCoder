"""TDD tests for item 4: Ctrl+C must return immediately from a tool batch.

The pool is shut down without waiting: queued calls are cancelled, and calls
already running finish in the background. The history still gets an
`[interrupted]` reply for every unanswered call.
"""

import _thread
import threading
import time
from typing import ClassVar

import pytest

from corecoder import agent as agent_module
from corecoder.agent import Agent
from corecoder.demo import ScriptedLLM
from corecoder.llm import LLMResponse, ToolCall
from corecoder.permissions import Permission
from corecoder.tools.base import Tool


class InterruptTool(Tool):
    name = "fast"
    description = "Raise KeyboardInterrupt."
    parameters: ClassVar[dict] = {"type": "object", "properties": {}}

    def execute(self, **kwargs):
        raise KeyboardInterrupt


class SlowTool(Tool):
    name = "slow"
    description = "Block until released."
    parameters: ClassVar[dict] = {"type": "object", "properties": {}}

    def __init__(self, release: threading.Event):
        self.release = release
        self.finished = False

    def execute(self, **kwargs):
        self.release.wait(2)
        self.finished = True
        return "slow done"


class OkTool(Tool):
    name = "ok"
    description = "Return immediately."
    parameters: ClassVar[dict] = {"type": "object", "properties": {}}

    def execute(self, **kwargs):
        return "ok"


def two_calls(names=("fast", "slow")):
    return LLMResponse(
        content="",
        tool_calls=[
            ToolCall(id=f"c{i}", name=name, arguments={})
            for i, name in enumerate(names)
        ],
    )


def make_agent(tools, response, followup=False):
    turns = [response]
    if followup:
        turns.append(LLMResponse(content="done"))
    return Agent(
        llm=ScriptedLLM(turns),
        tools=tools,
        permission=Permission(allow_all=True),
        max_rounds=3,
    )


def pairs(messages):
    ids = [tc["id"] for m in messages if m.get("role") == "assistant"
           for tc in (m.get("tool_calls") or [])]
    replies = [m["tool_call_id"] for m in messages if m.get("role") == "tool"]
    return sorted(ids) == sorted(replies)


class FakeFuture:
    def __init__(self, value):
        self.value = value

    def result(self, timeout=None):
        return self.value

    def done(self):
        return True


class FakePool:
    instances: ClassVar[list] = []

    def __init__(self, *args, **kwargs):
        self.shutdowns = []
        self.submits = 0
        FakePool.instances.append(self)

    def submit(self, fn, *args):
        self.submits += 1
        i = args[0]
        return FakeFuture((i, "ok", None))

    def shutdown(self, wait=True, cancel_futures=False):
        self.shutdowns.append((wait, cancel_futures))


class InterruptingPool(FakePool):
    def submit(self, fn, *args):
        self.submits += 1
        if self.submits == 2:
            raise KeyboardInterrupt
        i = args[0]
        return FakeFuture((i, "ok", None))


@pytest.fixture(autouse=True)
def reset_pools():
    FakePool.instances.clear()
    yield
    FakePool.instances.clear()


def test_ctrl_c_returns_without_waiting_for_slow_tool(monkeypatch):
    release = threading.Event()
    slow = SlowTool(release)
    agent = make_agent([InterruptTool(), slow], two_calls())

    started = time.perf_counter()
    try:
        with pytest.raises(KeyboardInterrupt):
            agent.chat("go")
    finally:
        elapsed = time.perf_counter() - started
        release.set()

    assert elapsed < 0.8
    assert pairs(agent.messages)
    assert not slow.finished


def test_pool_is_shut_down_without_wait_and_cancels_queued(monkeypatch):
    monkeypatch.setattr("corecoder.agent.concurrent.futures.ThreadPoolExecutor",
                        FakePool)
    agent = make_agent([OkTool(), OkTool()], two_calls(names=("ok", "ok")),
                       followup=True)

    agent.chat("go")

    pool = FakePool.instances[-1]
    assert pool.shutdowns == [(False, True)]


def test_batch_interrupted_event_reports_inflight(monkeypatch):
    events = []
    monkeypatch.setattr(agent_module, "log_event",
                        lambda event, **kw: events.append((event, kw)))
    release = threading.Event()
    agent = make_agent([InterruptTool(), SlowTool(release)], two_calls())

    try:
        with pytest.raises(KeyboardInterrupt):
            agent.chat("go")
    finally:
        release.set()

    batch = [kw for name, kw in events if name == "batch_interrupted"]
    assert batch, "batch_interrupted was not logged"
    assert batch[0]["submitted"] == 2
    assert batch[0]["inflight"] >= 1


def test_signal_reaches_main_thread_while_waiting():
    """A pending Ctrl+C must not wait for the slow tool to finish."""
    release = threading.Event()
    agent = make_agent([SlowTool(release), SlowTool(release)],
                       two_calls(names=("slow", "slow")))
    timer = threading.Timer(0.2, _thread.interrupt_main)
    timer.start()

    started = time.perf_counter()
    try:
        with pytest.raises(KeyboardInterrupt):
            agent.chat("go")
    finally:
        elapsed = time.perf_counter() - started
        timer.cancel()
        release.set()

    assert elapsed < 1.0
    assert pairs(agent.messages)
    assert all(m["content"] == "[interrupted]"
               for m in agent.messages if m.get("role") == "tool")


def test_single_tool_interrupt_still_backfills():
    agent = make_agent([InterruptTool()], two_calls(names=("fast",)))

    with pytest.raises(KeyboardInterrupt):
        agent.chat("go")

    assert pairs(agent.messages)
    replies = [m["content"] for m in agent.messages if m.get("role") == "tool"]
    assert replies == ["[interrupted]"]


def test_interrupt_during_submit_keeps_the_original_exception(monkeypatch):
    events = []
    monkeypatch.setattr(agent_module, "log_event",
                        lambda event, **kw: events.append((event, kw)))
    monkeypatch.setattr("corecoder.agent.concurrent.futures.ThreadPoolExecutor",
                        InterruptingPool)
    agent = make_agent([OkTool(), OkTool()], two_calls(names=("ok", "ok")))

    with pytest.raises(KeyboardInterrupt) as exc:
        agent.chat("go")

    assert type(exc.value) is KeyboardInterrupt
    pool = InterruptingPool.instances[-1]
    assert pool.shutdowns == [(False, True)]
    batch = [kw for name, kw in events if name == "batch_interrupted"]
    assert batch and batch[0]["submitted"] == 0
    assert pairs(agent.messages)
