"""Optional tracing: a small span interface with JSONL and Langfuse backends.

Tracing is off by default and never changes agent behaviour: every backend is
wrapped by :class:`SafeTracer`, which turns a broken sink into a one-time
warning. Audit logging is separate (see ``audit.py``): audit is metadata-only
and cheap, tracing is a span tree meant for a viewer. The two
meet through ``current_trace_context``: audit lines carry the ids of the span
that was open, so a line and its trace record can be joined.

Agent code never touches a span: it records business facts through
:class:`RunTrace` (one chat run, its model rounds, its tool calls) and this
module decides what those facts mean for a span. The span protocol below is
the backends' side of that contract.
"""

import atexit
import json
import logging
import os
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .audit import prune_old_logs, truthy

_general = logging.getLogger("corecoder")

_DEFAULT_MAX_BYTES = 100 * 1024 * 1024
# backends that mean "no tracing at all"
_OFF_BACKENDS = ("", "off", "none", "0")
# attributes dropped from every span unless content capture was asked for
_CONTENT_KEYS = ("input", "output")


class _Once:
    """A thread-safe latch for a warning that must fire at most once."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._done = False

    def claim(self) -> bool:
        """True for the first caller only."""
        with self._lock:
            if self._done:
                return False
            self._done = True
            return True


class _SpanStack:
    """The spans open on one thread, innermost last.

    Every tracer owns one, and so does this module: the audit log asks the
    module-wide stack (``_context``) for the innermost span, which is the only
    link between the two streams. Per-thread by construction, because a
    parallel worker starts no span and must therefore report none.
    """

    def __init__(self) -> None:
        self._local = threading.local()

    def _stack(self) -> list:
        stack = getattr(self._local, "stack", None)
        if stack is None:
            stack = self._local.stack = []
        return stack

    def push(self, span) -> None:
        self._stack().append(span)

    def pop(self, span) -> None:
        """Remove ``span``, tolerating an out-of-order close."""
        stack = self._stack()
        if stack and stack[-1] is span:
            stack.pop()
        elif span in stack:
            stack.remove(span)

    def top(self):
        """The innermost open span, or None."""
        stack = self._stack()
        return stack[-1] if stack else None


_context = _SpanStack()


def current_trace_context() -> dict[str, str] | None:
    """Ids of the innermost span open on this thread, as audit join keys.

    ``None`` when tracing is off: a NullTracer produces no spans, so there is
    nothing to join, and invented ids would make a broken join look fine.
    """
    span = _context.top()
    if span is None:
        return None
    return {"trace_id": span.trace_id, "span_id": span.span_id}


class TracingUnavailable(RuntimeError):
    """The requested backend cannot start (missing package, key or import)."""


class Span:
    def update(self, **attrs) -> None:
        raise NotImplementedError

    def event(self, name: str, **fields) -> None:
        raise NotImplementedError

    def retime(self, start: float, end: float) -> None:
        """Place this span on the timeline after the fact."""
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

    def retime(self, start: float, end: float) -> None:
        pass

    def end(self, **attrs) -> None:
        pass


class NullTracer:
    """The tracer used whenever tracing is off or cannot start."""

    capture_content = False

    def start(self, name: str, as_type: str = "span", **attrs) -> NullSpan:
        return NullSpan()

    def flush(self) -> None:
        pass


@dataclass
class _Origin:
    """Where a new span belongs: its parent, ids and inherited session."""

    parent: object | None
    trace_id: str
    session_id: str
    round_idx: int | None


class _SpanTracer:
    """Span bookkeeping the backends share: thread stack, content filter, ids."""

    def __init__(self, capture_content: bool = False) -> None:
        self.capture_content = capture_content
        self._spans = _SpanStack()

    def _filter(self, attrs: dict) -> dict:
        """Drop captured prompts and results unless the user opted in."""
        if self.capture_content:
            return attrs
        return {k: v for k, v in attrs.items() if k not in _CONTENT_KEYS}

    def _origin(self, attrs: dict) -> _Origin:
        """Resolve the parent, trace id, session and round of a new span.

        ``session_id`` and ``round`` travel as start attributes but belong to
        the span record rather than to the span's own attributes, so they are
        popped here.
        """
        parent = self._spans.top()
        session_id = attrs.pop("session_id", None)
        if session_id is None:
            session_id = parent.session_id if parent is not None else ""
        round_idx = attrs.pop("round", None)
        trace_id = parent.trace_id if parent is not None else uuid.uuid4().hex
        return _Origin(parent, trace_id, session_id, round_idx)

    def _pop(self, span) -> None:
        self._spans.pop(span)

    def flush(self) -> None:
        pass


class _BaseSpan(Span):
    def __init__(self, tracer, name, as_type, origin: _Origin):
        self.tracer = tracer
        self.name = name
        self.type = as_type
        self.parent = origin.parent
        self.parent_id = origin.parent.span_id if origin.parent is not None else None
        self.trace_id = origin.trace_id
        self.span_id = uuid.uuid4().hex
        self.session_id = origin.session_id
        self.round = origin.round_idx
        self.attrs: dict = {}
        self.events: list[dict] = []
        self.error: str | None = None
        self.start = time.time()
        self.end_time: float | None = None
        self._ended = False
        _context.push(self)

    def update(self, **attrs) -> None:
        self.attrs.update(self.tracer._filter(attrs))

    def event(self, name: str, **fields) -> None:
        self.events.append({"name": name, "t": time.time(),
                            **self.tracer._filter(fields)})

    def retime(self, start: float, end: float) -> None:
        """Replay a measured interval for a span opened once its work was over.

        A span created after the fact would otherwise start and end in the same
        instant and report duration_ms=0. Backends that time themselves (the
        Langfuse SDK) are unaffected; their metadata still carries the measured
        duration_ms.
        """
        self.start = start
        self.end_time = end

    def end(self, **attrs) -> None:
        if self._ended:
            return
        self._ended = True
        error = attrs.pop("error", None)
        if error is not None:
            self.error = str(error)
        self.attrs.update(self.tracer._filter(attrs))
        if self.end_time is None:
            self.end_time = time.time()
        try:
            self._close()
        finally:
            self.tracer._pop(self)
            _context.pop(self)

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


class JsonlTracer(_SpanTracer):
    """One JSON line per finished span: append-only, joinable by id."""

    def __init__(self, path, capture_content: bool = False,
                 max_bytes: int = _DEFAULT_MAX_BYTES, prune: bool = False):
        super().__init__(capture_content)
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and self.path.stat().st_size > max_bytes:
            os.replace(self.path, str(self.path) + ".1")
        self._fd = os.open(self.path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
        if prune:
            # prune after opening so the current pid file exists and is kept
            prune_old_logs(self.path.parent / "traces.*.jsonl", keep=10, skip=self.path)
        self._lock = threading.Lock()

    def start(self, name: str, as_type: str = "span", **attrs) -> _JsonlSpan:
        span = _JsonlSpan(self, name, as_type, self._origin(attrs))
        span.update(**attrs)
        self._spans.push(span)
        return span

    def _write(self, record: dict) -> None:
        data = (json.dumps(record, ensure_ascii=False, default=str) + "\n").encode(
            "utf-8", "replace")
        with self._lock:
            _os_write(self._fd, data)

    def close(self) -> None:
        try:
            os.close(self._fd)
        except OSError:
            pass


def _native_usage(attrs: dict) -> dict | None:
    """Move agent token counts onto Langfuse's native ``usage_details``.

    ``usage_reported=False`` means the provider sent no usage and the zeros in
    ``LLMResponse`` are placeholders, so nothing is invented here: the server
    keeps its own estimate instead of reporting a made-up zero.
    """
    reported = attrs.pop("usage_reported", None)
    prompt = attrs.pop("prompt_tokens", None)
    completion = attrs.pop("completion_tokens", None)
    if reported is False:
        return None
    details = {}
    if prompt is not None:
        details["input"] = prompt
    if completion is not None:
        details["output"] = completion
    return details or None


def _exit_quietly(context) -> None:
    """Leave an optional OTEL context manager, when one was entered."""
    if context is not None:
        context.__exit__(None, None, None)


class _LangfuseSpan(_BaseSpan):
    def __init__(self, tracer, observation, context, name, as_type, origin,
                 propagator=None):
        super().__init__(tracer, name, as_type, origin)
        self.observation = observation
        self.context = context
        self.propagator = propagator

    def _apply(self, attrs: dict) -> None:
        attrs = self.tracer._filter(attrs)
        payload: dict = {}
        for key in ("model", "input", "output"):
            if key in attrs:
                payload[key] = attrs.pop(key)
        usage = _native_usage(attrs)
        if usage is not None:
            payload["usage_details"] = usage
        cost = attrs.pop("cost_usd", None)
        if cost is not None:
            payload["cost_details"] = {"total": cost}
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
        if self.error is not None:
            # `level` is how the trace list separates a failed step from an
            # ordinary one; leaving it at DEFAULT makes a broken run invisible
            self.observation.update(level="ERROR", status_message=self.error)
        self.context.__exit__(None, None, None)
        # the attribute context was opened before the observation, so it must
        # be closed after it: OTEL contexts unhook in reverse order
        _exit_quietly(self.propagator)


def _trace_attributes(session_id: str, user_id, tags) -> dict:
    """The trace-level attributes that were actually supplied."""
    attrs: dict = {}
    if session_id:
        attrs["session_id"] = session_id
    if user_id:
        attrs["user_id"] = user_id
    if tags:
        attrs["tags"] = tags
    return attrs


class LangfuseTracer(_SpanTracer):
    """Spans backed by the Langfuse SDK's observation contexts."""

    def __init__(self, capture_content: bool = False, client=None):
        super().__init__(capture_content)
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

    def _open_trace_attributes(self, origin: _Origin, attrs: dict):
        """Enter the trace-level attribute context, on the root span only.

        ``user_id`` and ``tags`` describe the whole exchange rather than one
        step, so on a child they stay ordinary metadata.
        """
        if origin.parent is not None:
            return None
        propagator = self._trace_attributes(
            origin.session_id, attrs.pop("user_id", None), attrs.pop("tags", None))
        if propagator is None:
            return None
        propagator.__enter__()
        return propagator

    def _open_observation(self, name: str, as_type: str):
        """Enter the SDK's observation context; returns (observation, context)."""
        context = self.client.start_as_current_observation(name=name, as_type=as_type)
        return context.__enter__(), context

    def _trace_attributes(self, session_id: str, user_id, tags):
        """Open trace-level attributes through whichever API this SDK offers.

        3.x exposed ``update_current_trace``; 4.x replaced it with the
        module-level ``propagate_attributes`` context manager. Neither being
        present is not an error: the span tree is still worth collecting
        without trace-level grouping, so this returns None and the root span
        is created bare.
        """
        attrs = _trace_attributes(session_id, user_id, tags)
        if not attrs:
            return None
        update_trace = getattr(self.client, "update_current_trace", None)
        if update_trace is not None:
            update_trace(**attrs)
            return None
        try:
            from langfuse import propagate_attributes
        except ImportError:
            return None
        return propagate_attributes(**attrs)

    def start(self, name: str, as_type: str = "span", **attrs) -> _LangfuseSpan:
        origin = self._origin(attrs)
        propagator = self._open_trace_attributes(origin, attrs)
        try:
            observation, context = self._open_observation(name, as_type)
        except BaseException:
            # a half-entered attribute context would leak into every later
            # span on this thread
            _exit_quietly(propagator)
            raise
        span = _LangfuseSpan(self, observation, context, name, as_type, origin, propagator)
        if origin.round_idx is not None:
            attrs["round"] = origin.round_idx
        span.update(**attrs)
        self._spans.push(span)
        return span

    def flush(self) -> None:
        flush = getattr(self.client, "flush", None) or getattr(
            self.client, "force_flush", None)
        if flush is not None:
            flush()


