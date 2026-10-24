#!/usr/bin/env python3
"""M6 check: Ctrl+C returns immediately from a parallel tool batch.

A tool call that is already running cannot be killed (Python threads cannot be
interrupted), but the agent must stop waiting for it, cancel the calls still
queued, and keep the history valid. This script proves that against the real
Agent loop.

phase 1 - real provider preflight
    One tiny real request proves the key/model work. Without it, exit 2.

phase 2 - deterministic injection (default)
    A scripted turn asks for two tools in one round: `fast_interrupt` raises
    KeyboardInterrupt inside its worker, `slow_wait` blocks for 2s. Every
    worker exception is re-raised by `future.result()` on the main thread, so
    this reproduces a Ctrl+C portably, with no signal tricks. Expected:
    chat() raises in <1.0s, every tool_call is paired with an `[interrupted]`
    reply, the slow call had NOT finished, and `event=batch_interrupted`
    reports submitted=2 inflight>=1.

phase 3 - --signal
    Two slow tools instead, and a threading.Timer calls
    `_thread.interrupt_main()` after 0.3s: a real Ctrl+C delivered to the main
    thread from a child thread (works on Windows, unlike signal.raise_signal).
    Same upper bound on elapsed time.

phase 4 - --blocking-shutdown
    Patch the pool so shutdown waits (the old `with ThreadPoolExecutor`
    behavior). The same worker injection must now take >=2s: the fast path is
    what makes phase 2 drop, not a lucky sleep.

Exit codes:
    0  every enabled check passed
    1  a check failed
    2  no API key, or the real provider preflight failed
"""

import _thread
import argparse
import concurrent.futures
import contextlib
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import ClassVar

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel

from corecoder.agent import Agent
from corecoder.config import Config
from corecoder.demo import ScriptedLLM
from corecoder.llm import LLM, LiteLLM, LLMResponse, ToolCall
from corecoder.observability import configure_audit
from corecoder.permissions import Permission
from corecoder.tools.base import Tool

REPO_ROOT = Path(__file__).resolve().parent.parent
FAST_BOUND = 1.0
SLOW_SECONDS = 2.0
SIGNAL_DELAY = 0.3

console = Console()


class FastInterruptTool(Tool):
    name = "fast_interrupt"
    description = "Raise KeyboardInterrupt inside the worker."
    parameters: ClassVar[dict] = {"type": "object", "properties": {}}

    def execute(self, **kwargs):
        raise KeyboardInterrupt


class SlowTool(Tool):
    name = "slow_wait"
    description = "Block until released or the timeout expires."
    parameters: ClassVar[dict] = {"type": "object", "properties": {}}

    def __init__(self, release: threading.Event, seconds: float, name="slow_wait"):
        self.release = release
        self.seconds = seconds
        self.name = name
        self.finished = False

    def execute(self, **kwargs):
        released = self.release.wait(self.seconds)
        self.finished = True
        return f"slow finished (released={released})"


def two_call_response(names: list[str]) -> LLMResponse:
    return LLMResponse(
        content="",
        tool_calls=[
            ToolCall(id=f"call-{i}", name=name, arguments={})
            for i, name in enumerate(names)
        ],
    )


def history_pairs(messages: list[dict]) -> bool:
    calls = sorted(
        tc["id"]
        for m in messages
        if m.get("role") == "assistant"
        for tc in (m.get("tool_calls") or [])
    )
    replies = sorted(m["tool_call_id"] for m in messages if m.get("role") == "tool")
    return calls == replies


@contextlib.contextmanager
def blocking_pool():
    """Restore the old with-block semantics: shutdown(wait=True)."""
    original = concurrent.futures.ThreadPoolExecutor

    class BlockingPool(original):
        def shutdown(self, wait=True, cancel_futures=False):
            super().shutdown(wait=True, cancel_futures=False)

    concurrent.futures.ThreadPoolExecutor = BlockingPool
    try:
        yield
    finally:
        concurrent.futures.ThreadPoolExecutor = original


def run_batch(mode: str, blocking: bool) -> dict:
    """mode: worker (fast raises) or signal (timer interrupts main thread)."""
    release = threading.Event()
    if mode == "worker":
        tools = [FastInterruptTool(), SlowTool(release, SLOW_SECONDS)]
    else:
        tools = [
            SlowTool(release, SLOW_SECONDS, name="slow_a"),
            SlowTool(release, SLOW_SECONDS, name="slow_b"),
        ]
    slow = tools[-1]
    agent = Agent(
        llm=ScriptedLLM([two_call_response([t.name for t in tools])]),
        tools=tools,
        permission=Permission(allow_all=True),
        max_rounds=2,
    )

    timer = None
    if mode == "signal":
        timer = threading.Timer(SIGNAL_DELAY, _thread.interrupt_main)
        timer.start()

    raised = False
    started = time.perf_counter()
    try:
        with contextlib.ExitStack() as stack:
            if blocking:
                stack.enter_context(blocking_pool())
            try:
                agent.chat("go")
            except KeyboardInterrupt:
                raised = True
    finally:
        if timer is not None:
            timer.cancel()
        elapsed = time.perf_counter() - started
        finished_at_return = slow.finished
        release.set()

    replies = [m["content"] for m in agent.messages if m.get("role") == "tool"]
    return {
        "raised": raised,
        "elapsed": elapsed,
        "finished_at_return": finished_at_return,
        "pairs": history_pairs(agent.messages),
        "replies": replies,
    }


