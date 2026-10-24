#!/usr/bin/env python3
"""M7 check: a rejected tool call shows both ends of its raw JSON.

When a provider truncates a call, the useful evidence (unterminated string,
dangling brace) is at the end. A head-only excerpt hides it, so the model
cannot see why its call was rejected.

phase 1 - real provider control
    The real model calls `echo` once. This proves key/model work and gives a
    baseline for a valid call.

phase 2 - injected truncated call (default)
    A wrapper returns one call whose raw JSON is longer than 200 chars and
    carries HEADMARK at the start and TAILMARK at the end, then delegates to
    the real provider with no tools. Expected: the tool never ran, the
    refusal quotes both markers plus "(N chars total)", the history pairs
    every call, and the real follow-up request succeeds.

phase 3 - --head-only
    Patch `Agent._bad_arguments` back to the old head-only excerpt. The same
    injection must now hide TAILMARK, proving the default check can fail and
    the fast path is what makes phase 2 pass.

Exit codes:
    0  every enabled check passed
    1  a check failed
    2  no API key, or the control call did not invoke the tool
"""

import argparse
import sys
from pathlib import Path
from typing import ClassVar

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel

from corecoder.agent import Agent
from corecoder.config import Config
from corecoder.llm import LLM, LiteLLM, LLMResponse, ToolCall
from corecoder.permissions import Permission
from corecoder.tools.base import Tool

REPO_ROOT = Path(__file__).resolve().parent.parent
console = Console()

TOKEN = "PING_M7_TOKEN"
HEAD_MARKER = "HEADMARK"
TAIL_MARKER = "TAILMARK"

# 120/80 head-tail split: HEADMARK sits in the head, TAILMARK past 200 chars
RAW = '{"text": "' + HEAD_MARKER + " " + "A" * 200 + " " + TAIL_MARKER + '"}'


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
    """Injects one unparseable call, then asks the real provider for an answer."""

    def __init__(self, inner, raw):
        self.inner = inner
        self.raw = raw
        self.injected = False

    def __getattr__(self, name):
        # keep model / estimated_cost visible to the agent
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
                    raw_arguments=self.raw,
                    arguments_error=self.raw,
                )],
            )
        # no tools on the follow-up: the real provider still has to accept the
        # history (refusal included), but it must not add a legit echo call
        # that would blur "the broken call never ran"
        followup = messages + [{
            "role": "user",
            "content": "The tool call above was rejected. Do not retry it; "
                       "reply with the single word DONE.",
        }]
        return self.inner.chat(
            messages=followup, tools=[],
            on_token=on_token, on_reasoning=on_reasoning,
        )


