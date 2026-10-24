#!/usr/bin/env python3
"""M4 check: tool results are clamped and redacted before entering history.

Three phases, all against real code:

  1. clamp:   a real 1.6 MB file is read with the real `read_file` tool. The
              model must call the tool; the stored result must be capped,
              carry a truncation marker and keep both markers.
  2. redact:  a real file contains four fake secrets. The stored result must
              contain none of the raw values and must carry [REDACTED markers.
  3. size:    a real 11 MB file is rejected by `ReadFileTool` (no API call).

Run (repo root):
    .venv\\Scripts\\python.exe examples\\m4_tool_result_governance.py

Failure injection:
    --max-result-chars 1000000  disable the clamp by raising the limit above
                                the file size; the marker checks should flip
                                to False, proving this phase covers clamping.
    --no-redact                 replace `corecoder.agent._redact` with an
                                identity function at runtime; raw secret
                                markers should flip to 4/4, proving this
                                phase covers redaction.

Cleanup:
    The script creates a temporary directory and prints its path. It is kept
    for inspection; remove it with the printed `rm -rf` command.

Exit codes:
    0  every enabled check passed
    1  a check failed
    2  no API key, or the model did not call read_file (retry or change model)
"""

import argparse
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rich.console import Console
from rich.panel import Panel

from corecoder.agent import Agent
from corecoder.config import Config
from corecoder.llm import LLM, LiteLLM
from corecoder.permissions import Permission
from corecoder.tools.read import ReadFileTool

REPO_ROOT = Path(__file__).resolve().parent.parent
console = Console()

HEAD_MARKER = "HEAD_MARKER_"
TAIL_MARKER = "_TAIL_MARKER"
SECRET_MARKERS = ("sk-proj-FAKEfakeFAKEfake0123456789", "Bearer FAKEbearer123456",
                  "AKIAFAKEFAKEFAKEFAKE", "MIIEfakekeymaterial")


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


def history_valid(messages: list[dict]) -> bool:
    ids = [
        tc["id"]
        for m in messages
        if m.get("role") == "assistant"
        for tc in (m.get("tool_calls") or [])
    ]
    answers = [m["tool_call_id"] for m in messages if m.get("role") == "tool"]
    return sorted(ids) == sorted(answers)


def tool_replies(messages: list[dict]) -> list[str]:
    return [m["content"] for m in messages if m.get("role") == "tool"]


def ask_for_read(llm, path: Path, max_chars: int, required: str) -> tuple[Agent, str]:
    """Run real turns until the model actually reads `path`.

    A model may answer without calling the tool, or mangle the path; that is
    model noise, not a governance failure. Three attempts keep the check
    deterministic without faking the tool call.
    """
    reply = ""
    agent = None
    for attempt in range(3):
        agent = Agent(
            llm=llm,
            tools=[ReadFileTool()],
            permission=Permission(allow_all=True),
            max_tool_result_chars=max_chars,
            max_rounds=6,
        )
        prompt = (
            f"Call the read_file tool on this exact path: {path}\n"
            "Do not ask questions and do not use any other tool. "
            "After the tool returns, reply with the single word DONE."
        )
        try:
            agent.chat(prompt)
        except Exception as exc:  # noqa: BLE001 - the failure is evidence
            console.print(f"[red]chat() raised:[/red] {type(exc).__name__}: {exc}")
        replies = tool_replies(agent.messages)
        reply = replies[0] if replies else ""
        final = next((m.get("content") for m in reversed(agent.messages)
                      if m.get("role") == "assistant" and m.get("content")), "")
        console.print(f"attempt {attempt + 1}: tool replies {len(replies)}, "
                      f"stored {len(reply)} chars, final {str(final)[:80]!r}")
        if required in reply:
            return agent, reply
        if reply:
            console.print(f"  stored head: {reply[:160]!r}")
    return agent, reply


def phase_clamp(llm, workdir: Path, max_chars: int) -> dict:
    path = workdir / "big.txt"
    body = "x" * (200_000)
    path.write_text(
        f"{HEAD_MARKER}\n{body}\n{body}\n{body}\n...{TAIL_MARKER}\n",
        encoding="utf-8",
    )
    console.print(f"file size: {path.stat().st_size} bytes")
    agent, reply = ask_for_read(llm, path, max_chars, HEAD_MARKER)
    stored = len(reply)
    marker = "truncated:" in reply
    head = HEAD_MARKER in reply[:2000]
    tail = reply.endswith(TAIL_MARKER)
    valid = history_valid(agent.messages) if agent else False
    return {"stored": stored, "marker": marker, "head": head, "tail": tail,
            "valid": valid, "read": HEAD_MARKER in reply,
            "file_size": path.stat().st_size}


