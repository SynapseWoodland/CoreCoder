"""Interactive REPL - the user-facing terminal interface."""

import argparse
import os
import sys

from prompt_toolkit import prompt as pt_prompt
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

from . import __version__
from .agent import Agent, BudgetExceeded, TruncatedOutput
from .audit import configure_logging
from .config import Config
from .hooks import load_hooks
from .llm import LLM, LiteLLM
from .mcp import load_mcp_tools
from .permissions import DEFAULT_STORE, Permission
from .session import list_sessions, load_session_record, save_session
from .tools import ALL_TOOLS

console = Console()
err_console = Console(stderr=True)


def _parse_args():
    p = argparse.ArgumentParser(
        prog="corecoder",
        description="Minimal AI coding agent. Works with any OpenAI-compatible LLM.",
    )
    p.add_argument("-m", "--model", help="Model name (default: $CORECODER_MODEL or gpt-5.5)")
    p.add_argument("--base-url", help="API base URL (default: $OPENAI_BASE_URL)")
    p.add_argument("--api-key", help="API key (default: $OPENAI_API_KEY)")
    p.add_argument("-p", "--prompt", help="One-shot prompt (non-interactive mode)")
    p.add_argument("--yes", action="store_true", help="Auto-approve every tool call (for scripts and CI)")
    p.add_argument("--demo", action="store_true", help="Run the offline scripted demo (no API key needed)")
    p.add_argument("-r", "--resume", metavar="ID", help="Resume a saved session")
    p.add_argument("-v", "--version", action="version", version=f"%(prog)s {__version__}")
    return p.parse_args()


def main():
    configure_logging()
    args = _parse_args()

    if args.demo:
        from .demo import run_demo
        raise SystemExit(run_demo())

    config = Config.from_env()
    # .env is loaded above, so re-read CORECODER_LOG_* now that it is available
    configure_logging()

    # CLI args override env vars
    if args.model:
        config.model = args.model
    if args.base_url:
        config.base_url = args.base_url
    if args.api_key:
        config.api_key = args.api_key

    if not config.api_key:
        console.print("[red bold]No API key found.[/]")
        console.print(
            "Set one of: OPENAI_API_KEY, DEEPSEEK_API_KEY, or CORECODER_API_KEY\n"
            "\nExamples:\n"
            "  # OpenAI\n"
            "  export OPENAI_API_KEY=sk-...\n"
            "\n"
            "  # DeepSeek\n"
            "  export OPENAI_API_KEY=sk-... OPENAI_BASE_URL=https://api.deepseek.com\n"
            "\n"
            "  # Ollama (local)\n"
            "  export OPENAI_API_KEY=ollama OPENAI_BASE_URL=http://localhost:11434/v1 CORECODER_MODEL=qwen2.5-coder\n"
        )
        sys.exit(1)

    llm_cls = LiteLLM if config.provider == "litellm" else LLM
    fallback = [m.strip() for m in config.fallback_models.split(",") if m.strip()]
    llm = llm_cls(
        model=config.model,
        api_key=config.api_key,
        base_url=config.base_url,
        temperature=config.temperature,
        max_tokens=config.max_tokens,
        fallback_models=fallback,
    )
    # consent layer: ask in the REPL, refuse in one-shot mode, --yes skips it
    if args.yes:
        permission = Permission(allow_all=True)
    elif args.prompt:
        permission = Permission()
    else:
        permission = Permission(ask=_ask_permission, persist_path=DEFAULT_STORE)
    agent = Agent(
        llm=llm,
        tools=[*ALL_TOOLS, *load_mcp_tools()],
        max_context_tokens=config.max_context_tokens,
        permission=permission,
        hooks=load_hooks(),
        budget_ceiling=config.budget_ceiling,
    )

    # resume saved session
    if args.resume:
        record = load_session_record(args.resume)
        if record:
            agent.messages = record["messages"]
            agent.session_id = record.get("id", agent.session_id)
            loaded_model = record["model"]
            # restore the model from the saved session unless overridden by CLI
            if not args.model:
                agent.llm.model = loaded_model
                config.model = loaded_model
            console.print(f"[green]Resumed session: {args.resume} (model: {agent.llm.model})[/green]")
        else:
            console.print(f"[red]Session '{args.resume}' not found.[/red]")
            sys.exit(1)

    # one-shot mode
    if args.prompt:
        _run_once(agent, args.prompt)
        return

    # interactive REPL
    _repl(agent, config)


