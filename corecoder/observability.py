"""Structured audit logging and general logging setup.

The audit stream is deliberately separate from the ``corecoder`` logger: the
latter defaults to WARNING, and audit events are INFO by design, so sharing it
would hide every event until the user also turned on debug logging.

Audit events carry metadata only: tool names, decisions, timings and token
counts. Arguments, results and user input never enter the log. ``log_event``
never raises: it runs from ``finally`` blocks where an exception would replace
the real failure (a swallowed KeyboardInterrupt, for example).

Sinks (first match wins):

- an explicit ``stream=`` passed by an embedding or a test,
- ``CORECODER_AUDIT_STREAM=stderr``, an opt-in because the REPL owns the
  terminal,
- ``CORECODER_AUDIT_FILE`` or an explicit ``file=``,
- ``~/.corecoder/audit.<pid>.log``.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
from pathlib import Path

_TRUTHY = {"1", "true", "yes", "on"}

audit_logger = logging.getLogger("corecoder.audit")
audit_logger.propagate = False

_general = logging.getLogger("corecoder")


class _State:
    def __init__(self) -> None:
        self.handler: logging.Handler | None = None
        self.disabled = False
        self.failures = 0
        self.warned = False


_state = _State()
_lock = threading.Lock()


class _AuditHandler(logging.Handler):
    """Writes one record per line. Emit does not swallow errors: log_event
    owns the failure policy, and a standard StreamHandler would turn it into
    an unreadable stderr traceback instead."""

    def __init__(self, stream=None, fd: int | None = None) -> None:
        super().__init__()
        self._stream = stream
        self._fd = fd

    def emit(self, record: logging.LogRecord) -> None:
        data = (self.format(record) + "\n").encode("utf-8", "replace")
        if self._fd is not None:
            os.write(self._fd, data)
        else:
            self._stream.write(data.decode("utf-8"))
            flush = getattr(self._stream, "flush", None)
            if flush is not None:
                flush()

    def close(self) -> None:
        try:
            if self._fd is not None:
                os.close(self._fd)
                self._fd = None
        finally:
            super().close()


def audit_enabled() -> bool:
    """Read the switch on every call so a late ``.env`` load still counts."""
    return os.getenv("CORECODER_AUDIT", "").strip().lower() in _TRUTHY


def _default_audit_path() -> Path:
    return Path.home() / ".corecoder" / f"audit.{os.getpid()}.log"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _parse_level(value: str) -> int:
    level = logging.getLevelName(str(value).upper())
    return level if isinstance(level, int) else logging.INFO


def prune_old_logs(pattern, keep: int = 10, skip: Path | None = None) -> None:
    """Keep the newest ``keep`` files matching ``pattern``, never deleting
    ``skip`` (the file this process just opened). Errors are best-effort."""
    try:
        target = Path(pattern)
        candidates = [p for p in target.parent.glob(target.name) if p.is_file()]
        candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        skip_resolved = skip.resolve() if skip is not None else None
        for old in candidates[keep:]:
            if skip_resolved is not None and old.resolve() == skip_resolved:
                continue
            try:
                old.unlink()
            except OSError:
                pass
    except Exception:  # noqa: BLE001 - retention must never break startup
        pass


def _reset_locked() -> None:
    if _state.handler is not None:
        audit_logger.removeHandler(_state.handler)
        _state.handler.close()
    _state.handler = None
    _state.disabled = False
    _state.failures = 0
    _state.warned = False


def reset_audit() -> None:
    """Drop the configured sink and forget its failure state (tests, reconfig)."""
    with _lock:
        _reset_locked()


def _build_handler(stream=None, file=None) -> logging.Handler:
    if stream is not None:
        return _AuditHandler(stream=stream)

    if os.getenv("CORECODER_AUDIT_STREAM", "").strip().lower() == "stderr":
        return _AuditHandler(stream=sys.stderr)

    explicit = file if file is not None else os.getenv("CORECODER_AUDIT_FILE")
    path = Path(explicit) if explicit else _default_audit_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    max_bytes = _env_int("CORECODER_AUDIT_MAX_MB", 100) * 1024 * 1024
    if path.exists() and path.stat().st_size > max_bytes:
        os.replace(path, str(path) + ".1")

    fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
    if explicit is None:
        # pid-suffixed files accumulate over many sessions; size rotation
        # almost never fires for them, so count-based retention is the real cap
        prune_old_logs(path.parent / "audit.*.log", keep=10, skip=path)
    return _AuditHandler(fd=fd)


def configure_audit(stream=None, file=None, level=None) -> None:
    """Install the audit sink. Safe to call repeatedly; never raises."""
    with _lock:
        _reset_locked()
        try:
            handler = _build_handler(stream=stream, file=file)
        except Exception as e:  # noqa: BLE001 - audit is optional
            _state.disabled = True
            try:
                _general.warning("event=audit_disabled error_type=%s", type(e).__name__)
            except Exception:  # noqa: BLE001
                pass
            return
        configured = level if level is not None else os.getenv("CORECODER_AUDIT_LEVEL", "INFO")
        handler.setLevel(_parse_level(configured))
        handler.setFormatter(logging.Formatter("%(message)s"))
        setattr(handler, "_cc_audit", True)
        audit_logger.addHandler(handler)
        _state.handler = handler


def _quote(value: str) -> str:
    if value == "" or any(ch.isspace() for ch in value) or "=" in value:
        return json.dumps(value, ensure_ascii=False)
    return value


def _format(event: str, fields: dict) -> str:
    parts = [f"event={event}"]
    for key, value in fields.items():
        if isinstance(value, str):
            parts.append(f"{key}={_quote(value)}")
        else:
            parts.append(f"{key}={value}")
    return " ".join(parts)


def _write_failed(exc: BaseException) -> None:
    _state.failures += 1
    if not _state.warned:
        _state.warned = True
        try:
            _general.warning("event=audit_write_failed error_type=%s", type(exc).__name__)
        except Exception:  # noqa: BLE001
            pass
    if _state.failures >= 3:
        _state.disabled = True


def log_event(event: str, level: int = logging.INFO, **fields) -> None:
    """Append one structured line. Never raises, never touches the terminal
    on its own, and drops silently once audit is switched off."""
    try:
        if not audit_enabled() or _state.disabled:
            return
        handler = _state.handler
        if handler is None:
            configure_audit()
            handler = _state.handler
            if handler is None or _state.disabled:
                return
        if level < handler.level:
            return
        record = logging.LogRecord(
            "corecoder.audit", level, __file__, 0, _format(event, fields), None, None)
        try:
            handler.handle(record)
        except Exception as e:  # noqa: BLE001 - a broken sink is not an agent error
            _write_failed(e)
        else:
            _state.failures = 0
    except Exception:  # noqa: BLE001 - log_event is called from finally blocks
        pass


def configure_logging() -> None:
    """Configure the general ``corecoder`` logger (hooks, MCP, degradations).
    Independent of the audit switch: audit INFO events never depend on this."""
    level = _parse_level(os.getenv("CORECODER_LOG_LEVEL", "WARNING"))
    logger = _general
    for handler in list(logger.handlers):
        if getattr(handler, "_cc_general", False):
            logger.removeHandler(handler)
            handler.close()

    target = os.getenv("CORECODER_LOG_FILE")
    handler: logging.Handler
    if target:
        try:
            Path(target).parent.mkdir(parents=True, exist_ok=True)
            handler = logging.FileHandler(target, encoding="utf-8")
        except OSError:
            handler = logging.StreamHandler()
    else:
        handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    handler.setLevel(level)
    setattr(handler, "_cc_general", True)
    logger.addHandler(handler)
    logger.setLevel(level)