def phase_redact(llm, workdir: Path, max_chars: int, required: str) -> dict:
    path = workdir / "secrets.txt"
    pem = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        f"{SECRET_MARKERS[3]}\n"
        "-----END RSA PRIVATE KEY-----"
    )
    path.write_text("\n".join(SECRET_MARKERS[:3] + (pem,)) + "\n", encoding="utf-8")
    agent, reply = ask_for_read(llm, path, max_chars, required)
    raw = sum(1 for marker in SECRET_MARKERS if marker in reply)
    redacted = "[REDACTED" in reply
    valid = history_valid(agent.messages) if agent else False
    return {"raw": raw, "redacted": redacted, "valid": valid,
            "read": required in reply}


def phase_size(workdir: Path) -> bool:
    path = workdir / "huge.txt"
    path.write_bytes(b"x" * (11 * 1024 * 1024))
    result = ReadFileTool().execute(file_path=str(path))
    return "Error: file too large" in result


def parse_args():
    parser = argparse.ArgumentParser(description="M4 tool-result governance check")
    parser.add_argument("--max-result-chars", type=int, default=15_000,
                        help="stored-result cap; raise it to disable clamping")
    parser.add_argument("--no-redact", action="store_true",
                        help="replace _redact with an identity function")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    load_dotenv_from_repo()
    cfg, llm = build_llm()
    console.print(Panel.fit(
        f"model: {cfg.model}\nbase: {cfg.base_url or 'default'}\n"
        f"max result chars: {args.max_result_chars}",
        title="M4 - clamp + redact tool results before history",
    ))

    workdir = Path(tempfile.mkdtemp(prefix="corecoder_m4_"))
    identity = mock.Mock(side_effect=lambda text: text)
    patcher = mock.patch("corecoder.agent._redact", identity) if args.no_redact else None
    if patcher:
        patcher.start()
    try:
        section("phase 1 - real read_file, oversized result")
        clamp = phase_clamp(llm, workdir, args.max_result_chars)

        section("phase 2 - real read_file, secrets in the output")
        required = SECRET_MARKERS[0] if args.no_redact else "[REDACTED"
        redact = phase_redact(llm, workdir, args.max_result_chars, required)

        section("phase 3 - real ReadFileTool, oversized file")
        oversized = phase_size(workdir)
    finally:
        if patcher:
            patcher.stop()

    # the clamp only fires when the raw result exceeds the configured cap
    expect_clamp = clamp["file_size"] > args.max_result_chars
    expect_redact = not args.no_redact
    valid = clamp["valid"] and redact["valid"]

    section("verdict")
    console.print(f"stored length: {clamp['stored']}")
    console.print(f"contains truncated marker: {clamp['marker']}")
    console.print(f"head preserved: {clamp['head']}")
    console.print(f"tail preserved: {clamp['tail']}")
    console.print(f"raw secret markers in history: {redact['raw']}/4")
    console.print(f"redacted marker present: {redact['redacted']}")
    console.print(f"oversized rejected: {oversized}")
    console.print(f"history valid: {valid}")

    if not clamp["read"] or not redact["read"]:
        console.print("")
        console.print("[yellow]The model did not read the target file; retry or "
                      "switch CORECODER_MODEL.[/yellow]")
        console.print(f"temporary files kept at: {workdir}")
        console.print(f'cleanup: rm -rf "{workdir}"')
        return 2

    ok = (
        (clamp["stored"] <= 15_200 if expect_clamp else clamp["stored"] > 15_200)
        and clamp["marker"] == expect_clamp
        and clamp["head"]
        and clamp["tail"]
        and redact["raw"] == (0 if expect_redact else 4)
        and redact["redacted"] == expect_redact
        and oversized
        and valid
    )
    if not ok:
        console.print("")
        console.print("[red]FAIL[/red] one or more governance checks did not hold.")
        console.print(f"temporary files kept at: {workdir}")
        console.print(f'cleanup: rm -rf "{workdir}"')
        return 1

    console.print("")
    console.print("[green]PASS[/green] all checks matched the configured mode "
                  f"(clamp {'on' if expect_clamp else 'off'}, "
                  f"redact {'on' if expect_redact else 'off'}).")
    console.print(f"temporary files kept at: {workdir}")
    console.print(f'cleanup: rm -rf "{workdir}"')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