class SafeSpan(Span):
    """A span whose backend failures warn instead of raising."""

    def __init__(self, span, on_error):
        self._span = span
        self._on_error = on_error

    def update(self, **attrs) -> None:
        self._call("update", **attrs)

    def event(self, name: str, **fields) -> None:
        self._call("event", name, **fields)

    def retime(self, start: float, end: float) -> None:
        self._call("retime", start, end)

    def end(self, **attrs) -> None:
        self._call("end", **attrs)

    def _call(self, method: str, *args, **kwargs) -> None:
        try:
            getattr(self._span, method)(*args, **kwargs)
        except Exception as e:  # noqa: BLE001 - tracing must never break a run
            self._on_error(e)


class SafeTracer:
    """A tracer whose failures degrade to a no-op and one warning per process."""

    def __init__(self, tracer):
        self._tracer = tracer
        self._once = _Once()

    @property
    def capture_content(self) -> bool:
        return bool(getattr(self._tracer, "capture_content", False))

    def _warn(self, exc: BaseException) -> None:
        if not self._once.claim():
            return
        _warn_event("event=trace_error error_type=%s", type(exc).__name__)

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


def _warn_event(message: str, *args) -> None:
    """A tracing warning is best-effort: logging must not break a run either."""
    try:
        _general.warning(message, *args)
    except Exception:  # noqa: BLE001, S110 - a warning is best-effort
        pass


