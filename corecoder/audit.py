"""The audit stream, plus the package's general logger setup.

Two jobs, deliberately independent:

- the audit log: metadata-only events, its sink, its failure policy and its
  retention. ``CORECODER_AUDIT`` switches it on, and it never shares the
  ``corecoder`` logger because the latter defaults to WARNING while audit
  events are INFO by design.
- ``configure_logging``: the one place that configures that ``corecoder``
  logger (hook warnings, MCP degradations, tracing fallbacks).

Tracing is not here: spans, backends and the ``RunTrace`` adapter live in
``tracing.py``. The two meet through ``current_trace_context``: an audit line
carries the ids of the span that was open, so a line and its trace record can
be joined.

Audit events carry metadata only: tool names, decisions, timings and token
counts. Arguments, results and user input never enter the log. ``log_event``
never raises: it runs from ``finally`` blocks where an exception would replace
the real failure (a swallowed KeyboardInterrupt, for example).

When tracing is on, each line also carries the ``trace_id``/``span_id`` of the
span that was open, so the audit log and the span tree describe the same work.

Sinks (first match wins):

- an explicit ``stream=`` passed by an embedding or a test,
- ``CORECODER_AUDIT_STREAM=stderr``, an opt-in because the REPL owns the
  terminal,
- ``CORECODER_AUDIT_FILE`` or an explicit ``file=``,
- ``~/.corecoder/audit.<pid>.log``.

Three layers, top to bottom: which sink to use (``_build_handler`` and below),
how a broken sink degrades (``_AuditState``), and how one line is shaped
(``_format`` and ``log_event``).
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
from pathlib import Path

_TRUTHY = frozenset({"1", "true", "yes", "on"})
# three failed writes in a row mean the sink is gone, not that a line was unlucky
_FAILURES_BEFORE_DISABLE = 3

audit_logger = logging.getLogger("corecoder.audit")
audit_logger.propagate = False

_general = logging.getLogger("corecoder")


def truthy(value: str | None) -> bool:
    """The ``1/true/yes/on`` spelling every switch in this package accepts."""
    return (value or "").strip().lower() in _TRUTHY


def _warn(message: str, *args) -> None:
    """A diagnostic about the audit log must never become the failure."""
    try:
        _general.warning(message, *args)
    except Exception:  # noqa: BLE001, S110 - a warning is best-effort
        pass


class _AuditState:
    """The installed sink plus its failure policy.

    Mutated only while ``_lock`` is held, so a concurrent ``log_event`` and
    ``reset_audit`` cannot leave a half-installed handler behind.
    """

    def __init__(self) -> None:
        self.handler: logging.Handler | None = None
        self.disabled = False
        self.failures = 0
        self.warned = False

    def reset(self) -> None:
        handler, self.handler = self.handler, None
        if handler is not None:
            audit_logger.removeHandler(handler)
            handler.close()
        self.disabled = False
        self.failures = 0
        self.warned = False

    def install(self, handler: logging.Handler) -> None:
        audit_logger.addHandler(handler)
        self.handler = handler

    def note_write_ok(self) -> None:
        self.failures = 0

    def note_write_failure(self) -> bool:
        """Count one failed write; True when the one-time warning is due.

        The stream switches itself off after ``_FAILURES_BEFORE_DISABLE``
        failures in a row: a sink that is gone stays gone, and calling it
        forever would cost the agent a syscall per event.
        """
        self.failures += 1
        due = not self.warned
        self.warned = True
        if self.failures >= _FAILURES_BEFORE_DISABLE:
            self.disabled = True
        return due


_state = _AuditState()
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
    return truthy(os.getenv("CORECODER_AUDIT"))


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


def _newest_files(target: Path) -> list[Path]:
    """Every file matching ``target``'s pattern, newest first."""
    candidates = [p for p in target.parent.glob(target.name) if p.is_file()]
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink()
    except OSError:  # a locked old log is not worth failing on
        pass


# one day of real runs already outgrows ten pid files; CORECODER_AUDIT_KEEP
# raises the cap, while anyone who never sets it keeps the old behaviour
_DEFAULT_KEEP = 10


def prune_old_logs(pattern, keep: int = _DEFAULT_KEEP, skip: Path | None = None) -> None:
    """Keep the newest ``keep`` files matching ``pattern``, never deleting
    ``skip`` (the file this process just opened). Errors are best-effort."""
    try:
        skip_resolved = skip.resolve() if skip is not None else None
        for old in _newest_files(Path(pattern))[keep:]:
            if skip_resolved is None or old.resolve() != skip_resolved:
                _unlink_quietly(old)
    except Exception:  # noqa: BLE001, S110 - retention must never break startup
        pass


