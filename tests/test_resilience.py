"""Resilience features: model fallback chain (llm) + budget ceiling (agent).

Both are the "no evaluation evidence required" production valves: a primary
model that is merely unavailable falls forward through a candidate chain, and a
runnable cost cap stops a runaway task before it burns the whole budget. The
tests below pin the two behaviours the rest of the loop relies on - LLM.chat
keeping its signature, and a non-retryable 4xx still reaching the caller's own
overflow recovery untouched.
"""

from types import SimpleNamespace
from unittest import mock

import pytest

from corecoder.agent import Agent, BudgetExceeded
from corecoder.demo import ScriptedLLM
from corecoder.llm import LLM, LLMResponse


def _httpx():
    try:
        import httpx
    except ModuleNotFoundError:
        # openai>=3.14 moved its HTTP layer from httpx to httpx2
        import httpx2 as httpx
    return httpx


def _connection_error():
    from openai import APIConnectionError
    return APIConnectionError(
        request=_httpx().Request("POST", "https://api.openai.com/v1/chat/completions"))


def _bad_request(msg):
    from openai import BadRequestError
    req = _httpx().Request("POST", "https://api.openai.com/v1/chat/completions")
    return BadRequestError(msg, response=_httpx().Response(400, request=req), body=None)


def _good_stream(content="full"):
    return iter([SimpleNamespace(
        usage=SimpleNamespace(prompt_tokens=7, completion_tokens=2),
        choices=[SimpleNamespace(delta=SimpleNamespace(
            content=content, tool_calls=None, reasoning_content=None))],
    )])


class _DyingStream:
    """Yields one chunk, then raises like a dropped connection mid-stream."""

    def __init__(self, exc):
        self._exc = exc
        self._yielded = False

    def __iter__(self):
        return self

    def __next__(self):
        if not self._yielded:
            self._yielded = True
            return SimpleNamespace(
                usage=None,
                choices=[SimpleNamespace(delta=SimpleNamespace(
                    content="p", tool_calls=None, reasoning_content=None))],
            )
        raise self._exc


def _llm(model, fallback=None):
    llm = LLM(model=model, api_key="sk-test", fallback_models=fallback)
    llm.client = mock.Mock()
    return llm


# ---------------------------------------------------------------------------
# Model fallback chain (llm.py)
# ---------------------------------------------------------------------------


class TestModelFallback:
    def test_fallback_after_primary_exhausts(self):
        llm = _llm("kimi-k3", fallback=["deepseek-chat"])
        create = llm.client.chat.completions.create
        create.side_effect = [
            _DyingStream(_connection_error()),
            _DyingStream(_connection_error()),
            _DyingStream(_connection_error()),
            _good_stream(),
        ]
        with mock.patch("corecoder.llm.time.sleep"):
            result = llm.chat(messages=[{"role": "user", "content": "hi"}])
        assert result.content == "full"
        # 3 exhausted attempts on the primary, then one success on the fallback
        assert create.call_count == 4
        assert create.call_args_list[-1][1]["model"] == "deepseek-chat"

    def test_no_fallback_when_primary_succeeds(self):
        llm = _llm("kimi-k3", fallback=["deepseek-chat"])
        create = llm.client.chat.completions.create
        create.return_value = _good_stream()
        result = llm.chat(messages=[{"role": "user", "content": "hi"}])
        assert result.content == "full"
        assert create.call_count == 1
        assert create.call_args[1]["model"] == "kimi-k3"

    def test_all_models_exhaust_then_raise(self):
        from openai import APIConnectionError
        llm = _llm("kimi-k3", fallback=["deepseek-chat"])
        create = llm.client.chat.completions.create
        create.side_effect = [
            _DyingStream(_connection_error()),
            _DyingStream(_connection_error()),
            _DyingStream(_connection_error()),
            _DyingStream(_connection_error()),
            _DyingStream(_connection_error()),
            _DyingStream(_connection_error()),
        ]
        with mock.patch("corecoder.llm.time.sleep"), pytest.raises(APIConnectionError):
            llm.chat(messages=[{"role": "user", "content": "hi"}])
        # 3 attempts on each of the two models
        assert create.call_count == 6

    def test_client_error_does_not_fallback(self):
        """A 4xx (here a context-length rejection) must propagate untouched so
        the agent's own overflow recovery still sees it - no fallback, ever."""
        from openai import BadRequestError
        llm = _llm("kimi-k3", fallback=["deepseek-chat"])
        create = llm.client.chat.completions.create
        create.side_effect = _bad_request("maximum context length is 8192 tokens")
        with pytest.raises(BadRequestError):
            llm.chat(messages=[{"role": "user", "content": "hi"}])
        assert all(c[1]["model"] == "kimi-k3" for c in create.call_args_list)

    def test_chat_signature_unchanged(self):
        import inspect
        sig = inspect.signature(LLM.chat)
        assert list(sig.parameters) == [
            "self", "messages", "tools", "on_token", "on_reasoning"]

    def test_fallback_models_not_leaked_into_request(self):
        llm = _llm("kimi-k3", fallback=["deepseek-chat"])
        assert "fallback_models" not in llm.extra
        create = llm.client.chat.completions.create
        create.return_value = _good_stream()
        llm.chat(messages=[{"role": "user", "content": "hi"}])
        assert "fallback_models" not in create.call_args[1]

    def test_duplicates_and_blanks_dropped_from_chain(self):
        llm = LLM.__new__(LLM)
        llm.model = "kimi-k3"
        llm.fallback_models = ["", "kimi-k3", "deepseek-chat", "deepseek-chat"]
        assert llm._model_chain() == ["kimi-k3", "deepseek-chat"]

    def test_default_no_fallback_is_single_model(self):
        llm = LLM(model="kimi-k3", api_key="sk-test")
        assert llm.fallback_models == []
        assert llm._model_chain() == ["kimi-k3"]