def audit_event(path: Path, event: str) -> dict:
    """Newest matching event, parsed into fields."""
    if not path.exists():
        return {}
    matches = [
        line for line in path.read_text(encoding="utf-8").splitlines()
        if line.startswith(f"event={event} ")
    ]
    if not matches:
        return {}
    return dict(token.split("=", 1) for token in matches[-1].split() if "=" in token)


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


def parse_args():
    parser = argparse.ArgumentParser(description="M6 Ctrl+C cancellation check")
    parser.add_argument("--signal", action="store_true",
                        help="simulate a real Ctrl+C via _thread.interrupt_main")
    parser.add_argument("--blocking-shutdown", action="store_true",
                        help="restore shutdown(wait=True) and expect a >=2s block")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    load_dotenv_from_repo()
    llm = build_llm()
    if llm is None:
        return 2

    workdir = Path(tempfile.mkdtemp(prefix="corecoder_m6_"))
    audit = workdir / "audit.log"
    os.environ["CORECODER_AUDIT"] = "1"
    configure_audit(file=audit)

    console.print(Panel.fit(
        f"model: {llm.model}\n"
        f"mode: {'signal' if args.signal else 'worker interrupt'}\n"
        f"shutdown: {'blocking (old behavior)' if args.blocking_shutdown else 'non-blocking'}\n"
        f"audit: {audit}",
        title="M6 - Ctrl+C must not wait for in-flight parallel tools",
    ))

    checks: list[tuple[str, bool, str]] = []

    section("phase 1 - real provider preflight")
    try:
        warm = Agent(llm=llm, tools=[], max_rounds=2)
        reply = warm.chat("Reply with the single word OK.")
    except Exception as e:  # noqa: BLE001 - any provider failure is an env problem
        console.print(f"[red]Real provider call failed:[/red] {type(e).__name__}: {e}")
        return 2
    preview = (reply or "").strip().replace("\n", " ")[:80]
    console.print(f"reply: {preview!r}")
    checks.append(("real provider replied", bool(preview), repr(preview)))
    if not preview:
        console.print("[red]The provider returned nothing; cannot continue.[/red]")
        return 2

    if args.blocking_shutdown:
        section("phase 2 - old with-block behavior (must block)")
        result = run_batch("worker", blocking=True)
        checks.append(("KeyboardInterrupt surfaced", result["raised"], str(result["raised"])))
        checks.append((f"elapsed >= {SLOW_SECONDS:.0f}s (blocking)", result["elapsed"] >= SLOW_SECONDS,
                       f"{result['elapsed']:.3f}s"))
        checks.append(("slow tool finished before return",
                       result["finished_at_return"], str(result["finished_at_return"])))
        checks.append(("history pairs every call", result["pairs"], str(result["pairs"])))
    elif args.signal:
        section("phase 2 - real Ctrl+C via _thread.interrupt_main")
        result = run_batch("signal", blocking=False)
        checks.append(("KeyboardInterrupt surfaced", result["raised"], str(result["raised"])))
        checks.append((f"elapsed < {FAST_BOUND:.1f}s", result["elapsed"] < FAST_BOUND,
                       f"{result['elapsed']:.3f}s"))
        checks.append(("history pairs every call", result["pairs"], str(result["pairs"])))
        checks.append(("every reply is the interrupted placeholder",
                       result["replies"] == ["[interrupted]"] * 2,
                       escape(str(result["replies"]))))
        event = audit_event(audit, "batch_interrupted")
        console.print(f"batch_interrupted: {event or '(missing)'}")
    else:
        section("phase 2 - worker KeyboardInterrupt, fast path")
        result = run_batch("worker", blocking=False)
        checks.append(("KeyboardInterrupt surfaced", result["raised"], str(result["raised"])))
        checks.append((f"elapsed < {FAST_BOUND:.1f}s", result["elapsed"] < FAST_BOUND,
                       f"{result['elapsed']:.3f}s"))
        checks.append(("slow call had NOT finished", not result["finished_at_return"],
                       str(result["finished_at_return"])))
        checks.append(("history pairs every call", result["pairs"], str(result["pairs"])))
        checks.append(("every reply is the interrupted placeholder",
                       result["replies"] == ["[interrupted]"] * 2,
                       escape(str(result["replies"]))))
        event = audit_event(audit, "batch_interrupted")
        console.print(f"batch_interrupted: {event or '(missing)'}")
        checks.append(("event=batch_interrupted submitted=2",
                       event.get("submitted") == "2", str(event.get("submitted"))))
        checks.append(("event=batch_interrupted inflight>=1",
                       int(event.get("inflight", "0")) >= 1, str(event.get("inflight"))))

    console.print("")
    console.print(f"elapsed: {result['elapsed']:.3f}s  raised: {result['raised']}  "
                  f"pairs: {result['pairs']}")
    console.print(escape(f"tool replies: {result['replies']}"))

    section("verdict")
    ok = True
    for name, passed, actual in checks:
        console.print(f"  [{'green' if passed else 'red'}]{'PASS' if passed else 'FAIL'}[/] "
                      f"{name} (actual: {actual})")
        ok = ok and passed

    console.print("")
    if not ok:
        console.print("[red]FAIL[/red] one or more interruption checks did not hold.")
        console.print(f"audit log kept at: {audit}")
        console.print(f'cleanup: rm -rf "{workdir}"')
        return 1

    console.print("[green]PASS[/green] every enabled interruption check held.")
    console.print(f"audit log kept at: {audit}")
    console.print(f'cleanup: rm -rf "{workdir}"')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
