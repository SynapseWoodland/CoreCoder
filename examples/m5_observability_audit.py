#!/usr/bin/env python3
"""M5 check: the audit and trace layers work against a real provider.

The base run drives one real tool call through the real Agent loop with
`CORECODER_AUDIT=1` and only the audit switch set (no CORECODER_LOG_LEVEL),
then asserts the metadata events:

    event=tool_decision decision=execute args_chars>0
    event=tool_result   effective_decision=execute outcome=ok duration_ms>0

With `--backend jsonl` (the default) it also parses the JSONL trace and asserts
the span tree (root -> generation/tool), the session id and the token count.
The sink is a temporary file, never ~/.corecoder and never the terminal.

Run (repo root):
    .venv\\Scripts\\python.exe examples\\m5_observability_audit.py
    .venv\\Scripts\\python.exe examples\\m5_observability_audit.py --backend langfuse
    .venv\\Scripts\\python.exe examples\\m5_observability_audit.py --drill
    .venv\\Scripts\\python.exe examples\\m5_observability_audit.py --degrade
    .venv\\Scripts\\python.exe examples\\m5_observability_audit.py --bench

Failure injection:
    --inject-backfill    make on_tool raise after the model asks for the tool;
                         the round is interrupted and `event=backfill count=1`
                         must appear, proving the backfill event is wired.
    --inject-bad-args    wrap the real LLM and inject one tool call whose JSON
                         never parsed; assert the tool did NOT run and the audit
                         records decision=bad_arguments / outcome=skipped.
    --inject-no-pricing  point the pricing table at an empty file, so the real
                         model has no known price; every cost field must be
                         None instead of a made-up zero.

Modes:
    default              one real tool call; audit + JSONL trace checks
    --drill              inject one 3 s tool and one broken call, then print a
                         timeline built only from the audit log and the trace
    --degrade            unwritable JSONL path / missing SDK / missing keys:
                         each must answer normally with exactly one warning
    --bench              20 scripted runs per tier: off, audit, audit+jsonl
    --backend langfuse   real Langfuse tracer; exits 2 when keys are missing
                         (this repo has no keys, so that path is not verified)

Exit codes:
    0  every enabled check passed
    1  a check failed
    2  no API key, required Langfuse keys, or the model did not call the tool
"""

import argparse
import json
import logging
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import ClassVar

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rich.console import Console
from rich.panel import Panel

from corecoder import llm as llm_module
from corecoder import observability
from corecoder.agent import Agent
from corecoder.config import Config
from corecoder.llm import LLM, LiteLLM, LLMResponse, ToolCall
from corecoder.permissions import Permission
from corecoder.tools.base import Tool
from corecoder.tracing import JsonlTracer, NullTracer, create_tracer

REPO_ROOT = Path(__file__).resolve().parent.parent
console = Console()

TOKEN = "PING_M5_TOKEN"
FAKE_SECRET = "sk-abcdefghijklmnop1234"


class EchoTool(Tool):
    name = "echo"
    description = "Echo the given text back to the caller."
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {"text": {"type": "string", "description": "text to echo"}},
        "required": ["text"],
    }

    def __init__(self):
        self.calls: list[str] = []

    def execute(self, text: str = "") -> str:
        self.calls.append(text)
        return f"echoed {text}"


class BulkTool(Tool):
    """A file-sized result that also contains a fake secret."""

    name = "bulk"
    description = "Return a large block of text for the caller."
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }

    def __init__(self):
        self.calls = 0

    def execute(self, text: str = "") -> str:
        self.calls += 1
        return f"{FAKE_SECRET}\n" + ("x" * 20_000) + "\nBULK_TAIL"


class SlowTool(Tool):
    name = "slow"
    description = "Sleep for three seconds and return."
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }

    def __init__(self):
        self.calls = 0

    def execute(self, text: str = "") -> str:
        self.calls += 1
        time.sleep(3)
        return f"slept on {text}"