def _ask_permission(tool_name: str, arguments: dict) -> str:
    """REPL consent prompt. Anything but a clear yes counts as a no."""
    console.print(f"\n[bold yellow]permission requested:[/] [cyan]{tool_name}[/cyan]({_brief(arguments)})")
    try:
        answer = pt_prompt("  [y] allow once  [a] always allow this tool  [n] deny: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        console.print("[dim]denied[/dim]")
        return "deny"
    if answer in ("y", "yes"):
        return "once"
    if answer in ("a", "always"):
        return "always"
    return "deny"


def _run_once(agent: Agent, prompt: str):
    """Non-interactive: run one prompt and exit."""
    perm = agent.permission
    if perm is not None and perm.ask is None and not perm.allow_all:
        console.print("[dim]one-shot mode: mutating tools are refused unless you pass --yes[/dim]")

    def on_token(tok):
        print(tok, end="", flush=True)

    def on_tool(name, kwargs):
        console.print(f"\n[dim]> {name}({_brief(kwargs)})[/dim]")

    def on_reasoning(tok):
        # thinking goes to stderr so piped stdout stays the plain answer
        err_console.print(f"[dim]{tok}[/dim]", end="")

    try:
        agent.chat(prompt, on_token=on_token, on_tool=on_tool, on_reasoning=on_reasoning)
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/yellow]")
        console.print("[dim]A parallel tool call already running may still finish; "
                      "re-check the workspace shortly.[/dim]")
        sys.exit(130)
    except TruncatedOutput as e:
        # the answer is unusable: stderr + non-zero so scripts and CI see it
        err_console.print(f"\n[red]Error: {e}[/red]")
        sys.exit(1)
    except BudgetExceeded as e:
        # the run was cut off on purpose: surface it distinctly, non-zero
        err_console.print(f"\n[red]Budget stopped: {e}[/red]")
        sys.exit(1)
    except Exception as e:  # noqa: BLE001
        # one-shot mode: print whatever went wrong and exit non-zero
        console.print(f"\n[red]Error: {e}[/red]")
        sys.exit(1)
    print()


def _repl(agent: Agent, config: Config):
    """Interactive read-eval-print loop."""
    _print_banner(agent, config)
    history = FileHistory(os.path.expanduser("~/.corecoder_history"))
    kb = _key_bindings()

    while True:
        user_input = _read_prompt(agent, history, kb)
        if user_input is None:
            console.print("\nBye!")
            break
        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit", "/quit", "/exit"):
            break
        prompt = _run_command(agent, config, user_input)
        if prompt is not None:
            _run_turn(agent, prompt)


def _print_banner(agent: Agent, config: Config) -> None:
    """The startup panel: version, model, consent mode, hooks and MCP."""
    perm = agent.permission
    mode = "auto-approve every tool call (--yes)" if (perm and perm.allow_all) else "ask before mutating tools"
    mcp_count = sum(1 for t in agent.tools if t.name.startswith("mcp__"))
    console.print(Panel(
        f"[bold]CoreCoder[/bold] v{__version__}\n"
        f"Model: [cyan]{config.model}[/cyan]"
        + (f"  Base: [dim]{config.base_url}[/dim]" if config.base_url else "")
        + f"\nPermissions: [cyan]{mode}[/cyan]"
        + (f"\nHooks: [cyan]{len(agent.hooks.pre)} pre, {len(agent.hooks.post)} post[/cyan]"
           " from ~/.corecoder/hooks.json" if agent.hooks else "")
        + (f"\nMCP: [cyan]{mcp_count} tools[/cyan] from ~/.corecoder/mcp.json" if mcp_count else "")
        + "\nType [bold]/help[/bold] for commands, [bold]Ctrl+C[/bold] to cancel, [bold]quit[/bold] to exit.",
        border_style="blue",
    ))


def _key_bindings() -> KeyBindings:
    """Enter submits, Escape+Enter inserts a newline (for pasting code blocks etc.)."""
    kb = KeyBindings()

    @kb.add("enter")
    def _submit(event):
        event.current_buffer.validate_and_handle()

    @kb.add("escape", "enter")
    def _newline(event):
        event.current_buffer.insert_text("\n")

    return kb


