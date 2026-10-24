#!/usr/bin/env python3
"""M2 check: a truncated tool call is refused, not executed.

Real LLM, deliberately small max_tokens. The model starts writing a long tool
argument, the provider cuts the stream at the cap, and the loop has to notice
`finish_reason == "length"` before anything runs.

Before the fix, `_drain` dropped `finish_reason`, `json.loads` failed on the
half-written arguments, and the failure was silently replaced with `{}`. A
stand-in MCP tool (`execute(**kwargs)`, no required args, exactly like
`MCPTool.execute`) then ran with its defaults: a real side effect from a call
the user never saw completed. Built-in tools merely tripped
`inspect.signature.bind` and made the model burn rounds retrying.

After the fix this script must never observe an execution: the round is
refused, every tool_call id gets a `[truncated by max_tokens ...]` reply, and
two consecutive truncated rounds raise `TruncatedOutput` rather than returning
a string that the REPL, one-shot mode and sub-agents each drop on the floor.

Run (repo root):
    .venv\\Scripts\\python.exe examples\\m2_truncated_tool_call.py

Exit codes:
    0  the guard held (nothing executed, the caller was told why)
    1  the guard failed (a truncated call ran, or a check did not hold)
    2  this run never hit the cap on a tool call (model-dependent; re-run)

Where to look:
    llm.py    _drain                        captures finish_reason per chunk
    llm.py    the tool-call parse loop      records raw_arguments / arguments_error
    agent.py  _answer_truncated_tool_calls  refuses the round, pairs every id
    agent.py  _bad_arguments                second defence for any other path
"""

import sys
import tempfile
from pathlib import Path
from typing import ClassVar

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rich.console import Console
from rich.panel import Panel

from corecoder.agent import Agent, TruncatedOutput
from corecoder.config import Config
from corecoder.llm import LLM, LiteLLM
from corecoder.permissions import Permission
from corecoder.tools.base import Tool
from corecoder.tools.write import WriteFileTool

REPO_ROOT = Path(__file__).resolve().parent.parent
console = Console()


def load_dotenv_from_repo() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(REPO_ROOT / ".env", override=False)


def build_llm(max_tokens: int):
    cfg = Config.from_env()
    if not cfg.api_key:
        console.print(f"[red]No API key.[/red] Put OPENAI_API_KEY in {REPO_ROOT / '.env'}")
        raise SystemExit(2)
    cls = LiteLLM if cfg.provider == "litellm" else LLM
    llm = cls(
        model=cfg.model,
        api_key=cfg.api_key,
        base_url=cfg.base_url,
        temperature=cfg.temperature,
        max_tokens=max_tokens,
    )
    return cfg, llm


def section(title: str) -> None:
    console.print()
    console.rule(f"[bold]{title}[/bold]")


def brief(args: dict, limit: int = 160) -> str:
    text = ", ".join(f"{k}={v!r}" for k, v in args.items())
    return text if len(text) <= limit else text[:limit] + "..."


class NoteAppender(Tool):
    """Same shape as an MCP tool: MCPTool.execute(**kwargs) binds anything.

    Both parameters are optional on purpose. A truncated argument stream used to
    turn into {}, the signature accepts {}, and the call executed with defaults.
    That is exactly what the guard now refuses.
    """

    name = "mcp__lab__append_note"
    description = (
        "Append one note to the lab notebook. Arguments are optional, exactly "
        "like an MCP tool exposed through **kwargs."
    )
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Note text to append"},
            "path": {"type": "string", "description": "Notebook path"},
        },
        "required": [],
    }

    def __init__(self, notebook: Path):
        self.notebook = notebook
        self.calls: list[dict] = []

    def execute(self, text: str = "(empty arguments: nothing was provided)",
                path: str | None = None) -> str:
        target = Path(path) if path else self.notebook
        with target.open("a", encoding="utf-8") as f:
            f.write(text + "\n")
        self.calls.append({"text": text, "path": str(target)})
        return f"appended {len(text)} chars to {target}"


def last_assistant_arguments(agent: Agent) -> str:
    for m in reversed(agent.messages):
        if m.get("role") == "assistant" and m.get("tool_calls"):
            return m["tool_calls"][0]["function"]["arguments"]
    return "(no assistant tool_calls)"


def tool_replies(agent: Agent) -> list[str]:
    """Every role=tool content in the history, in order."""
    return [str(m.get("content", "")) for m in agent.messages if m.get("role") == "tool"]


def history_is_paired(messages: list[dict]) -> bool:
    """Every assistant tool_call id must have exactly one matching tool reply."""
    ids = [
        tc["id"]
        for m in messages
        if m.get("role") == "assistant"
        for tc in (m.get("tool_calls") or [])
    ]
    answers = [m["tool_call_id"] for m in messages if m.get("role") == "tool"]
    return sorted(ids) == sorted(answers)


