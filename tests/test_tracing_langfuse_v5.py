"""Langfuse SDK 5.x contract: trace-level attributes, native usage and cost.

The installed SDK is 5.0.4, which no longer has ``update_current_trace``: v4
replaced it with the module-level ``propagate_attributes`` context manager.
``tracing.py`` guarded the old call with ``getattr(client, ..., None)``, so on
5.x every trace-level attribute (session_id, user_id, tags) was dropped without
a single warning, while ``tests/test_tracing.py`` stayed green because its
pseudo client still implements the removed method.

These tests therefore fake the 5.0.4 surface exactly -- no
``update_current_trace``, a module-level ``propagate_attributes``, and
``observation.update(usage_details=..., cost_details=...)`` -- so a regression
to the old, silently-degrading path fails here.
"""

import sys
import types

import pytest

from corecoder.tracing import (
    LangfuseTracer,
    NullTracer,
    SafeTracer,
    create_tracer,
    current_trace_context,
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in ("CORECODER_TRACE", "CORECODER_TRACE_CONTENT",
                "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY",
                "LANGFUSE_HOST", "LANGFUSE_BASE_URL"):
        monkeypatch.delenv(key, raising=False)


class FakeObservation:
    """A 5.x observation. The two id fields are public, and the audit log has to
    quote the backend's own values rather than a uuid we minted ourselves."""

    def __init__(self, name, as_type, order, trace_id=None, observation_id=None):
        self.name = name
        self.as_type = as_type
        self.order = order
        self.trace_id = trace_id
        self.id = observation_id
        self.updates = []
        self.events = []

    def update(self, **kwargs):
        self.updates.append(kwargs)

    def create_event(self, name, **kwargs):
        self.events.append((name, kwargs))


class FakeObservationCM:
    def __init__(self, observation, order):
        self.observation = observation
        self.order = order

    def __enter__(self):
        self.order.append(f"obs_enter:{self.observation.name}")
        return self.observation

    def __exit__(self, exc_type, exc, tb):
        self.order.append(f"obs_exit:{self.observation.name}")
        return False


class FakeClient5:
    """The 5.0.4 client surface, deliberately without update_current_trace."""

    expose_ids = True

    def __init__(self):
        self.order = []
        self.observations = []
        self.flushed = 0
        self.trace_id = "0a1b2c3d4e5f60718293a4b5c6d7e8f9"

    def start_as_current_observation(self, *, name, as_type="span", **kwargs):
        ids = ((self.trace_id, f"{len(self.observations):016x}")
               if self.expose_ids else (None, None))
        observation = FakeObservation(name, as_type, self.order, *ids)
        self.observations.append(observation)
        return FakeObservationCM(observation, self.order)

    def flush(self):
        self.flushed += 1

    def by_name(self, name):
        return [o for o in self.observations if o.name == name]


class FakePropagateCM:
    def __init__(self, kwargs, order):
        self.kwargs = kwargs
        self.order = order

    def __enter__(self):
        self.order.append("attr_enter")
        return self

    def __exit__(self, exc_type, exc, tb):
        self.order.append("attr_exit")
        return False


def install_fake_langfuse(monkeypatch, client, *, propagate=True):
    """Register a fake `langfuse` module whose surface matches the given SDK."""
    module = types.ModuleType("langfuse")
    module.get_client = lambda: client
    calls = []
    if propagate:
        def propagate_attributes(**kwargs):
            calls.append(kwargs)
            return FakePropagateCM(kwargs, client.order)
        module.propagate_attributes = propagate_attributes
    monkeypatch.setitem(sys.modules, "langfuse", module)
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    return calls


def test_fake_client_matches_the_5x_surface():
    """Guard the fake itself: testing the old API would hide the whole bug."""
    client = FakeClient5()
    assert not hasattr(client, "update_current_trace")


def test_trace_attributes_go_through_propagate_attributes(monkeypatch):
    client = FakeClient5()
    calls = install_fake_langfuse(monkeypatch, client)
    tracer = create_tracer("langfuse")

    assert isinstance(tracer, SafeTracer)
    with tracer.start("agent.chat", session_id="s-1", user_id="u-1",
                      tags=["corecoder", "smoke"]):
        pass

    assert calls == [{"session_id": "s-1", "user_id": "u-1",
                      "tags": ["corecoder", "smoke"]}]


def test_trace_attributes_wrap_the_root_span_and_are_not_restarted(monkeypatch):
    client = FakeClient5()
    calls = install_fake_langfuse(monkeypatch, client)
    tracer = SafeTracer(LangfuseTracer())

    with tracer.start("agent.chat", session_id="s-1"), \
            tracer.start("llm.round", as_type="generation"):
        pass

    assert len(calls) == 1, "child spans must not re-open the attribute context"
    order = client.order
    assert order[0] == "attr_enter"
    assert order.index("attr_enter") < order.index("obs_enter:agent.chat")
    assert order.index("attr_exit") > order.index("obs_exit:agent.chat")


def test_trace_attributes_are_not_duplicated_into_metadata(monkeypatch):
    client = FakeClient5()
    install_fake_langfuse(monkeypatch, client)
    tracer = SafeTracer(LangfuseTracer())

    with tracer.start("agent.chat", session_id="s-1", user_id="u-1", tags=["t"]):
        pass

    metadata = [u["metadata"] for u in client.by_name("agent.chat")[0].updates
                if "metadata" in u]
    assert all("session_id" not in m and "user_id" not in m for m in metadata)


def test_reported_usage_maps_to_native_usage_details(monkeypatch):
    client = FakeClient5()
    install_fake_langfuse(monkeypatch, client)
    tracer = SafeTracer(LangfuseTracer())

    with tracer.start("agent.chat", session_id="s-1"), \
            tracer.start("llm.round", as_type="generation",
                         model="gpt-4o-mini") as gen:
        # exactly what agent.py:215 puts on the generation span
        gen.update(prompt_tokens=5, completion_tokens=3, usage_reported=True,
                   finish_reason="stop", cost_usd=0.00012)

    observation = client.by_name("llm.round")[0]
    usage = [u["usage_details"] for u in observation.updates if "usage_details" in u]
    assert usage and usage[-1] == {"input": 5, "output": 3}
    assert all("usage_details" not in (u.get("metadata") or {})
               for u in observation.updates)


def test_reported_cost_maps_to_native_cost_details(monkeypatch):
    client = FakeClient5()
    install_fake_langfuse(monkeypatch, client)
    tracer = SafeTracer(LangfuseTracer())

    with tracer.start("agent.chat", session_id="s-1"), \
            tracer.start("llm.round", as_type="generation") as gen:
        gen.update(prompt_tokens=5, completion_tokens=3, usage_reported=True,
                   cost_usd=0.00012)

    observation = client.by_name("llm.round")[0]
    cost = [u["cost_details"] for u in observation.updates if "cost_details" in u]
    assert cost and cost[-1] == {"total": 0.00012}
    assert all("cost_usd" not in (u.get("metadata") or {})
               for u in observation.updates), "cost must not be metadata-only"


def test_unreported_usage_never_fabricates_numbers(monkeypatch):
    client = FakeClient5()
    install_fake_langfuse(monkeypatch, client)
    tracer = SafeTracer(LangfuseTracer())

    with tracer.start("agent.chat", session_id="s-1"), \
            tracer.start("llm.round", as_type="generation") as gen:
        # agent.py passes None/None + False when the provider sent no usage
        gen.update(prompt_tokens=None, completion_tokens=None,
                   usage_reported=False, cost_usd=None)

    observation = client.by_name("llm.round")[0]
    assert all("usage_details" not in u for u in observation.updates)
    assert all("cost_details" not in u for u in observation.updates)


def test_legacy_update_current_trace_is_still_used(monkeypatch):
    """SDK 3.x path must keep working: no propagate_attributes, old method."""
    class FakeClient3(FakeClient5):
        def __init__(self):
            super().__init__()
            self.trace_updates = []

        def update_current_trace(self, **kwargs):
            self.trace_updates.append(kwargs)

    client = FakeClient3()
    install_fake_langfuse(monkeypatch, client, propagate=False)
    tracer = SafeTracer(LangfuseTracer())

    with tracer.start("agent.chat", session_id="s-1"):
        pass

    assert client.trace_updates == [{"session_id": "s-1"}]


def test_no_trace_attribute_api_at_all_still_traces(monkeypatch):
    client = FakeClient5()
    install_fake_langfuse(monkeypatch, client, propagate=False)
    tracer = SafeTracer(LangfuseTracer(capture_content=True))

    with tracer.start("agent.chat", session_id="s-1", model="m") as span:
        span.update(output="done")

    observation = client.by_name("agent.chat")[0]
    assert observation.updates[-1].get("output") == "done"
    assert client.flushed == 0  # nothing flushed until asked


def test_factory_still_degrades_without_keys():
    assert isinstance(create_tracer("langfuse"), NullTracer)


def test_audit_join_keys_are_the_backends_own_ids(monkeypatch):
    """The ids the audit log quotes must be the ones the trace UI can find (C2).

    We minted a uuid and never told the SDK, which kept its own; every trace_id
    in the audit log was then a dead link against the REST API, so joining an
    audit row to its trace meant pasting an id that 404s.
    """
    client = FakeClient5()
    install_fake_langfuse(monkeypatch, client)
    tracer = LangfuseTracer()

    root = tracer.start("agent.chat", session_id="s-1")
    child = tracer.start("llm.round", as_type="generation")

    assert root.trace_id == client.trace_id
    assert child.trace_id == client.trace_id
    assert root.span_id == client.observations[0].id
    assert child.span_id == client.observations[1].id
    assert current_trace_context() == {"trace_id": client.trace_id,
                                       "span_id": child.span_id}
    child.end()
    root.end()


def test_ids_stay_our_own_when_the_sdk_shows_none(monkeypatch):
    """An older client without public ids must not lose the key entirely."""
    client = FakeClient5()
    client.expose_ids = False
    install_fake_langfuse(monkeypatch, client)
    tracer = LangfuseTracer()

    span = tracer.start("agent.chat", session_id="s-1")

    assert span.trace_id != client.trace_id
    assert len(span.trace_id) == 32 and len(span.span_id) == 32
    span.end()
