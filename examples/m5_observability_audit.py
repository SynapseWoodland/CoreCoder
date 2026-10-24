#!/usr/bin/env python3
"""M5 check: the audit layer works against a real provider.

The base run drives one real tool call through the real Agent loop with
`CORECODER_AUDIT=1` and only the audit switch set (no CORECODER_LOG_LEVEL),
then asserts the metadata events:

    event=tool_decision decision=execute args_chars>0
    event=tool_result   effective_decision=execute outcome=ok duration_ms>0

The audit sink is a temporary file, never ~/.corecoder and never the terminal.

Run (repo root):
    .venv\\Scripts\\python.exe examples\\m5_observability_audit.py

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

Exit codes:
    0  every enabled check passed
    1  a check failed
    2  no API key, or the model did not call the tool (retry / change model)
"""

import argparse
import json
import os
import sys
import tempfile
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

REPO_ROOT = Path(__file__).resolve().parent.parent
console = Console()

TOKEN = "PING_M5_TOKEN"


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


class BadArgumentsLLM:
    """Delegates to the real LLM after injecting one unparseable tool call."""

    def __init__(self, inner):
        self.inner = inner
        self.injected = False

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


def number(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def positive(value) -> bool:
    return number(value) > 0


def reset(path: Path) -> None:
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


def phase_events(llm, path: Path) -> dict:
    tool = EchoTool()
    agent = Agent(llm=llm, tools=[tool], permission=Permission(allow_all=True), max_rounds=6)
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
    return {
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
    }


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


def parse_args():
    parser = argparse.ArgumentParser(description="M5 audit-layer check")
    parser.add_argument("--inject-backfill", action="store_true",
                        help="raise from on_tool and expect a backfill event")
    parser.add_argument("--inject-bad-args", action="store_true",
                        help="inject an unparseable tool call and expect a refusal")
    parser.add_argument("--inject-no-pricing", action="store_true",
                        help="empty the pricing table and expect cost_usd=None")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    load_dotenv_from_repo()
    cfg, llm = build_llm()

    workdir = Path(tempfile.mkdtemp(prefix="corecoder_m5_"))
    audit = workdir / "audit.log"
    pricing = workdir / "pricing.json"
    price = {} if args.inject_no_pricing else {cfg.model: [1.0, 2.0]}
    pricing.write_text(json.dumps(price), encoding="utf-8")
    llm_module.PRICING_FILE = pricing

    # only the audit switch: no CORECODER_LOG_LEVEL, no stream opt-in
    os.environ["CORECODER_AUDIT"] = "1"
    os.environ["CORECODER_AUDIT_FILE"] = str(audit)

    console.print(Panel.fit(
        f"model: {cfg.model}\nbase: {cfg.base_url or 'default'}\n"
        f"pricing: {'none (cost must be None)' if args.inject_no_pricing else 'temp $1/$2 per Mtok'}\n"
        f"audit: {audit}",
        title="M5 - structured audit against a real provider",
    ))

    checks: list[tuple[str, bool, str]] = []

    section("phase 1 - real tool call, audit events")
    events = phase_events(llm, audit)
    console.print(f"tool calls: {events.get('tool_calls')}  history valid: {events.get('valid')}")
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

    section("verdict")
    ok = True
    for name, passed, actual in checks:
        console.print(f"  [{'green' if passed else 'red'}]{'PASS' if passed else 'FAIL'}[/] "
                      f"{name} (actual: {actual})")
        ok = ok and passed

    console.print("")
    if not ok:
        console.print("[red]FAIL[/red] one or more audit checks did not hold.")
        console.print(f"audit log kept at: {audit}")
        console.print(f'cleanup: rm -rf "{workdir}"')
        return 1

    console.print("[green]PASS[/green] every enabled audit check held against the real provider.")
    console.print(f"audit log kept at: {audit}")
    console.print(f'cleanup: rm -rf "{workdir}"')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