# ---------------------------------------------------------------------------
# Budget ceiling (agent.py _account_usage)
# ---------------------------------------------------------------------------


class _PricedLLM:
    """A minimal priced backend: gpt-5.4 rates ($2.5/M in, $15/M out) baked in,
    so cost grows deterministically with the usage each reported call carries."""

    model = "gpt-5.4"

    def __init__(self, responses):
        self._responses = list(responses)
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0

    @property
    def estimated_cost(self):
        return (self.total_prompt_tokens * 2.5
                + self.total_completion_tokens * 15) / 1_000_000

    def chat(self, messages, tools=None, on_token=None, on_reasoning=None):
        resp = self._responses.pop(0)
        if resp.usage_reported:
            self.total_prompt_tokens += resp.prompt_tokens
            self.total_completion_tokens += resp.completion_tokens
        return resp


def _spendy(content="", tool_calls=None):
    return LLMResponse(
        content=content, tool_calls=tool_calls or [],
        prompt_tokens=1_000_000, completion_tokens=500_000, usage_reported=True)


class TestBudgetCeiling:
    def test_over_ceiling_trips_after_first_call(self):
        # one call already costs $10 against a $1 ceiling
        llm = _PricedLLM([_spendy(content="should never settle")])
        agent = Agent(llm=llm, tools=[], budget_ceiling=1.0)
        with pytest.raises(BudgetExceeded):
            agent.chat("go")

    def test_within_ceiling_completes(self):
        llm = _PricedLLM([LLMResponse(
            content="done", prompt_tokens=100, completion_tokens=50,
            usage_reported=True)])
        agent = Agent(llm=llm, tools=[], budget_ceiling=100.0)
        assert agent.chat("go") == "done"

    def test_no_ceiling_never_trips(self):
        llm = _PricedLLM([_spendy(content="done")])
        agent = Agent(llm=llm, tools=[])
        assert agent.chat("go") == "done"

    def test_unpriced_model_skips_budget(self):
        # ScriptedLLM reports estimated_cost None: nothing mechanical to enforce
        llm = ScriptedLLM([LLMResponse(content="done")])
        agent = Agent(llm=llm, tools=[], budget_ceiling=0.0001)
        assert agent.chat("go") == "done"

    def test_default_is_no_ceiling(self):
        agent = Agent(llm=_PricedLLM([]), tools=[])
        assert agent.budget_ceiling is None