class BadArgumentsLLM:
    """Delegates to the real LLM after injecting one unparseable tool call."""

    def __init__(self, inner):
        self.inner = inner
        self.injected = False

    def __getattr__(self, name):
        # cost/model are read off the LLM by the agent; keep them visible
        return getattr(self.inner, name)

    def chat(self, messages, tools=None, on_token=None, on_reasoning=None):
        if not self.injected:
            self.injected = True
            return LLMResponse(
                content="",
                tool_calls=[ToolCall(
                    id="bad-args-1",
                    name="echo",
                    arguments={},
                    raw_arguments='{"text": "cut',
                    arguments_error='{"text": "cut',
                )],
            )
        # no tools on the follow-up: the real provider still has to accept the
        # history, but it must not add a second (legitimate) echo call
        return self.inner.chat(
            messages=messages, tools=[],
            on_token=on_token, on_reasoning=on_reasoning,
        )


class InjectedCallsLLM:
    """Return injected tool calls first, then let the real provider finish."""

    def __init__(self, inner, calls: list[list[ToolCall]]):
        self.inner = inner
        self.calls = list(calls)

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def chat(self, messages, tools=None, on_token=None, on_reasoning=None):
        if self.calls:
            return LLMResponse(content="", tool_calls=self.calls.pop(0))
        return self.inner.chat(
            messages=messages, tools=[],
            on_token=on_token, on_reasoning=on_reasoning,
        )


class BenchLLM:
    """Alternates one echo call and one final answer."""

    model = "bench"
    total_prompt_tokens = 0
    total_completion_tokens = 0
    estimated_cost = None

    def __init__(self):
        self.turn = 0

    def chat(self, messages, tools=None, on_token=None, on_reasoning=None):
        self.turn += 1
        if self.turn % 2:
            return LLMResponse(
                content="",
                tool_calls=[ToolCall(id=f"b{self.turn}", name="echo",
                                     arguments={"text": "x"})],
                prompt_tokens=10, completion_tokens=5, usage_reported=True,
            )
        return LLMResponse(content="done", prompt_tokens=10,
                           completion_tokens=5, usage_reported=True)


def load_dotenv_from_repo() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(REPO_ROOT / ".env", override=False)


def build_llm():
    cfg = Config.from_env()
    if not cfg.api_key:
        console.print(f"[red]No API key.[/red] Put OPENAI_API_KEY in {REPO_ROOT / '.env'}")
        raise SystemExit(2)
    cls = LiteLLM if cfg.provider == "litellm" else LLM
    return cfg, cls(
        model=cfg.model,
        api_key=cfg.api_key,
        base_url=cfg.base_url,
        temperature=cfg.temperature,
        max_tokens=512,
    )


def section(title: str) -> None:
    console.print()
    console.rule(f"[bold]{title}[/bold]")


def lines(path: Path, event: str | None = None) -> list[str]:
    found = [ln for ln in path.read_text(encoding="utf-8").splitlines()
             if ln.startswith("event=")]
    if event is None:
        return found
    return [ln for ln in found if ln.startswith(f"event={event} ")]


def fields(line: str) -> dict:
    return dict(token.split("=", 1) for token in line.split() if "=" in token)