def _read_prompt(agent: Agent, history, kb) -> str | None:
    """One submitted line; None when the user closed the prompt (Ctrl+C / EOF)."""
    try:
        return pt_prompt(
            "You (plan) > " if agent.plan_mode else "You > ",
            history=history,
            multiline=True,
            key_bindings=kb,
            prompt_continuation="...  ",
        ).strip()
    except (EOFError, KeyboardInterrupt):
        return None


def _run_command(agent: Agent, config: Config, text: str) -> str | None:
    """Run one REPL built-in; return the prompt to send, or None when consumed.

    Only ``approve`` returns text: leaving plan mode is not the end of the
    turn, the approval itself becomes the message the model acts on.
    """
    if text == "/help":
        _show_help()
    elif text == "/reset":
        agent.reset()
        console.print("[yellow]Conversation reset.[/yellow]")
    elif text == "/plan":
        _toggle_plan(agent)
    elif agent.plan_mode and text.lower() in ("approve", "/approve"):
        return _approve_plan(agent)
    elif text == "/tokens":
        _print_tokens(agent)
    elif text == "/model" or text.startswith("/model "):
        _switch_model(agent, config, text)
    elif text == "/compact":
        _compact_context(agent)
    elif text == "/save":
        sid = save_session(agent.messages, config.model, session_id=agent.session_id)
        console.print(f"[green]Session saved: {sid}[/green]")
        console.print(f"Resume with: corecoder -r {sid}")
    elif text == "/diff":
        _print_changes()
    elif text == "/undo":
        _undo_change()
    elif text == "/sessions":
        _print_sessions()
    elif text == "/mcp" or text.startswith("/mcp "):
        _mcp(text[4:].strip())
    elif text.startswith("/"):
        console.print(f"[yellow]Unknown command: {text.split()[0]} (try /help)[/yellow]")
    else:
        return text
    return None


def _run_turn(agent: Agent, prompt: str) -> None:
    """Send one prompt, render the answer, and keep the REPL alive."""
    streamed: list[str] = []

    def on_token(tok):
        streamed.append(tok)
        print(tok, end="", flush=True)

    def on_tool(name, kwargs):
        console.print(f"\n[dim]> {name}({_brief(kwargs)})[/dim]")

    def on_reasoning(tok):
        # dim the thinking inline; mark the stream non-empty so the final
        # newline logic below still fires even if the reply itself is empty
        streamed.append(tok)
        console.print(f"[dim]{tok}[/dim]", end="")

    try:
        response = agent.chat(prompt, on_token=on_token, on_tool=on_tool,
                              on_reasoning=on_reasoning)
        if streamed:
            print()  # newline after streamed tokens
        else:
            # response wasn't streamed (came after tool calls)
            console.print(Markdown(response))
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/yellow]")
        console.print("[dim]A parallel tool call already running may still finish; "
                      "re-check the workspace shortly.[/dim]")
    except TruncatedOutput as e:
        # the streamed half-answer stays on screen; say why it stopped
        console.print(f"\n[red]{e}[/red]")
    except Exception as e:  # noqa: BLE001
        # keep the REPL alive no matter what chat() throws
        console.print(f"\n[red]Error: {e}[/red]")


def _toggle_plan(agent: Agent) -> None:
    agent.plan_mode = not agent.plan_mode
    if agent.plan_mode:
        console.print(
            "[yellow]Plan mode on.[/yellow] The agent can look but not touch: it will "
            "investigate read-only and present a plan. Type [bold]approve[/bold] to "
            "accept the plan, or [bold]/plan[/bold] again to exit."
        )
    else:
        console.print("[yellow]Plan mode off.[/yellow]")


def _approve_plan(agent: Agent) -> str:
    """Leave plan mode; the approval itself becomes the prompt."""
    agent.plan_mode = False
    console.print("[yellow]Plan mode off.[/yellow]")
    return "approve"


def _print_tokens(agent: Agent) -> None:
    p = agent.llm.total_prompt_tokens
    c = agent.llm.total_completion_tokens
    line = f"Tokens: [cyan]{p}[/cyan] prompt + [cyan]{c}[/cyan] completion = [bold]{p+c}[/bold] total"
    cost = agent.llm.estimated_cost
    if cost is not None:
        line += f"  (~${cost:.4f})"
    console.print(line)