def reset_audit() -> None:
    """Drop the configured sink and forget its failure state (tests, reconfig)."""
    with _lock:
        _state.reset()


def _open_audit_file(file=None) -> logging.Handler:
    """Open this process's audit file, rotating an oversized one first."""
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
        # -- and ten of them is less than a day of real runs, so it is a setting
        prune_old_logs(
            path.parent / "audit.*.log",
            keep=_env_int("CORECODER_AUDIT_KEEP", _DEFAULT_KEEP), skip=path)
    return _AuditHandler(fd=fd)


def _build_handler(stream=None, file=None) -> logging.Handler:
    """Pick the sink: explicit stream, opted-in stderr, then a file."""
    if stream is not None:
        return _AuditHandler(stream=stream)
    if os.getenv("CORECODER_AUDIT_STREAM", "").strip().lower() == "stderr":
        return _AuditHandler(stream=sys.stderr)
    return _open_audit_file(file)


def _install_sink(stream=None, file=None, level=None) -> None:
    """Build the configured sink, attach it to the audit logger, keep it."""
    handler = _build_handler(stream=stream, file=file)
    configured = level if level is not None else os.getenv("CORECODER_AUDIT_LEVEL", "INFO")
    handler.setLevel(_parse_level(configured))
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler._cc_audit = True
    _state.install(handler)


def configure_audit(stream=None, file=None, level=None) -> None:
    """Install the audit sink. Safe to call repeatedly; never raises."""
    error_type = None
    with _lock:
        _state.reset()
        try:
            _install_sink(stream=stream, file=file, level=level)
        except Exception as e:  # noqa: BLE001 - audit is optional
            _state.disabled = True
            error_type = type(e).__name__
    if error_type is not None:
        _warn("event=audit_disabled error_type=%s", error_type)


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


def _trace_context() -> dict:
    """Ids of the span open on this thread, or nothing when tracing is off.

    Imported lazily: ``tracing`` imports this module, so a top-level import
    here would be a cycle. Failing to resolve the ids must never cost the line.
    """
    try:
        from .tracing import current_trace_context
        return current_trace_context() or {}
    except Exception:  # noqa: BLE001 - the line matters more than the join
        return {}


def _active_handler() -> logging.Handler | None:
    """The installed sink, installing the default one on first use.

    ``None`` means "drop this line": audit is switched off, or the last
    attempt to configure a sink failed.
    """
    if not audit_enabled() or _state.disabled:
        return None
    handler = _state.handler
    if handler is None:
        configure_audit()
        handler = _state.handler
    return None if _state.disabled else handler


def _write_failed(exc: BaseException) -> None:
    with _lock:
        due = _state.note_write_failure()
    if due:
        _warn("event=audit_write_failed error_type=%s", type(exc).__name__)


def _emit(handler: logging.Handler, event: str, level: int, fields: dict) -> None:
    """Hand one record to the sink; a broken sink is not an agent error."""
    # same names as the trace record, so the two files join on them; a
    # caller may set them explicitly and win over the ambient span
    context = _trace_context()
    if context:
        fields = {**context, **fields}
    record = logging.LogRecord(
        "corecoder.audit", level, __file__, 0, _format(event, fields), None, None)
    try:
        handler.handle(record)
    except Exception as e:  # noqa: BLE001 - a broken sink is not an agent error
        _write_failed(e)
    else:
        with _lock:
            _state.note_write_ok()


def log_event(event: str, level: int = logging.INFO, **fields) -> None:
    """Append one structured line. Never raises, never touches the terminal
    on its own, and drops silently once audit is switched off.

    With tracing on, the line also names the span that was open (``trace_id``,
    ``span_id``); that is what lets a reader find the matching trace record.
    """
    try:
        handler = _active_handler()
        if handler is None or level < handler.level:
            return
        _emit(handler, event, level, fields)
    except Exception:  # noqa: BLE001, S110 - log_event runs in finally blocks
        pass


def _general_handler(level: int) -> logging.Handler:
    """A file sink when ``CORECODER_LOG_FILE`` is set, otherwise stderr."""
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
    handler._cc_general = True
    return handler


def configure_logging() -> None:
    """Configure the general ``corecoder`` logger (hooks, MCP, degradations).
    Independent of the audit switch: audit INFO events never depend on this."""
    level = _parse_level(os.getenv("CORECODER_LOG_LEVEL", "WARNING"))
    logger = _general
    for handler in list(logger.handlers):
        if getattr(handler, "_cc_general", False):
            logger.removeHandler(handler)
            handler.close()
    logger.addHandler(_general_handler(level))
    logger.setLevel(level)
