#!/usr/bin/env python3
"""M3 check: a context-length 400 is collapsed and retried exactly once.

Real LLM, real provider 400. Local accounting is deliberately set far above
the provider window (`max_context_tokens=20_000_000`), which is what a wrong
CORECODER_MAX_CONTEXT, a model swap, or the uncounted fixed overhead (system
prompt + tool schemas) looks like in production. The oversized history reaches
the provider and is rejected; the fix catches that rejection, force-collapses
the history and re-issues the request once.

The summary call inside the collapse is stubbed so the LLM call count stays
deterministic; both top-level requests are real.

Run (repo root):
    .venv\\Scripts\\python.exe examples\\m3_context_overflow.py

Exit codes:
    0  recovered: one collapse, one retry, answer 'OK'
    1  not recovered, or the counts are wrong
    2  no API key, the provider silently accepted the oversized request, or
       the model did not answer the tiny prompt

Failure injection:
    --inject-400   force the context-length 400 through llm._call_with_retry.
                   Two are injected on purpose: the first is absorbed by
                   LLM.chat's stream_options fallback, the second escapes to
                   the agent. The retry after them is a real request.
    --no-inject    never inject: a lenient provider makes the script exit 2
                   instead of walking the recovery path.
    The inverse check is to comment out the huge `block` history: no 400 and
    no collapse should happen.

Cleanup:
    This script writes no files and persists nothing; no cleanup is required.
"""

import argparse
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rich.console import Console
from rich.panel import Panel

from corecoder.agent import Agent
from corecoder.config import Config
from corecoder.context import estimate_tokens
from corecoder.llm import LLM, LiteLLM, is_context_length_error
from corecoder.permissions import Permission
from corecoder.tools import ALL_TOOLS

REPO_ROOT = Path(__file__).resolve().parent.parent
console = Console()

# no punctuation and no CJK: keeps the local estimator fast and predictable
FILLER = "lorem ipsum dolor sit amet consectetur adipiscing elit "
# 90_000 repeats -> ~4.9M chars -> ~1.45M estimated tokens, more than any
# 1M-token window and far more than the common 128k/200k/400k ones
FACTOR = 90_000


def load_dotenv_from_repo() -> None:
    """Load .env from the repo root regardless of the working directory."""
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
        max_tokens=16,  # the probe only needs a token or two of output
    )


def section(title: str) -> None:
    console.print()
    console.rule(f"[bold]{title}[/bold]")


def context_400():
    """The 400 shape a real OpenAI-compatible provider returns for overflow."""
    try:
        import httpx
    except ModuleNotFoundError:
        import httpx2 as httpx

    from openai import BadRequestError

    request = httpx.Request("POST", "https://injected.invalid/v1/chat/completions")
    response = httpx.Response(400, request=request)
    message = (
        "Error code: 400 - {'error': {'message': \"This model's maximum context "
        "length is 128000 tokens. However, your messages resulted in 2264718 "
        "tokens. Please reduce the length of the messages.\", "
        "'type': 'invalid_request_error'}}"
    )
    return BadRequestError(message, response=response, body=None)


def seed_messages() -> list[dict]:
    """A huge first message plus enough small turns that collapse keeps only
    the tail (len > 4) and drops the oversized block."""
    return [
        {"role": "user", "content": "Reference material, ignore its content:\n" + FILLER * FACTOR},
        {"role": "assistant", "content": "Noted."},
        {"role": "user", "content": "small 1"},
        {"role": "assistant", "content": "small 2"},
        {"role": "user", "content": "small 3"},
        {"role": "assistant", "content": "small 4"},
        {"role": "user", "content": "small 5"},
        {"role": "assistant", "content": "small 6"},
    ]


def instrument(llm, inject_400: bool) -> dict:
    """Count top-level LLM.chat calls and context-length rejections.

    Injection deliberately fires twice: LLM.chat answers the first 400 by
    dropping stream_options and retrying, so one injected 400 never reaches
    the agent. The third call is always real.
    """
    original_call = llm._call_with_retry
    original_chat = llm.chat
    state = {"chat_calls": 0, "rejections": 0, "injected": 0}

    def wrapped_call(params, max_retries=3):
        if inject_400 and state["injected"] < 2:
            state["injected"] += 1
            state["rejections"] += 1
            raise context_400()
        try:
            return original_call(params, max_retries=max_retries)
        except Exception as exc:
            if is_context_length_error(exc):
                state["rejections"] += 1
            raise

    def wrapped_chat(*args, **kwargs):
        state["chat_calls"] += 1
        return original_chat(*args, **kwargs)

    llm._call_with_retry = wrapped_call
    llm.chat = wrapped_chat
    return state