def main() -> int:
    load_dotenv_from_repo()
    cfg, llm = build_llm(max_tokens=96)
    console.print(Panel.fit(
        f"model: {cfg.model}\nbase: {cfg.base_url or 'default'}\n"
        "max_tokens: deliberately tiny (adaptive: 512 -> 4096)",
        title="M2 - a truncated tool call is refused, not executed",
    ))

    workdir = Path(tempfile.mkdtemp(prefix="cc-m2-"))
    notebook = workdir / "lab_notes.txt"
    notebook.write_text("", encoding="utf-8")
    note_tool = NoteAppender(notebook)

    agent = Agent(
        llm=llm,
        tools=[note_tool, WriteFileTool()],
        permission=Permission(allow_all=True),
        max_rounds=3,
    )

    def on_tool(name, arguments):
        console.print(f"  [cyan]tool call:[/cyan] {name}({brief(arguments)})")

    # (cap, repeats): repeats controls how long the argument is. The pairs walk
    # up until one attempt lands a tool call whose JSON the provider cuts off.
    attempts = ((512, 800), (1024, 800), (1536, 800), (2048, 2000), (4096, 2000))
    hit = {"seen": False, "raised": "", "refusals": 0}

    section("phase 1 - ask for a long argument, cap the output")
    for cap, repeats in attempts:
        llm.extra["max_tokens"] = cap
        agent.reset()
        note_tool.calls.clear()
        before = llm.total_completion_tokens
        prompt = (
            f"Use mcp__lab__append_note exactly once, with no explanation before the call. "
            f"Set its text argument to the word 'pencil' repeated {repeats} times, "
            "separated by single spaces. Do not use any other tool."
        )
        console.print(f"[dim]--- max_tokens={cap}, repeats={repeats} ---[/dim]")
        try:
            reply = agent.chat(prompt, on_tool=on_tool)
        except TruncatedOutput as exc:
            # two truncated rounds in a row: the loop refuses to go on quietly
            hit.update(seen=True, raised=str(exc))
            break
        except Exception as exc:  # noqa: BLE001 - provider differences land here
            console.print(f"[red]request failed:[/red] {type(exc).__name__}: {exc}")
            continue

        used = llm.total_completion_tokens - before
        console.print(f"completion tokens this turn: {used} (cap {cap})")

        # the pre-fix symptom: a call nobody finished writing ran anyway
        if note_tool.calls:
            console.print("[red]FAIL[/red] the truncated call was executed anyway:")
            for call in note_tool.calls:
                console.print(f"  text={call['text'][:120]!r}")
            console.print("this is exactly what _answer_truncated_tool_calls removes")
            return 1

        refused = [r for r in tool_replies(agent) if "[truncated by max_tokens" in r]
        if refused:
            hit.update(seen=True, refusals=len(refused))
            break

        console.print(f"[yellow]no truncation and no execution either; reply was {reply[:160]!r}[/yellow]")
        console.print("[yellow]trying another cap[/yellow]")

    if not hit["seen"]:
        console.print("[yellow]This run never hit the output cap on a tool call.[/yellow]")
        console.print("That is provider/model dependent, so the guard went unexercised: "
                      "raise the repeats or lower the ladder and re-run.")
        console.print(f"[dim]workspace kept at {workdir}[/dim]")
        return 2

    section("phase 2 - what has to be true after the fix")
    checks = [
        ("the tool never ran", note_tool.calls == []),
        ("the notebook was not written", notebook.read_text(encoding="utf-8") == ""),
        ("every tool_call id is paired", history_is_paired(agent.messages)),
        ("the caller was told why", hit["refusals"] > 0 or bool(hit["raised"])),
    ]
    for label, ok in checks:
        console.print(f"  {'[green]PASS[/green]' if ok else '[red]FAIL[/red]'}  {label}")

    if hit["raised"]:
        console.print(f"\n[bold]raised TruncatedOutput[/bold]: {hit['raised']}")
        console.print("two consecutive truncated rounds stop the loop instead of returning "
                      "a string that the REPL drops and the sub-agent reads as success")
    else:
        console.print(f"\n[bold]{hit['refusals']} truncated call(s) refused[/bold] "
                      "with a re-issue hint; nothing executed")
    console.print(f"history recorded arguments as: {last_assistant_arguments(agent)}")
    console.print(f"[dim]workspace kept at {workdir}[/dim]")
    cost = llm.estimated_cost
    if cost is not None:
        console.print(f"[dim]tokens: {llm.total_prompt_tokens} prompt + "
                      f"{llm.total_completion_tokens} completion (~${cost:.4f})[/dim]")
    return 0 if all(ok for _, ok in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