def _default_trace_path() -> Path:
    return Path.home() / ".corecoder" / f"traces.{os.getpid()}.jsonl"


def _warn_disabled(reason: str) -> None:
    _warn_event("event=trace_disabled reason=%s", reason)


def _max_trace_bytes() -> int:
    """``CORECODER_TRACE_MAX_MB`` in bytes, with the built-in cap on bad input."""
    max_mb = os.getenv("CORECODER_TRACE_MAX_MB")
    try:
        return int(float(max_mb) * 1024 * 1024) if max_mb else _DEFAULT_MAX_BYTES
    except ValueError:
        return _DEFAULT_MAX_BYTES


def _jsonl_tracer(file, capture_content: bool):
    """The JSONL backend, or a NullTracer when its path cannot be opened."""
    explicit = file if file is not None else os.getenv("CORECODER_TRACE_FILE")
    path = Path(explicit) if explicit else _default_trace_path()
    try:
        return SafeTracer(JsonlTracer(
            path, capture_content=capture_content, max_bytes=_max_trace_bytes(),
            prune=explicit is None))
    except Exception as e:  # noqa: BLE001 - unwritable path is not fatal
        _warn_disabled(f"jsonl_{type(e).__name__}")
        return NullTracer()


def _langfuse_tracer(capture_content: bool):
    """The Langfuse backend, or a NullTracer when the SDK or keys are missing."""
    try:
        return SafeTracer(LangfuseTracer(capture_content=capture_content))
    except Exception as e:  # noqa: BLE001 - missing SDK/keys is not fatal
        _warn_disabled(f"langfuse_{type(e).__name__}")
        return NullTracer()


