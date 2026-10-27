"""Shared pytest fixtures and helpers."""

import pytest

from corecoder import Config, audit, checkpoints, cli
from corecoder import llm as llm_module
from corecoder.tools import ALL_TOOLS


@pytest.fixture(autouse=True)
def _checkpoints_isolated(tmp_path, monkeypatch):
    """Checkpoint persistence must never touch the real ~/.corecoder."""
    monkeypatch.setattr(checkpoints, "CHECKPOINTS_FILE", tmp_path / "checkpoints.json")
    checkpoints.clear()
    yield
    checkpoints.clear()


@pytest.fixture(autouse=True)
def _pricing_isolated(tmp_path, monkeypatch):
    """Cost estimates must never read the real ~/.corecoder/pricing.json."""
    monkeypatch.setattr(llm_module, "PRICING_FILE", tmp_path / "pricing.json")


@pytest.fixture(autouse=True)
def _observability_isolated(tmp_path, monkeypatch):
    """Audit and trace must never reach the real ~/.corecoder or Langfuse.

    Most tests build an Agent without a tracer, and Agent.__init__ then falls
    back to create_tracer(), which reads the developer's .env: audit on, trace
    pointed at langfuse with content capture. Without this fixture one run of
    the suite is indistinguishable from real usage in both streams.
    """
    monkeypatch.setenv("CORECODER_AUDIT_FILE", str(tmp_path / "audit.log"))
    # retention is a per-developer knob, so an inherited value would move the
    # file counts the pruning tests assert on
    monkeypatch.delenv("CORECODER_AUDIT_KEEP", raising=False)
    # a stderr sink outranks the file in audit._build_handler, so an inherited
    # switch here would send the lines past the redirect above
    monkeypatch.delenv("CORECODER_AUDIT_STREAM", raising=False)
    # the general logger gets its own file only when opted in, but that opt-in
    # belongs to the developer's shell, not to a test run
    monkeypatch.delenv("CORECODER_LOG_FILE", raising=False)
    # the langfuse backend ignores CORECODER_TRACE_FILE entirely, so redirecting
    # the path alone would still upload: only "off" keeps the suite out
    monkeypatch.setenv("CORECODER_TRACE", "off")
    monkeypatch.delenv("CORECODER_TRACE_FILE", raising=False)
    monkeypatch.delenv("CORECODER_TRACE_CONTENT", raising=False)
    # in case a test re-enables it: keep the pool separate from real runs
    monkeypatch.setenv("LANGFUSE_TRACING_ENVIRONMENT", "test")
    # both label the trace itself, and run_summary reads CORECODER_BUILD, so an
    # inherited value would put a developer's tag into the assertions about runs
    # that supplied no label at all
    monkeypatch.delenv("CORECODER_BUILD", raising=False)
    monkeypatch.delenv("CORECODER_USER", raising=False)
    # tool-search deferral is a runtime knob: an inherited value would hide MCP
    # schemas behind search_tools and change the eager path the suite asserts on
    monkeypatch.delenv("CORECODER_TOOL_SEARCH", raising=False)
    monkeypatch.delenv("CORECODER_TOOL_SEARCH_MIN_COUNT", raising=False)
    monkeypatch.delenv("CORECODER_TOOL_SEARCH_PCT", raising=False)
    # the sink is cached in module state, so re-pointing the env needs a reset
    audit.reset_audit()
    yield
    audit.reset_audit()


def get_tool(name: str):
    """Look up a tool by name."""
    for t in ALL_TOOLS:
        if t.name == name:
            return t
    return None


def repl_with(monkeypatch, agent, inputs, config=None):
    """Drive the real REPL with a scripted list of inputs. Returns the config in use."""
    it = iter(inputs)
    monkeypatch.setattr(cli, "pt_prompt", lambda *a, **k: next(it))
    config = config or Config.from_env()
    cli._repl(agent, config)
    return config