def old_bad_arguments(tc):
    """The pre-fix behavior: 200 chars from the head, tail discarded."""
    error = getattr(tc, "arguments_error", "")
    if not error:
        return None
    shown = error if len(error) <= 200 else error[:200] + "..."
    return (
        f"Error: bad arguments for {tc.name}: the arguments were cut off or "
        f"are not a valid JSON object ({shown!r}); the call was NOT executed. "
        "Re-issue it with one complete, smaller JSON object."
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
        return None
    cls = LiteLLM if cfg.provider == "litellm" else LLM
    return cls(
        model=cfg.model,
        api_key=cfg.api_key,
        base_url=cfg.base_url,
        temperature=cfg.temperature,
        max_tokens=512,
    )


def section(title: str) -> None:
    console.print()
    console.rule(f"[bold]{title}[/bold]")


def history_pairs(messages: list[dict]) -> bool:
    calls = sorted(
        tc["id"]
        for m in messages
        if m.get("role") == "assistant"
        for tc in (m.get("tool_calls") or [])
    )
    replies = sorted(m["tool_call_id"] for m in messages if m.get("role") == "tool")
    return calls == replies


def control_call(llm) -> dict:
    tool = EchoTool()
    agent = Agent(llm=llm, tools=[tool], permission=Permission(allow_all=True),
                  max_rounds=6)
    answer = agent.chat(
        f"Call the echo tool exactly once with text={TOKEN}. Do not ask "
        "questions. After the tool returns, reply with the single word DONE."
    )
    return {"calls": list(tool.calls), "answer": answer,
            "pairs": history_pairs(agent.messages)}


def injected_call(llm, head_only: bool) -> dict:
    tool = EchoTool()
    agent = Agent(llm=BadArgumentsLLM(llm, RAW), tools=[tool],
                  permission=Permission(allow_all=True), max_rounds=6)
    original = Agent.__dict__["_bad_arguments"] if head_only else None
    if head_only:
        Agent._bad_arguments = staticmethod(old_bad_arguments)
    try:
        answer = agent.chat(f"Reply with the single word DONE. Context: {TOKEN}")
    finally:
        if original is not None:
            Agent._bad_arguments = original
    refusal = next(
        (m["content"] for m in agent.messages if m.get("role") == "tool"), "")
    return {"answer": answer, "refusal": refusal, "calls": list(tool.calls),
            "pairs": history_pairs(agent.messages)}


def parse_args():
    parser = argparse.ArgumentParser(description="M7 rejected-argument diagnostics")
    parser.add_argument("--head-only", action="store_true",
                        help="restore the old 200-char head-only excerpt")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    load_dotenv_from_repo()
    llm = build_llm()
    if llm is None:
        return 2

    console.print(Panel.fit(
        f"model: {llm.model}\n"
        f"raw JSON: {len(RAW)} chars, {HEAD_MARKER} at the start, "
        f"{TAIL_MARKER} at the end\n"
        f"mode: {'head-only (old behavior)' if args.head_only else 'head+tail'}",
        title="M7 - rejected arguments show the cut, not just the start",
    ))

    checks: list[tuple[str, bool, str]] = []

    section("phase 1 - real provider control")
    control = control_call(llm)
    console.print(f"tool calls: {control['calls']}  history pairs: {control['pairs']}")
    if not control["calls"]:
        console.print("")
        console.print("[yellow]The model did not call echo; retry or switch "
                      "CORECODER_MODEL.[/yellow]")
        return 2
    checks.append(("control call executed once", control["calls"] == [TOKEN],
                   str(control["calls"])))
    checks.append(("control history pairs", control["pairs"], str(control["pairs"])))

    section("phase 2 - injected truncated call")
    result = injected_call(llm, head_only=args.head_only)
    console.print(escape(f"refusal: {result['refusal']}"))
    console.print(f"tool calls: {result['calls']}  answer: {result['answer'][:40]!r}  "
                  f"history pairs: {result['pairs']}")

    checks.append(("tool never executed", result["calls"] == [], str(result["calls"])))
    checks.append(("refusal quotes the head", HEAD_MARKER in result["refusal"],
                   str(HEAD_MARKER in result["refusal"])))
    checks.append((f"raw JSON is {len(RAW)} chars", len(RAW) > 200, str(len(RAW))))
    checks.append(("history pairs after refusal", result["pairs"], str(result["pairs"])))
    checks.append(("real follow-up request answered", bool(result["answer"].strip()),
                   repr(result["answer"][:40])))

    if args.head_only:
        checks.append(("head-only drops the tail (expected flip)",
                       TAIL_MARKER not in result["refusal"],
                       str(TAIL_MARKER in result["refusal"])))
    else:
        checks.append(("refusal quotes the tail", TAIL_MARKER in result["refusal"],
                       str(TAIL_MARKER in result["refusal"])))
        checks.append((f"refusal reports ({len(RAW)} chars total)",
                       f"({len(RAW)} chars total)" in result["refusal"],
                       repr(f"({len(RAW)} chars total)")))

    section("verdict")
    ok = True
    for name, passed, actual in checks:
        console.print(f"  [{'green' if passed else 'red'}]{'PASS' if passed else 'FAIL'}[/] "
                      f"{name} (actual: {actual})")
        ok = ok and passed

    console.print("")
    if not ok:
        console.print("[red]FAIL[/red] one or more diagnostics checks did not hold.")
        return 1
    console.print("[green]PASS[/green] every enabled diagnostics check held.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
