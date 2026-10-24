"""Optional tracing: a small span interface with JSONL and Langfuse backends.

Tracing is off by default and never changes agent behaviour: every backend is
wrapped by :class:`SafeTracer`, which turns a broken sink into a one-time
warning. Audit logging is separate (see ``observability.py``): audit is
metadata-only and cheap, tracing is a span tree meant for a viewer.
"""

import atexit
import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path

from .observability import prune_old_logs

_general = logging.getLogger("corecoder")


class TracingUnavailable(RuntimeError):
    """The requested backend cannot start (missing package, key or import)."""


class Span:
    def update(self, **attrs) -> None:
        raise NotImplementedError

    def event(self, name: str, **fields) -> None:
        raise NotImplementedError

    def end(self, **attrs) -> None:
        raise NotImplementedError

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            self.end(error=f"{exc_type.__name__}: {exc}")
        else:
            self.end()
        return False


class NullSpan(Span):
    def update(self, **attrs) -> None:
        pass

    def event(self, name: str, **fields) -> None:
        pass

    def end(self, **attrs) -> None:
        pass


class NullTracer:
    capture_content = False

    def start(self, name: str, as_type: str = "span", **attrs) -> NullSpan:
        return NullSpan()

    def flush(self) -> None:
        pass


class _BaseSpan(Span):
    def __init__(self, tracer, name, as_type, parent, trace_id, session_id, round_idx):
        self.tracer = tracer
        self.name = name
        self.type = as_type
        self.parent = parent
        self.parent_id = parent.span_id if parent is not None else None
        self.trace_id = trace_id
        self.span_id = uuid.uuid4().hex
        self.session_id = session_id
        self.round = round_idx
        self.attrs: dict = {}
        self.events: list[dict] = []
        self.error: str | None = None
        self.start = time.time()
        self.end_time: float | None = None
        self._ended = False

    def update(self, **attrs) -> None:
        self.attrs.update(self.tracer._filter(attrs))

    def event(self, name: str, **fields) -> None:
        self.events.append({"name": name, "t": time.time(),
                            **self.tracer._filter(fields)})

    def end(self, **attrs) -> None:
        if self._ended:
            return
        self._ended = True
        error = attrs.pop("error", None)
        if error is not None:
            self.error = str(error)
        self.attrs.update(self.tracer._filter(attrs))
        self.end_time = time.time()
        try:
            self._close()
        finally:
            self.tracer._pop(self)

    def _close(self) -> None:
        raise NotImplementedError


class _JsonlSpan(_BaseSpan):
    def _close(self) -> None:
        record = {
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_id": self.parent_id,
            "session_id": self.session_id,
            "name": self.name,
            "type": self.type,
            "start": round(self.start, 6),
            "end": round(self.end_time, 6),
            "duration_ms": round((self.end_time - self.start) * 1000, 3),
            "attrs": self.attrs,
            "events": self.events,
            "error": self.error,
        }
        if self.round is not None:
            record["round"] = self.round
        self.tracer._write(record)


