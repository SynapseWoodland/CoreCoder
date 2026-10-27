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


# ─── 5. P1: the system prompt must stop re-listing every description ────────


class TestPromptSlimming:
    """The catalog in # Tools duplicates, verbatim, the description the model
    already gets in the structured ``tools=`` array. Dropping it from the
    prompt shrinks the fixed overhead without losing any capability signal."""

    def test_system_prompt_lists_tool_names_only(self):
        from corecoder.prompt import system_prompt

        spy = SpyTool()
        spy.description = "SENTINEL_DESC_must_not_appear_in_the_prompt"
        prompt = system_prompt([spy])

        assert spy.name in prompt, "the tool must still be enumerable"
        assert spy.description not in prompt, (
            "description is still copied into the system prompt: it is already "
            "in tools=, so every request carries it twice"
        )

    def test_schema_descriptions_survive_prompt_slimming(self):
        spy = SpyTool()
        spy.description = "SENTINEL_DESC_must_survive_in_tools"
        llm = RecordingLLM([LLMResponse(content="ok")])
        agent = Agent(llm=llm, tools=[spy])
        assert agent.chat("hi") == "ok"

        # whatever the slimmed prompt says, the model still receives the full
        # description where function calling actually reads it
        [sent] = llm.seen
        [doc] = sent["tools"]
        assert doc["function"]["description"] == spy.description, (
            "slimming the prompt must not slim the tools= schema: the "
            "description is how the model chooses and calls a tool"
        )


# ─── 6. P3: defer MCP schemas behind an on-demand search_tools meta-tool ─────


class FakeMcpTool(Tool):
    """An MCP-shaped tool (mcp__ name) with a real, untrimmed inputSchema."""

    def __init__(self, name, description="do the thing"):
        self.name = name
        self.description = description
        self.parameters = {
            "type": "object",
            "properties": {"x": {"type": "string", "description": "keep me"}},
            "required": ["x"],
        }

    def execute(self, **kwargs) -> str:
        return f"ran {self.name}"


def _mcp_tools(n):
    return [FakeMcpTool(f"mcp__s__t{i}", description=f"performs action number {i}")
            for i in range(n)]


def _deferred_agent(n=10, llm=None, **kw):
    monkey = kw.pop("monkeypatch", None)
    if monkey is not None:
        monkey.delenv("CORECODER_TOOL_SEARCH", raising=False)
        monkey.delenv("CORECODER_TOOL_SEARCH_MIN_COUNT", raising=False)
        monkey.delenv("CORECODER_TOOL_SEARCH_PCT", raising=False)
    return Agent(
        llm=llm or ScriptedLLM([LLMResponse(content="ok")]),
        tools=[SpyTool(), *_mcp_tools(n)],
        **kw,
    )


def _names(schema_list):
    return {s["function"]["name"] for s in schema_list}


class TestDeferredToolSearch:
    def test_no_deferral_below_threshold_is_byte_identical(self):
        # one MCP tool: auto must not engage, so the request stays exactly
        # what P0/P1 produced -- every tool eager, no search_tools injected
        agent = _deferred_agent(n=1)
        names = _names(agent._tool_schemas())
        assert "mcp__s__t0" in names, "small MCP set must stay eager (no regression)"
        assert "search_tools" not in names, (
            "search_tools leaked into a below-threshold catalog; the default "
            "path must be byte-identical to P1"
        )

    def test_deferral_engages_above_threshold(self):
        agent = _deferred_agent(n=10)
        names = _names(agent._tool_schemas())
        assert "spy" in names and "search_tools" in names
        assert "mcp__s__t3" not in names, (
            "an MCP tool's full schema was sent before it was ever requested"
        )
        # the withheld tool is still discoverable by name in the prompt index
        system = agent._full_messages()[0]["content"]
        assert "mcp__s__t3" in system, (
            "deferred tools must appear in the compact index or the model "
            "cannot know to search for them"
        )
        # the compact index carries a short summary, never the full schema
        assert '"mcp__s__t3"' not in agent._schema_json_str(), (
            "a deferred tool's schema was serialized into the sent payload"
        )

    def test_search_activates_tool_full_schema_verbatim(self):
        agent = _deferred_agent(n=10)
        target = agent._mcp_by_name["mcp__s__t3"]
        loaded = agent._activate_matching("t3")
        assert loaded == ["mcp__s__t3"]
        docs = {d["function"]["name"]: d for d in agent._tool_schemas()}
        assert "mcp__s__t3" in docs, "activated tool did not enter the sent set"
        # loading must hand back the parameters untouched -- properties and
        # required are the contract that makes the call technically valid
        assert docs["mcp__s__t3"]["function"]["parameters"] == target.parameters

    def test_active_tools_append_only_keeps_prefix_stable(self):
        agent = _deferred_agent(n=10)
        before = agent._schema_json_str()
        assert agent._tool_schemas() is agent._tool_schemas(), (
            "an unchanged active set must keep serving the cached object"
        )
        agent._activate_matching("t3")
        after = agent._schema_json_str()
        # the earlier serialization is a prefix of the later one: a provider
        # prefix cache discounts the unchanged head when tools are appended
        assert after.startswith(before[:-1]), (
            "activation rewrote the tool list instead of appending: prompt-"
            "cache prefix is broken"
        )

    def test_fixed_prompt_tokens_tracks_sent_after_activation(self):
        agent = _deferred_agent(n=10)
        fixed0 = agent._fixed_prompt_tokens()
        system = [{"role": "system", "content": agent._full_messages()[0]["content"]}]
        assert fixed0 == (
            estimate_tokens(system) + estimate_tokens([{"content": agent._schema_json_str()}]))
        agent._activate_matching("t3")
        fixed1 = agent._fixed_prompt_tokens()
        assert fixed1 > fixed0, (
            "loading a tool changed the real payload but not the estimate: "
            "context.py fixed_overhead would under-count"
        )
        assert fixed1 == (
            estimate_tokens(system) + estimate_tokens([{"content": agent._schema_json_str()}]))

    def test_kill_switch_off_never_defers(self, monkeypatch):
        monkeypatch.setenv("CORECODER_TOOL_SEARCH", "off")
        agent = _deferred_agent(n=12)
        names = _names(agent._tool_schemas())
        assert "search_tools" not in names
        assert "mcp__s__t5" in names, (
            "CORECODER_TOOL_SEARCH=off must be a total disable: eager behavior"
        )

    def test_deferred_index_summary_is_short(self):
        from corecoder.prompt import system_prompt

        tool = FakeMcpTool(
            "mcp__s__echo", "a" * 400 + " tail never shown in the index")
        prompt = system_prompt([SpyTool()], deferred=[tool])
        line = next(ln for ln in prompt.splitlines() if "mcp__s__echo" in ln)
        assert len(line) < 120, (
            "deferred index line is not compact -- it must not dump the full "
            "description, only name + a short summary"
        )
        assert "a" * 400 not in prompt

    def test_subagent_inherits_deferral_and_loaded(self):
        # loaded_names is what tools/agent.py will pass to a child so a tool
        # the parent already searched for stays available without a re-search
        agent = _deferred_agent(n=10, loaded_names=["mcp__s__t7"])
        names = _names(agent._tool_schemas())
        assert "mcp__s__t7" in names, "inherited loaded tool was not activated"
        assert "mcp__s__t8" not in names, (
            "deferral did not engage for the inherited catalog"
        )