def _resolve_capture_content(capture_content: bool | None) -> bool:
    """The flag decides once, here: an explicit argument beats the env switch."""
    if capture_content is None:
        capture_content = truthy(os.getenv("CORECODER_TRACE_CONTENT"))
    if capture_content:
        _warn_content_enabled()
    return capture_content


def create_tracer(backend: str | None = None, *, file=None,
                  capture_content: bool | None = None):
    """Build the configured tracer, or a NullTracer when the backend is off
    or cannot start. Degradation is always a warning, never an exception."""
    backend = (backend if backend is not None
               else os.getenv("CORECODER_TRACE", "off")).strip().lower()
    if backend in _OFF_BACKENDS:
        return NullTracer()

    capture_content = _resolve_capture_content(capture_content)
    if backend == "jsonl":
        return _jsonl_tracer(file, capture_content)
    if backend == "langfuse":
        return _langfuse_tracer(capture_content)

    _warn_disabled(f"unknown_backend_{backend}")
    return NullTracer()


_CONTENT_WARNED = _Once()


def _warn_content_enabled() -> None:
    """Say once that content capture writes source code and prompts to disk."""
    if not _CONTENT_WARNED.claim():
        return
    _warn_event(
        "event=trace_content_enabled note=%s",
        "content includes source and prompts; redaction is best-effort",
    )


def _round_fields(round_idx) -> dict:
    """An omitted round means "the caller did not know", not round zero."""
    return {} if round_idx is None else {"round": round_idx}