def history_valid(messages: list[dict]) -> bool:
    """Every assistant tool_call id must have exactly one matching tool reply."""
    ids = [
        tc["id"]
        for m in messages
        if m.get("role") == "assistant"
        for tc in (m.get("tool_calls") or [])
    ]
    answers = [m["tool_call_id"] for m in messages if m.get("role") == "tool"]
    return sorted(ids) == sorted(answers)


def parse_args():
    parser = argparse.ArgumentParser(description="M3 context-overflow recovery check")
    parser.add_argument("--inject-400", action="store_true",
                        help="force the context-length 400 instead of relying on the provider")
    parser.add_argument("--no-inject", action="store_true",
                        help="never inject; exit 2 if the provider accepts oversized input")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    load_dotenv_from_repo()
    cfg, llm = build_llm()
    console.print(Panel.fit(
        f"model: {cfg.model}\nbase: {cfg.base_url or 'default'}",
        title="M3 - context-length rejection -> collapse once -> retry once",
    ))

    probe = Agent(
        llm=llm,
        tools=list(ALL_TOOLS),
        permission=Permission(allow_all=True),
        max_context_tokens=20_000_000,
    )
    fixed = probe._fixed_prompt_tokens()
    messages = seed_messages()
    estimated = estimate_tokens(messages)
    section("the request the local estimate cannot see")
    console.print(f"fixed prompt tokens (system + tool schemas): {fixed}")
    console.print(f"history tokens (before the new prompt): {estimated}")
    console.print(f"fixed share: {fixed / (estimated + fixed):.4%} of the request")

    agent = Agent(
        llm=llm,
        tools=list(ALL_TOOLS),
        permission=Permission(allow_all=True),
        max_context_tokens=20_000_000,  # pretend the local budget knows no limit
        max_rounds=2,
    )
    agent.messages = messages
    state = instrument(llm, inject_400=args.inject_400 and not args.no_inject)

    section("one request - real provider unless --inject-400")
    with mock.patch.object(agent.context, "_get_summary", return_value="[scripted summary]"), \
            mock.patch.object(agent.context, "force_collapse",
                              wraps=agent.context.force_collapse) as collapse:
        error = None
        answer = ""
        try:
            answer = agent.chat("Answer with exactly: OK")
        except Exception as exc:  # noqa: BLE001 - the failure is the evidence
            error = exc

    context_rejection = state["rejections"] > 0
    answered_ok = answer.strip().upper().startswith("OK")
    valid = history_valid(agent.messages)

    section("verdict")
    console.print(f"context rejection: {context_rejection}")
    console.print(f"injected 400: {state['injected'] > 0}")
    console.print(f"collapse calls: {collapse.call_count}")
    console.print(f"chat calls: {state['chat_calls']}")
    console.print(f"answer: {answer!r}")
    console.print(f"history valid: {valid}")
    console.print(f"messages after: {len(agent.messages)}")
    if error is not None:
        console.print(f"[red]chat() raised:[/red] {type(error).__name__}: {error}")

    if not context_rejection and not args.inject_400:
        console.print("")
        console.print("[yellow]This provider accepted the oversized request, so the "
                      "recovery path was not exercised.[/yellow]")
        console.print("Re-run with --inject-400 to walk it deterministically.")
        return 2

    recovered = (
        context_rejection
        and collapse.call_count == 1
        and state["chat_calls"] == 2
        and answered_ok
        and valid
        and error is None
    )
    if not recovered:
        console.print("")
        console.print("[red]FAIL[/red] expected one collapse, two LLM calls and answer 'OK'.")
        return 1

    console.print("")
    console.print("[green]PASS[/green] the provider rejection was absorbed by one "
                  "forced collapse and one retry.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