class JsonlTracer:
    def __init__(self, path, capture_content: bool = False,
                 max_bytes: int = 100 * 1024 * 1024, prune: bool = False):
        self.capture_content = capture_content
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and self.path.stat().st_size > max_bytes:
            os.replace(self.path, str(self.path) + ".1")
        self._fd = os.open(self.path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
        if prune:
            # prune after opening so the current pid file exists and is kept
            prune_old_logs(self.path.parent / "traces.*.jsonl", keep=10, skip=self.path)
        self._lock = threading.Lock()
        self._local = threading.local()

    def _filter(self, attrs: dict) -> dict:
        if self.capture_content:
            return attrs
        return {k: v for k, v in attrs.items() if k not in ("input", "output")}

    def _stack(self) -> list:
        stack = getattr(self._local, "stack", None)
        if stack is None:
            stack = self._local.stack = []
        return stack

    def _pop(self, span) -> None:
        stack = self._stack()
        if stack and stack[-1] is span:
            stack.pop()
        elif span in stack:
            stack.remove(span)

    def start(self, name: str, as_type: str = "span", **attrs) -> _JsonlSpan:
        stack = self._stack()
        parent = stack[-1] if stack else None
        session_id = attrs.pop("session_id", None)
        if session_id is None:
            session_id = parent.session_id if parent is not None else ""
        round_idx = attrs.pop("round", None)
        trace_id = parent.trace_id if parent is not None else uuid.uuid4().hex
        span = _JsonlSpan(self, name, as_type, parent, trace_id, session_id, round_idx)
        span.update(**attrs)
        stack.append(span)
        return span

    def _write(self, record: dict) -> None:
        data = (json.dumps(record, ensure_ascii=False, default=str) + "\n").encode(
            "utf-8", "replace")
        with self._lock:
            _os_write(self._fd, data)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        try:
            os.close(self._fd)
        except OSError:
            pass


class _LangfuseSpan(_BaseSpan):
    def __init__(self, tracer, observation, context, *args):
        super().__init__(tracer, *args)
        self.observation = observation
        self.context = context

    def _apply(self, attrs: dict) -> None:
        attrs = self.tracer._filter(attrs)
        payload: dict = {}
        for key in ("model", "input", "output"):
            if key in attrs:
                payload[key] = attrs.pop(key)
        if attrs:
            payload["metadata"] = attrs
        if payload:
            self.observation.update(**payload)

    def update(self, **attrs) -> None:
        self.attrs.update(self.tracer._filter(attrs))
        self._apply(dict(attrs))

    def event(self, name: str, **fields) -> None:
        self.events.append({"name": name, "t": time.time(),
                            **self.tracer._filter(fields)})
        create = getattr(self.observation, "create_event", None)
        if create is not None:
            create(name=name, metadata=self.tracer._filter(fields))

    def _close(self) -> None:
        self._apply(self.attrs)
        self.context.__exit__(None, None, None)


class LangfuseTracer:
    def __init__(self, capture_content: bool = False, client=None):
        self.capture_content = capture_content
        if client is None:
            if not (os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY")):
                raise TracingUnavailable(
                    "LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are required")
            try:
                from langfuse import get_client
            except ImportError as e:
                raise TracingUnavailable(f"langfuse is not installed: {e}") from e
            client = get_client()
            atexit.register(self.flush)
        self.client = client
        self._local = threading.local()

    def _filter(self, attrs: dict) -> dict:
        if self.capture_content:
            return attrs
        return {k: v for k, v in attrs.items() if k not in ("input", "output")}

    def _stack(self) -> list:
        stack = getattr(self._local, "stack", None)
        if stack is None:
            stack = self._local.stack = []
        return stack

    def _pop(self, span) -> None:
        stack = self._stack()
        if stack and stack[-1] is span:
            stack.pop()
        elif span in stack:
            stack.remove(span)

    def start(self, name: str, as_type: str = "span", **attrs) -> _LangfuseSpan:
        stack = self._stack()
        parent = stack[-1] if stack else None
        session_id = attrs.pop("session_id", None)
        if session_id is None:
            session_id = parent.session_id if parent is not None else ""
        round_idx = attrs.pop("round", None)
        trace_id = parent.trace_id if parent is not None else uuid.uuid4().hex

        context = self.client.start_as_current_observation(name=name, as_type=as_type)
        observation = context.__enter__()
        span = _LangfuseSpan(
            self, observation, context, name, as_type, parent, trace_id,
            session_id, round_idx)
        if round_idx is not None:
            attrs["round"] = round_idx
        if parent is None and session_id:
            update_trace = getattr(self.client, "update_current_trace", None)
            if update_trace is not None:
                update_trace(session_id=session_id)
        span.update(**attrs)
        stack.append(span)
        return span

    def flush(self) -> None:
        flush = getattr(self.client, "flush", None) or getattr(
            self.client, "force_flush", None)
        if flush is not None:
            flush()


class SafeSpan(Span):
    def __init__(self, span, on_error):
        self._span = span
        self._on_error = on_error

    def update(self, **attrs) -> None:
        self._call("update", **attrs)

    def event(self, name: str, **fields) -> None:
        self._call("event", name, **fields)

    def end(self, **attrs) -> None:
        self._call("end", **attrs)

    def _call(self, method: str, *args, **kwargs) -> None:
        try:
            getattr(self._span, method)(*args, **kwargs)
        except Exception as e:  # noqa: BLE001 - tracing must never break a run
            self._on_error(e)


class SafeTracer:
    def __init__(self, tracer):
        self._tracer = tracer
        self._warned = False
        self._lock = threading.Lock()

    @property
    def capture_content(self) -> bool:
        return bool(getattr(self._tracer, "capture_content", False))

    def _warn(self, exc: BaseException) -> None:
        with self._lock:
            if self._warned:
                return
            self._warned = True
        try:
            _general.warning("event=trace_error error_type=%s", type(exc).__name__)
        except Exception:  # noqa: BLE001, S110 - a warning is best-effort
            pass

    def start(self, name: str, as_type: str = "span", **attrs) -> SafeSpan:
        try:
            return SafeSpan(self._tracer.start(name, as_type=as_type, **attrs), self._warn)
        except Exception as e:  # noqa: BLE001 - tracing must never break a run
            self._warn(e)
            return SafeSpan(NullSpan(), self._warn)

    def flush(self) -> None:
        flush = getattr(self._tracer, "flush", None)
        if flush is None:
            return
        try:
            flush()
        except Exception as e:  # noqa: BLE001
            self._warn(e)


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def _default_trace_path() -> Path:
    return Path.home() / ".corecoder" / f"traces.{os.getpid()}.jsonl"


def _warn_disabled(reason: str) -> None:
    try:
        _general.warning("event=trace_disabled reason=%s", reason)
    except Exception:  # noqa: BLE001, S110 - a warning is best-effort
        pass


def create_tracer(backend: str | None = None, *, file=None,
                  capture_content: bool | None = None):
    """Build the configured tracer, or a NullTracer when the backend is off
    or cannot start. Degradation is always a warning, never an exception."""
    backend = (backend if backend is not None
               else os.getenv("CORECODER_TRACE", "off")).strip().lower()
    if backend in ("", "off", "none", "0"):
        return NullTracer()

    if capture_content is None:
        capture_content = _truthy(os.getenv("CORECODER_TRACE_CONTENT"))
    if capture_content:
        _warn_disabled_content()

    if backend == "jsonl":
        explicit = file if file is not None else os.getenv("CORECODER_TRACE_FILE")
        path = Path(explicit) if explicit else _default_trace_path()
        max_mb = os.getenv("CORECODER_TRACE_MAX_MB")
        try:
            max_bytes = int(float(max_mb) * 1024 * 1024) if max_mb else 100 * 1024 * 1024
        except ValueError:
            max_bytes = 100 * 1024 * 1024
        try:
            return SafeTracer(JsonlTracer(
                path, capture_content=capture_content, max_bytes=max_bytes,
                prune=explicit is None))
        except Exception as e:  # noqa: BLE001 - unwritable path is not fatal
            _warn_disabled(f"jsonl_{type(e).__name__}")
            return NullTracer()

    if backend == "langfuse":
        try:
            return SafeTracer(LangfuseTracer(capture_content=capture_content))
        except Exception as e:  # noqa: BLE001 - missing SDK/keys is not fatal
            _warn_disabled(f"langfuse_{type(e).__name__}")
            return NullTracer()

    _warn_disabled(f"unknown_backend_{backend}")
    return NullTracer()


_CONTENT_WARNED = False


def _warn_disabled_content() -> None:
    global _CONTENT_WARNED
    if _CONTENT_WARNED:
        return
    _CONTENT_WARNED = True
    try:
        _general.warning(
            "event=trace_content_enabled note=%s",
            "content includes source and prompts; redaction is best-effort",
        )
    except Exception:  # noqa: BLE001, S110 - a warning is best-effort
        pass


_os_write = os.write