class GenerationTrace:
    """The recording handle for one model call.

    ``record`` speaks the same metric names the audit line uses, so the two
    streams describe a call with one vocabulary.
    """

    def __init__(self, span, capture_content: bool) -> None:
        self._span = span
        self._capture = capture_content

    def record(self, metrics: dict) -> None:
        self._span.update(**metrics)

    def capture_output(self, text: str) -> None:
        if self._capture:
            self._span.update(output=text)


class RunTrace:
    """The agent's view of a trace: one run, its rounds and its tool calls.

    Business code says what happened ("this round reported 5 prompt tokens",
    "this call took 12ms"); this class owns the span keys, the lazy history
    snapshot, the replayed interval of a tool span opened after the fact, and
    the fact that all of it is a no-op while tracing is off.
    """

    def __init__(self, tracer) -> None:
        self._tracer = tracer
        self._capture = bool(getattr(tracer, "capture_content", False))
        self._root = None

    # ── one chat run ──────────────────────────────────────────────────────
    def start_run(self, *, model, session_id, version, max_rounds,
                  tags=None, user_id=None) -> None:
        """Open the root span the whole run hangs from.

        ``tags`` and ``user_id`` describe the exchange rather than one step, and
        the caller supplies them: this module reads no labelling environment, so
        changing what a run is grouped by stays one module's decision.
        """
        labels: dict = {}
        if tags:
            labels["tags"] = tags
        if user_id:
            labels["user_id"] = user_id
        self._root = self._tracer.start(
            "agent.chat", model=model, session_id=session_id,
            version=version, max_rounds=max_rounds, **labels)

    def capture_request(self, text: str) -> None:
        """Record the user message that opened the run."""
        if self._capture and self._root is not None:
            self._root.update(input=text)

    def event(self, name: str, **fields) -> None:
        """Mark a point in time on the run: a truncation, a backfill, a retry."""
        if self._root is not None:
            self._root.event(name, **fields)

    def end_run(self, **fields) -> None:
        """Close the root span; later events are dropped rather than orphaned."""
        root, self._root = self._root, None
        if root is not None:
            root.end(**fields)

    # ── model calls ───────────────────────────────────────────────────────
    @contextmanager
    def llm_round(self, round_idx, *, model, estimated_prompt, messages=None):
        """Trace one model call.

        ``messages`` is a callable so the history is snapshotted only when
        content capture is on: the whole transcript is not cheap to copy, and
        off is the default.
        """
        with self._tracer.start(
            "llm.round", as_type="generation", **_round_fields(round_idx),
            model=model, estimated_prompt=estimated_prompt,
        ) as span:
            if self._capture and messages is not None:
                span.update(input=messages())
            yield GenerationTrace(span, self._capture)

    # ── tool calls ────────────────────────────────────────────────────────
    def tool_span(self, *, tool, round_idx=None, args_chars=0,
                  measured_ms=None, started_at=None):
        """Open a call's span.

        A parallel worker cannot push onto the tracer's thread-local stack, so
        its span is opened on the caller's thread after the call is over and
        would otherwise report duration_ms=0. Passing the ``measured_ms`` the
        worker timed (and the wall-clock ``started_at`` it began at) replays
        the real interval instead.
        """
        span = self._tracer.start(
            f"tool.{tool}", as_type="tool", **_round_fields(round_idx),
            args_chars=args_chars)
        if measured_ms is not None:
            duration = measured_ms / 1000
            begin = started_at if started_at is not None else time.time() - duration
            span.retime(begin, begin + duration)
        return span

    def tool_finished(self, span, *, tool, call_id, decision, attrs,
                      request_text=None, response_text=None) -> None:
        """Close a call's span with its outcome and its captured I/O.

        ``request_text``/``response_text`` are recorded only when content
        capture is on. A refusal that quotes broken JSON is never copied back
        out as the call's output.
        """
        fields = dict(attrs, decision=decision, tool=tool, call_id=call_id)
        if self._capture:
            if request_text is not None:
                fields["input"] = request_text
            if response_text is not None and decision != "bad_arguments":
                fields["output"] = response_text
        span.end(**fields)

    def tool_failed(self, span, exc: BaseException) -> None:
        """Close a span whose call escaped (Ctrl+C, a BaseException from a tool)."""
        span.end(error=f"{type(exc).__name__}: {exc}")


_os_write = os.write