def _switch_model(agent: Agent, config: Config, text: str) -> None:
    new_model = text[7:].strip() if text.startswith("/model ") else ""
    if not new_model:
        console.print(f"Current model: [cyan]{config.model}[/cyan]")
        return
    agent.llm.model = new_model
    config.model = new_model
    console.print(f"Switched to [cyan]{new_model}[/cyan]")


def _compact_context(agent: Agent) -> None:
    from .context import estimate_tokens

    before = estimate_tokens(agent.messages)
    compressed = agent.context.maybe_compress(agent.messages, agent.llm)
    after = estimate_tokens(agent.messages)
    if compressed:
        console.print(f"[green]Compressed: {before} → {after} tokens ({len(agent.messages)} messages)[/green]")
    else:
        console.print(f"[dim]Nothing to compress ({before} tokens, {len(agent.messages)} messages)[/dim]")


def _print_changes() -> None:
    from .tools.edit import _changed_files

    if not _changed_files:
        console.print("[dim]No files modified this session.[/dim]")
        return
    console.print(f"[bold]Files modified this session ({len(_changed_files)}):[/bold]")
    for f in sorted(_changed_files):
        console.print(f"  [cyan]{f}[/cyan]")


def _undo_change() -> None:
    from .checkpoints import pending, undo

    console.print(undo())
    left = pending()
    if left:
        console.print(f"[dim]{left} more checkpoint(s) on the stack.[/dim]")


def _print_sessions() -> None:
    sessions = list_sessions()
    if not sessions:
        console.print("[dim]No saved sessions.[/dim]")
        return
    for s in sessions:
        console.print(f"  [cyan]{s['id']}[/cyan] ({s['model']}, {s['saved_at']}) {s['preview']}")


def _mcp(arg: str):
    """The /mcp command: server states, or `reconnect <name>` to revive one."""
    from . import mcp
    parts = arg.split()
    if not parts:
        if not mcp._live_clients:
            console.print("[dim]No MCP servers running (configure ~/.corecoder/mcp.json).[/dim]")
        for client in mcp._live_clients:
            if client.error is None:
                console.print(f"  [cyan]{client.name}[/cyan]: alive, {len(client.tools)} tools")
            else:
                console.print(
                    f"  [cyan]{client.name}[/cyan]: [red]dead[/red] ({client.error})"
                    f", revive with /mcp reconnect {client.name}"
                )
        return
    if len(parts) == 2 and parts[0] == "reconnect":
        client = next((c for c in mcp._live_clients if c.name == parts[1]), None)
        if client is None:
            console.print(f"[red]No MCP server named '{parts[1]}'.[/red]")
            return
        try:
            client.reconnect()
        except (mcp.MCPError, OSError) as e:
            console.print(f"[red]Reconnect failed:[/red] {e}")
        else:
            console.print(f"[green]Reconnected:[/green] [cyan]{client.name}[/cyan], {len(client.tools)} tools")
        return
    console.print("[yellow]Usage: /mcp [reconnect <name>][/yellow]")


def _show_help():
    console.print(Panel(
        "[bold]Commands:[/bold]\n"
        "  /help          Show this help\n"
        "  /reset         Clear conversation history\n"
        "  /model         Show current model\n"
        "  /model <name>  Switch model mid-conversation\n"
        "  /tokens        Show token usage\n"
        "  /compact       Compress conversation context\n"
        "  /diff          Show files modified this session\n"
        "  /undo          Revert the most recent file change\n"
        "  /plan          Toggle plan mode: read-only, then a plan to approve\n"
        "  /save          Save session to disk\n"
        "  /sessions      List saved sessions\n"
        "  /mcp           MCP server status; /mcp reconnect <name> revives a dead one\n"
        "  quit           Exit CoreCoder\n"
        "\n"
        "[bold]Input:[/bold]\n"
        "  Enter          Submit message\n"
        "  Esc+Enter      Insert newline (for pasting code)",
        title="CoreCoder Help",
        border_style="dim",
    ))


def _brief(kwargs: dict, maxlen: int = 80) -> str:
    s = ", ".join(f"{k}={repr(v)[:40]}" for k, v in kwargs.items())
    return s[:maxlen] + ("..." if len(s) > maxlen else "")