def trace_rows(path: Path | None) -> list[dict]:
    if path is None or not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def number(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def positive(value) -> bool:
    return number(value) > 0


def reset(path: Path) -> None:
    # drill/degrade modes never touch the switch, so enable it here too
    os.environ["CORECODER_AUDIT"] = "1"
    os.environ["CORECODER_AUDIT_FILE"] = str(path)
    observability.reset_audit()
    path.write_text("", encoding="utf-8")
    observability.configure_audit(file=path)


def history_valid(messages: list[dict]) -> bool:
    ids = [
        tc["id"]
        for m in messages
        if m.get("role") == "assistant"
        for tc in (m.get("tool_calls") or [])
    ]
    answers = [m["tool_call_id"] for m in messages if m.get("role") == "tool"]
    return sorted(ids) == sorted(answers)


def prompt_for_echo() -> str:
    return (
        f"Call the echo tool exactly once with text={TOKEN}. "
        "Do not ask questions and do not use any other tool. "
        "After the tool returns, reply with the single word DONE."
    )


def trace_fields(records: list[dict]) -> dict:
    root = next((r for r in records if r["name"] == "agent.chat"), None)
    if root is None:
        return {"root": False}
    trace = [r for r in records if r["trace_id"] == root["trace_id"]]
    generations = [r for r in trace if r["type"] == "generation"]
    tools = [r for r in trace if r["type"] == "tool"]
    first_gen = generations[0] if generations else {"attrs": {}}
    return {
        "root": True,
        "root_parent": root["parent_id"] is None,
        "session": root["session_id"],
        "rounds": sorted(r.get("round") for r in generations),
        "generation_parent": bool(
            generations and generations[0]["parent_id"] == root["span_id"]),
        "tool_parent": bool(tools and tools[0]["parent_id"] == root["span_id"]),
        "prompt_tokens": number(first_gen["attrs"].get("prompt_tokens")),
        "tool_duration": number(tools[0]["attrs"].get("duration_ms")) if tools else 0,
        "root_outcome": root["attrs"].get("outcome"),
        "root_input": root["attrs"].get("input"),
    }


def phase_events(llm, path: Path, trace_path: Path | None, tracer=None) -> dict:
    tool = EchoTool()
    if tracer is None and trace_path is not None:
        tracer = JsonlTracer(trace_path, capture_content=True)
    if tracer is None:
        tracer = NullTracer()
    agent = Agent(llm=llm, tools=[tool], permission=Permission(allow_all=True),
                  max_rounds=6, tracer=tracer)
    reset(path)
    agent.chat(prompt_for_echo())

    decisions = lines(path, "tool_decision")
    results = lines(path, "tool_result")
    if not decisions or not results:
        return {"called": False, "tool_calls": len(tool.calls),
                "valid": history_valid(agent.messages)}

    decision = fields(decisions[0])
    result = fields(results[0])
    calls = lines(path, "llm_call")
    summaries = lines(path, "run_summary")
    call = fields(calls[0]) if calls else {}
    summary = fields(summaries[-1]) if summaries else {}
    data = {
        "called": bool(tool.calls),
        "tool_calls": len(tool.calls),
        "decision": decision.get("decision"),
        "args_chars": int(decision.get("args_chars", "0")),
        "effective": result.get("effective_decision"),
        "outcome": result.get("outcome"),
        "duration": number(result.get("duration_ms")),
        "valid": history_valid(agent.messages),
        "usage_reported": call.get("usage_reported"),
        "prompt_tokens": number(call.get("prompt_tokens")),
        "call_cost": call.get("cost_usd"),
        "summary_outcome": summary.get("outcome"),
        "summary_tokens": number(summary.get("tokens_in")),
        "summary_cost": summary.get("cost_usd"),
        "session": agent.session_id,
    }
    if trace_path is not None:
        data["trace"] = trace_fields(trace_rows(trace_path))
    return data


def phase_backfill(llm, path: Path) -> dict:
    tool = EchoTool()
    agent = Agent(llm=llm, tools=[tool], permission=Permission(allow_all=True), max_rounds=6)

    def explode(name, arguments):
        raise RuntimeError("injected on_tool failure")

    reset(path)
    raised = False
    try:
        agent.chat(prompt_for_echo(), on_tool=explode)
    except RuntimeError:
        raised = True

    backfills = lines(path, "backfill")
    count = int(fields(backfills[0]).get("count", "0")) if backfills else 0
    return {"raised": raised, "count": count, "valid": history_valid(agent.messages),
            "tool_calls": len(tool.calls)}


def phase_bad_arguments(llm, path: Path) -> dict:
    tool = EchoTool()
    agent = Agent(llm=BadArgumentsLLM(llm), tools=[tool],
                  permission=Permission(allow_all=True), max_rounds=6)
    reset(path)
    agent.chat(f"Reply with the single word DONE. Context token: {TOKEN}")

    decisions = lines(path, "tool_decision")
    results = lines(path, "tool_result")
    decision = fields(decisions[0]) if decisions else {}
    result = fields(results[0]) if results else {}
    return {
        "decision": decision.get("decision"),
        "effective": result.get("effective_decision"),
        "outcome": result.get("outcome"),
        "tool_calls": len(tool.calls),
        "valid": history_valid(agent.messages),
    }


def phase_trace_public(llm, audit: Path, trace_path: Path) -> dict:
    """One clamped/redacted result and one rejected call in the same round."""
    bulk = BulkTool()
    calls = [
        [ToolCall(id="bulk-1", name="bulk", arguments={"text": "x"}),
         ToolCall(id="bad-1", name="bulk", arguments={},
                  raw_arguments='{"text": "cut',
                  arguments_error='{"text": "cut')],
    ]
    agent = Agent(
        llm=InjectedCallsLLM(llm, calls), tools=[bulk],
        permission=Permission(allow_all=True), max_rounds=6,
        tracer=JsonlTracer(trace_path, capture_content=True),
    )
    reset(audit)
    reply = agent.chat("Call the tools, then reply with the single word DONE.")

    records = trace_rows(trace_path)
    spans = {r["attrs"].get("call_id"): r for r in records if r["type"] == "tool"}
    bulk_attrs = spans.get("bulk-1", {"attrs": {}})["attrs"]
    bad_attrs = spans.get("bad-1", {"attrs": {}})["attrs"]
    return {
        "reply": reply,
        "bulk_effective": bulk_attrs.get("effective_decision"),
        "bulk_outcome": bulk_attrs.get("outcome"),
        "truncated": bulk_attrs.get("truncated"),
        "redacted": bulk_attrs.get("redacted"),
        "leaked": FAKE_SECRET in json.dumps(spans.get("bulk-1", {}), ensure_ascii=False),
        "bad_decision": bad_attrs.get("decision"),
        "bad_outcome": bad_attrs.get("outcome"),
        "valid": history_valid(agent.messages),
        "tool_calls": bulk.calls,
    }


def drill_report(workdir: Path, llm) -> dict:
    audit = workdir / "drill_audit.log"
    trace = workdir / "drill_trace.jsonl"
    slow = SlowTool()
    calls = [
        [ToolCall(id="slow-1", name="slow", arguments={"text": "drill"})],
        [ToolCall(id="bad-1", name="slow", arguments={},
                  raw_arguments='{"text": "cut', arguments_error='{"text": "cut')],
    ]
    agent = Agent(
        llm=InjectedCallsLLM(llm, calls), tools=[slow],
        permission=Permission(allow_all=True), max_rounds=6,
        tracer=JsonlTracer(trace),
    )
    reset(audit)
    agent.chat("Follow the injected calls, then reply with the single word DONE.")

    summary = lines(audit, "run_summary")
    summary_fields = fields(summary[-1]) if summary else {}
    spans = [r for r in trace_rows(trace) if r["type"] == "tool"]
    report = []
    for span in spans:
        attrs = span["attrs"]
        report.append({
            "round": span.get("round"),
            "tool": attrs.get("tool"),
            "decision": attrs.get("decision"),
            "effective": attrs.get("effective_decision"),
            "outcome": attrs.get("outcome"),
            "duration_ms": number(attrs.get("duration_ms")),
            "chars_in": attrs.get("chars_in"),
            "chars_out": attrs.get("chars_out"),
        })
    return {
        "rows": report,
        "valid": history_valid(agent.messages),
        "summary_outcome": summary_fields.get("outcome"),
        "tokens_in": number(summary_fields.get("tokens_in")),
        "cost_usd": summary_fields.get("cost_usd"),
        "slow_calls": slow.calls,
    }


class WarningSink(logging.Handler):
    def __init__(self):
        super().__init__(logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record):
        self.messages.append(record.getMessage())


def _build_and_run(llm, builder, sink):
    """Build the tracer and run one chat while warnings are captured.

    The tracer is built *inside* the guarded block: a degraded backend warns
    at construction time, which is exactly the warning under test.
    """
    logger = logging.getLogger("corecoder")
    logger.addHandler(sink)
    old_level, old_propagate = logger.level, logger.propagate
    logger.setLevel(logging.WARNING)
    logger.propagate = False
    try:
        tracer = builder()
        agent = Agent(llm=llm, tools=[], tracer=tracer, max_rounds=2)
        return tracer, agent.chat("Reply with the single word DONE.")
    finally:
        logger.removeHandler(sink)
        logger.setLevel(old_level)
        logger.propagate = old_propagate


def phase_degrade(llm, workdir: Path) -> list[dict]:
    results = []

    def record(name, builder):
        sink = WarningSink()
        tracer, reply = _build_and_run(llm, builder, sink)
        results.append({
            "name": name,
            "tracer": type(tracer).__name__,
            "reply": reply,
            "warnings": sum("event=trace_disabled" in m for m in sink.messages),
        })

    record("jsonl path is a directory",
           lambda: create_tracer("jsonl", file=str(workdir)))

    def missing_sdk():
        os.environ["LANGFUSE_PUBLIC_KEY"] = "pk-fake"
        os.environ["LANGFUSE_SECRET_KEY"] = "sk-fake"
        saved = sys.modules.get("langfuse", None)
        sys.modules["langfuse"] = None
        try:
            return create_tracer("langfuse")
        finally:
            if saved is None:
                sys.modules.pop("langfuse", None)
            else:
                sys.modules["langfuse"] = saved

    record("langfuse package missing", missing_sdk)

    def missing_keys():
        os.environ.pop("LANGFUSE_PUBLIC_KEY", None)
        os.environ.pop("LANGFUSE_SECRET_KEY", None)
        return create_tracer("langfuse")

    record("langfuse keys missing", missing_keys)
    return results


def phase_bench(workdir: Path) -> list[dict]:
    results = []
    tiers = (("off", False, False), ("audit", True, False),
             ("audit+jsonl", True, True))
    for name, audit_on, trace_on in tiers:
        stem = name.replace("+", "_")
        audit = workdir / f"bench_{stem}.audit.log"
        trace = workdir / f"bench_{stem}.jsonl"
        os.environ["CORECODER_AUDIT"] = "1" if audit_on else "0"
        os.environ["CORECODER_AUDIT_FILE"] = str(audit)
        os.environ["CORECODER_TRACE"] = "jsonl" if trace_on else "off"
        os.environ["CORECODER_TRACE_FILE"] = str(trace)
        observability.reset_audit()
        if audit_on:
            observability.configure_audit(file=audit)

        agent = Agent(llm=BenchLLM(), tools=[EchoTool()],
                      permission=Permission(allow_all=True), max_rounds=4)
        started = time.perf_counter()
        for _ in range(20):
            agent.chat("go")
        elapsed = time.perf_counter() - started
        observability.reset_audit()
        results.append({"name": name, "total_s": elapsed,
                        "per_chat_ms": elapsed / 20 * 1000,
                        "audit_bytes": audit.stat().st_size if audit.exists() else 0,
                        "trace_bytes": trace.stat().st_size if trace.exists() else 0})
    return results


def parse_args():
    parser = argparse.ArgumentParser(description="M5 audit + trace check")
    parser.add_argument("--backend", choices=["jsonl", "langfuse", "none"],
                        default="jsonl", help="trace backend for the default mode")
    parser.add_argument("--inject-backfill", action="store_true",
                        help="raise from on_tool and expect a backfill event")
    parser.add_argument("--inject-bad-args", action="store_true",
                        help="inject an unparseable tool call and expect a refusal")
    parser.add_argument("--inject-no-pricing", action="store_true",
                        help="empty the pricing table and expect cost_usd=None")
    parser.add_argument("--drill", action="store_true",
                        help="3 s tool + broken call timeline from audit and trace")
    parser.add_argument("--degrade", action="store_true",
                        help="broken sinks must answer normally with one warning")
    parser.add_argument("--bench", action="store_true",
                        help="20 scripted runs per tier: off / audit / audit+jsonl")
    return parser.parse_args()


def verdict(checks: list[tuple[str, bool, str]]) -> bool:
    section("verdict")
    ok = True
    for name, passed, actual in checks:
        console.print(f"  [{'green' if passed else 'red'}]"
                      f"{'PASS' if passed else 'FAIL'}[/] {name} (actual: {actual})")
        ok = ok and passed
    return ok


def run_drill(llm, workdir: Path) -> int:
    section("drill - 3 s tool + broken call, reconstructed from logs")
    result = drill_report(workdir, llm)
    for row in result["rows"]:
        console.print(
            f"  round {row['round']}: tool={row['tool']} "
            f"decision={row['decision']} effective={row['effective']} "
            f"outcome={row['outcome']} duration_ms={row['duration_ms']} "
            f"chars {row['chars_in']}->{row['chars_out']}")
    first = result["rows"][0] if result["rows"] else {}
    last = result["rows"][-1] if result["rows"] else {}
    checks = [
        ("two tool spans", len(result["rows"]) == 2, str(len(result["rows"]))),
        ("slow tool ran", result["slow_calls"] == 1, str(result["slow_calls"])),
        ("slow duration >= 2900 ms", first.get("duration_ms", 0) >= 2900,
         str(first.get("duration_ms"))),
        ("broken call skipped", last.get("outcome") == "skipped",
         str(last.get("outcome"))),
        ("history valid", result["valid"] is True, str(result["valid"])),
        ("run_summary outcome=answer",
         result["summary_outcome"] == "answer", str(result["summary_outcome"])),
        ("tokens reported", result["tokens_in"] > 0, str(result["tokens_in"])),
        ("cost known", result["cost_usd"] not in (None, "None"),
         str(result["cost_usd"])),
    ]
    ok = verdict(checks)
    console.print("")
    console.print("[green]PASS[/green] drill report complete." if ok
                  else "[red]FAIL[/red] drill checks did not hold.")
    console.print(f'cleanup: rm -rf "{workdir}"')
    return 0 if ok else 1


def run_degrade(llm, workdir: Path) -> int:
    section("degrade - broken sinks must not change the answer")
    results = phase_degrade(llm, workdir)
    for row in results:
        console.print(f"  {row['name']}: tracer={row['tracer']} "
                      f"warnings={row['warnings']} reply={row['reply'][:40]!r}")
    checks = [
        ("all tracers degraded to Null",
         all(r["tracer"] == "NullTracer" for r in results),
         str([r["tracer"] for r in results])),
        ("all answers unaffected",
         all("DONE" in (r["reply"] or "").upper() for r in results),
         str([r["reply"][:20] for r in results])),
        ("exactly one warning each",
         all(r["warnings"] == 1 for r in results),
         str([r["warnings"] for r in results])),
    ]
    ok = verdict(checks)
    console.print("")
    console.print("[green]PASS[/green] degradation is silent and safe." if ok
                  else "[red]FAIL[/red] degradation checks did not hold.")
    console.print(f'cleanup: rm -rf "{workdir}"')
    return 0 if ok else 1


def run_bench(workdir: Path) -> int:
    section("bench - scripted runs, three tiers")
    results = phase_bench(workdir)
    base = next(r["total_s"] for r in results if r["name"] == "off")
    for row in results:
        delta = (row["total_s"] - base) / base * 100 if base else 0
        console.print(
            f"  {row['name']:>12}: total {row['total_s']:.3f}s  "
            f"per chat {row['per_chat_ms']:.3f}ms  "
            f"delta vs off {delta:+.1f}%  "
            f"audit {row['audit_bytes']}B  trace {row['trace_bytes']}B")
    console.print("")
    console.print("[green]PASS[/green] bench completed (numbers are informational).")
    console.print(f'cleanup: rm -rf "{workdir}"')
    return 0


def main() -> int:
    args = parse_args()
    workdir = Path(tempfile.mkdtemp(prefix="corecoder_m5_"))

    if args.bench:
        return run_bench(workdir)

    load_dotenv_from_repo()
    cfg, llm = build_llm()

    pricing = workdir / "pricing.json"
    price = {} if args.inject_no_pricing else {cfg.model: [1.0, 2.0]}
    pricing.write_text(json.dumps(price), encoding="utf-8")
    llm_module.PRICING_FILE = pricing

    if args.drill:
        return run_drill(llm, workdir)
    if args.degrade:
        return run_degrade(llm, workdir)

    audit = workdir / "audit.log"
    # only the audit switch: no CORECODER_LOG_LEVEL, no stream opt-in
    os.environ["CORECODER_AUDIT"] = "1"
    os.environ["CORECODER_AUDIT_FILE"] = str(audit)

    tracer = None
    trace_path = None
    if args.backend == "langfuse":
        tracer = create_tracer("langfuse", capture_content=True)
        if isinstance(tracer, NullTracer):
            console.print("[red]Langfuse needs LANGFUSE_PUBLIC_KEY, "
                          "LANGFUSE_SECRET_KEY and LANGFUSE_HOST "
                          "(and `pip install -e .\\[langfuse]`).[/red]")
            return 2
    elif args.backend == "jsonl":
        trace_path = workdir / "trace.jsonl"
    else:
        tracer = NullTracer()

    console.print(Panel.fit(
        f"model: {cfg.model}\nbase: {cfg.base_url or 'default'}\n"
        f"pricing: {'none (cost must be None)' if args.inject_no_pricing else 'temp $1/$2 per Mtok'}\n"
        f"trace: {args.backend}\n"
        f"audit: {audit}",
        title="M5 - audit and trace against a real provider",
    ))

    checks: list[tuple[str, bool, str]] = []

    section("phase 1 - real tool call, audit + trace events")
    events = phase_events(llm, audit, trace_path, tracer)
    console.print(f"tool calls: {events.get('tool_calls')}  "
                  f"history valid: {events.get('valid')}")
    if not events.get("called"):
        console.print("")
        console.print("[yellow]The model did not call echo; retry or switch "
                      "CORECODER_MODEL.[/yellow]")
        console.print(f"temporary files kept at: {workdir}")
        console.print(f'cleanup: rm -rf "{workdir}"')
        return 2
    checks.append(("tool_decision decision=execute", events.get("decision") == "execute",
                   str(events.get("decision"))))
    checks.append(("tool_decision args_chars>0", events.get("args_chars", 0) > 0,
                   str(events.get("args_chars"))))
    checks.append(("tool_result effective_decision=execute",
                   events.get("effective") == "execute", str(events.get("effective"))))
    checks.append(("tool_result outcome=ok", events.get("outcome") == "ok",
                   str(events.get("outcome"))))
    checks.append(("tool_result duration_ms>0", events.get("duration", 0) > 0,
                   str(events.get("duration"))))
    checks.append(("history valid", events.get("valid") is True, str(events.get("valid"))))
    checks.append(("llm_call usage_reported=True",
                   events.get("usage_reported") == "True",
                   str(events.get("usage_reported"))))
    checks.append(("llm_call prompt_tokens>0", events.get("prompt_tokens", 0) > 0,
                   str(events.get("prompt_tokens"))))
    checks.append(("run_summary outcome=answer",
                   events.get("summary_outcome") == "answer",
                   str(events.get("summary_outcome"))))
    checks.append(("run_summary tokens_in>0", events.get("summary_tokens", 0) > 0,
                   str(events.get("summary_tokens"))))
    if args.inject_no_pricing:
        checks.append(("llm_call cost_usd=None", events.get("call_cost") == "None",
                       str(events.get("call_cost"))))
        checks.append(("run_summary cost_usd=None", events.get("summary_cost") == "None",
                       str(events.get("summary_cost"))))
    else:
        checks.append(("llm_call cost_usd>0", positive(events.get("call_cost")),
                       str(events.get("call_cost"))))
        checks.append(("run_summary cost_usd>0", positive(events.get("summary_cost")),
                       str(events.get("summary_cost"))))

    if trace_path is not None:
        trace = events.get("trace", {})
        checks.append(("trace root span", trace.get("root") is True,
                       str(trace.get("root"))))
        checks.append(("trace root has no parent", trace.get("root_parent") is True,
                       str(trace.get("root_parent"))))
        checks.append(("trace session matches agent",
                       trace.get("session") == events.get("session"),
                       f"{trace.get('session')} vs {events.get('session')}"))
        checks.append(("trace two generations", trace.get("rounds") == [1, 2],
                       str(trace.get("rounds"))))
        checks.append(("trace generation parented to root",
                       trace.get("generation_parent") is True,
                       str(trace.get("generation_parent"))))
        checks.append(("trace tool parented to root",
                       trace.get("tool_parent") is True, str(trace.get("tool_parent"))))
        checks.append(("trace prompt_tokens>0", trace.get("prompt_tokens", 0) > 0,
                       str(trace.get("prompt_tokens"))))
        checks.append(("trace tool duration>0", trace.get("tool_duration", 0) > 0,
                       str(trace.get("tool_duration"))))
        checks.append(("trace root outcome=answer",
                       trace.get("root_outcome") == "answer",
                       str(trace.get("root_outcome"))))
        checks.append(("trace captured user input",
                       trace.get("root_input") == prompt_for_echo(),
                       str(trace.get("root_input"))[:40]))
    else:
        tracer.flush()
        console.print("[green]Langfuse tracer flushed; inspect the host.[/green]")

    if args.inject_backfill:
        section("phase 2 - injected on_tool failure")
        backfill = phase_backfill(llm, audit)
        console.print(f"raised: {backfill['raised']}  backfill count: {backfill['count']}  "
                      f"history valid: {backfill['valid']}")
        checks.append(("KeyboardInterrupt-style escape surfaced", backfill["raised"],
                       str(backfill["raised"])))
        checks.append(("event=backfill count=1", backfill["count"] == 1,
                       str(backfill["count"])))
        checks.append(("history valid after backfill", backfill["valid"] is True,
                       str(backfill["valid"])))

    if args.inject_bad_args:
        section("phase 3 - injected unparseable arguments")
        bad = phase_bad_arguments(llm, audit)
        console.print(f"decision: {bad['decision']}  effective: {bad['effective']}  "
                      f"outcome: {bad['outcome']}  tool calls: {bad['tool_calls']}")
        checks.append(("decision=bad_arguments", bad["decision"] == "bad_arguments",
                       str(bad["decision"])))
        checks.append(("effective_decision=bad_arguments",
                       bad["effective"] == "bad_arguments", str(bad["effective"])))
        checks.append(("outcome=skipped", bad["outcome"] == "skipped", str(bad["outcome"])))
        checks.append(("tool never executed", bad["tool_calls"] == 0, str(bad["tool_calls"])))
        checks.append(("history valid after refusal", bad["valid"] is True, str(bad["valid"])))

    if trace_path is not None:
        section("phase 4 - oversized + secret result, and a rejected call")
        public = phase_trace_public(llm, audit, workdir / "trace_public.jsonl")
        console.print(f"reply: {public['reply'][:60]!r}")
        console.print(f"bulk: effective={public['bulk_effective']} "
                      f"outcome={public['bulk_outcome']} "
                      f"truncated={public['truncated']} redacted={public['redacted']}")
        console.print(f"rejected: decision={public['bad_decision']} "
                      f"outcome={public['bad_outcome']}")
        checks.append(("bulk executed", public["bulk_effective"] == "execute",
                       str(public["bulk_effective"])))
        checks.append(("bulk result truncated", public["truncated"] is True,
                       str(public["truncated"])))
        checks.append(("bulk result redacted", public["redacted"] is True,
                       str(public["redacted"])))
        checks.append(("raw secret absent from trace", public["leaked"] is False,
                       str(public["leaked"])))
        checks.append(("rejected call decision=bad_arguments",
                       public["bad_decision"] == "bad_arguments",
                       str(public["bad_decision"])))
        checks.append(("rejected call skipped", public["bad_outcome"] == "skipped",
                       str(public["bad_outcome"])))
        checks.append(("bulk ran exactly once", public["tool_calls"] == 1,
                       str(public["tool_calls"])))
        checks.append(("history valid after mixed round",
                       public["valid"] is True, str(public["valid"])))

    ok = verdict(checks)
    console.print("")
    if not ok:
        console.print("[red]FAIL[/red] one or more audit/trace checks did not hold.")
        console.print(f"audit log kept at: {audit}")
        if trace_path is not None:
            console.print(f"trace kept at: {trace_path}")
        console.print(f'cleanup: rm -rf "{workdir}"')
        return 1

    console.print("[green]PASS[/green] every enabled audit/trace check held "
                  "against the real provider.")
    console.print(f"audit log kept at: {audit}")
    if trace_path is not None:
        console.print(f"trace kept at: {trace_path}")
    console.print(f'cleanup: rm -rf "{workdir}"')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
