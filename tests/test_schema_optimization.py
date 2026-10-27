"""P0: tool-schema caching and a single estimation entrance.

Every model round sends the full tool catalog through ``tools=`` and every
``_fixed_prompt_tokens()`` call re-serialized it for the estimate, so one
``chat()`` rebuilt and re-dumped the same ~4.6 KB several times over. The
cache keeps the sent bytes identical round to round - what a provider's
prefix cache can discount - and makes the estimation entrance reuse the
same string instead of a fresh ``json.dumps``.

Written BEFORE the fix (TDD red): tests 1-3 assert the cached identity and
the dumps budget, which the current rebuild-every-call shape cannot give.
Test 4 pins the boundary that must NOT move: the fixed term stays history-
free and counts the system prompt once (the M3 formula).
"""

import json
from typing import ClassVar

from corecoder.agent import Agent
from corecoder.context import estimate_tokens
from corecoder.demo import ScriptedLLM
from corecoder.llm import LLMResponse, ToolCall
from corecoder.tools.base import Tool

# ─── helpers ────────────────────────────────────────────────────────────────


class SpyTool(Tool):
    """Zero-arg tool so a scripted round can execute without side effects."""

    name = "spy"
    description = "records execution count"
    parameters: ClassVar[dict] = {"type": "object", "properties": {}}

    def __init__(self):
        self.calls: list = []

    def execute(self) -> str:
        self.calls.append(1)
        return "executed"


class RecordingLLM(ScriptedLLM):
    """ScriptedLLM that keeps the kwargs of every call it played back."""

    def __init__(self, script):
        super().__init__(script)
        self.seen: list[dict] = []

    def chat(self, messages, tools=None, on_token=None, on_reasoning=None):
        self.seen.append({"messages": messages, "tools": tools})
        return super().chat(messages, tools=tools, on_token=on_token,
                            on_reasoning=on_reasoning)


def _answer_agent(spy=None):
    return Agent(llm=ScriptedLLM([LLMResponse(content="ok")]),
                 tools=[spy or SpyTool()])


def _two_round_agent(llm_model):
    """One tool round then an answer: enough rounds to count dumps."""
    return Agent(
        llm=llm_model,
        tools=[SpyTool()],
        max_rounds=5,
    )


# ─── 1. the schema list is built once while the catalog is unchanged ────────


class TestSchemaDocsCached:
    def test_schema_docs_cached_across_calls(self):
        agent = _answer_agent()
        first = agent._tool_schemas()
        assert first == agent._tool_schemas()  # sanity: content is there
        assert agent._tool_schemas() is first, (
            "schemas rebuilt per call: the request bytes are not a cached "
            "shape a provider prefix cache can discount"
        )


# ─── 2. mutating the catalog must never leave a stale schema behind ─────────


class TestSchemaCacheInvalidation:
    def test_schema_cache_invalidates_on_tool_add(self):
        agent = _answer_agent()
        agent._tool_schemas()  # warm the cache
        extra = SpyTool()
        extra.name = "late_tool"
        agent.tools.append(extra)

        names = [s["function"]["name"] for s in agent._tool_schemas()]
        assert "late_tool" in names, "cache served a stale catalog"

    def test_schema_cache_invalidates_on_tool_replace(self):
        agent = _answer_agent()
        before = agent._tool_schemas()
        replaced = SpyTool()
        replaced.description = "now says something else"
        agent.tools[0] = replaced

        assert agent._tool_schemas() != before
        assert agent._tool_schemas()[0]["function"]["description"] == (
            "now says something else")


# ─── 3. one request round serializes the catalog at most once ───────────────


class TestSingleSerialization:
    def test_fixed_prompt_tokens_uses_cached_json_and_counts_schemas_once(self, monkeypatch):
        agent = _two_round_agent(ScriptedLLM([
            LLMResponse(tool_calls=[ToolCall(id="t1", name="spy", arguments={})]),
            LLMResponse(content="done"),
        ]))
        agent._tool_schemas()  # warm: the cache's own dump lands before counting

        counts: dict[str, int] = {}
        real_dumps = json.dumps

        def counting_dumps(obj, *a, **kw):
            if isinstance(obj, list) and obj and isinstance(obj[0], dict) \
                    and obj[0].get("type") == "function":
                counts["schema"] = counts.get("schema", 0) + 1
            return real_dumps(obj, *a, **kw)

        monkeypatch.setattr("corecoder.agent.json.dumps", counting_dumps)
        assert agent.chat("go") == "done"
        assert counts.get("schema", 0) <= 1, (
            f"schema JSON rebuilt {counts['schema']}x in one chat(): "
            "_fixed_prompt_tokens must reuse the cached string"
        )

    def test_llm_call_event_has_schema_tokens(self, monkeypatch):
        import corecoder.agent as agent_module

        captured: list[dict] = []
        real_log = agent_module.log_event

        def recording_log_event(name, **fields):
            if name == "llm_call":
                captured.append(fields)
            return real_log(name, **fields)

        monkeypatch.setattr(agent_module, "log_event", recording_log_event)
        agent = _two_round_agent(ScriptedLLM([
            LLMResponse(tool_calls=[ToolCall(id="t1", name="spy", arguments={})]),
            LLMResponse(content="done"),
        ]))
        assert agent.chat("go") == "done"

        assert captured, "no llm_call event was logged"
        for fields in captured:
            assert fields["schema_tokens"] > 0, (
                "llm_call must report the schema's own token cost: it is the "
                "quantitative yardstick for the schema-slimming phases"
            )


# ─── 4. the boundary that must not move: fixed term stays history-free ──────


class TestEstimationEntranceUnchanged:
    def test_fixed_prompt_tokens_formula_after_caching(self):
        agent = _answer_agent()
        fixed = agent._fixed_prompt_tokens()

        full = agent._full_messages()
        system = [{"role": "system", "content": full[0]["content"]}]
        schemas = [{"content": json.dumps(agent._tool_schemas())}]
        assert fixed == estimate_tokens(system) + estimate_tokens(schemas)

        # the double-count shape the M3 test forbids: history absorbed into
        # the fixed term would make every request counted twice
        assert fixed == agent._fixed_prompt_tokens()
        agent.messages.append({"role": "user", "content": "x" * 4000})
        assert agent._fixed_prompt_tokens() == fixed

    def test_sent_tools_are_the_cached_docs(self):
        llm = RecordingLLM([LLMResponse(content="ok")])
        agent = Agent(llm=llm, tools=[SpyTool()])
        assert agent.chat("hi") == "ok"

        [sent] = llm.seen
        assert sent["tools"] is agent._tool_schemas(), (
            "the estimation and the request must read the same cached object, "
            "or schema_tokens would describe a payload nobody sent"
        )
