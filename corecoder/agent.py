"""Core agent loop.

This is the heart of CoreCoder.  The pattern is simple:

    user message -> LLM (with tools) -> tool calls? -> execute -> loop
                                      -> text reply? -> return to user

It keeps looping until the LLM responds with plain text (no tool calls),
which means it's done working and ready to report back.
"""

import concurrent.futures
import inspect
import json
import logging
import os
import re
import time
from dataclasses import dataclass

from .audit import log_event
from .context import ContextManager, estimate_tokens
from .llm import LLM, is_context_length_error
from .permissions import Permission
from .prompt import PLAN_MODE_PROMPT, system_prompt
from .session import new_session_id
from .tools import ALL_TOOLS
from .tools.agent import AgentTool
from .tools.base import Tool, ToolRefused
from .tools.bash import get_tracked_cwd, set_tracked_cwd
from .tools.todo import TodoWriteTool
from .tracing import RunTrace, create_tracer


class TruncatedOutput(RuntimeError):
    """The model hit its output cap twice in a row, so the last tool call was
    cut off and nothing was executed. Raised instead of returned: a return
    value is dropped by the streaming REPL, ignored by one-shot mode and read
    as success by sub-agents, so the failure would be invisible."""


class BudgetExceeded(RuntimeError):
    """A single run crossed its cost ceiling. Raised rather than returned so the
    runaway task stops immediately instead of spending another round: the point
    of the fuse is to cut power, not to report the bill afterwards."""


@dataclass
class _RunStats:
    """What one ``chat()`` accumulates for the run summary and the root span."""

    started: float
    cost_before: float | None
    # "max_rounds" used to be the default, so any escape the except clause in
    # `chat` missed was reported as a benign round exhaustion; every finish
    # path now names itself and an unlabeled run stays an error
    outcome: str = "error"
    error_type: str | None = None
    executed: int = 0        # tool calls that actually ran
    rounds: int = 0
    tool_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    usage_reported: bool = True
    # what the run delivered; the root span shows it as the trace's output, so
    # the trace list can answer "what did this run give back" at all
    answer: str | None = None


class Agent:
    def __init__(
        self,
        llm: LLM,
        tools: list[Tool] | None = None,
        max_context_tokens: int = 128_000,
        max_rounds: int = 50,
        permission=None,
        hooks=None,
        max_tool_result_chars: int = 15_000,
        session_id: str | None = None,
        tracer=None,
        loaded_names=None,
        budget_ceiling: float | None = None,
    ):
        self.llm = llm
        self.session_id = session_id or new_session_id()
        # per-run USD cap; None means no cap. Enforced in _account_usage against
        # the cost accrued since the run started, so a runaway task is cut off
        self.budget_ceiling = budget_ceiling
        self.tracer = tracer if tracer is not None else create_tracer()
        # everything the loop reports about tracing goes through this adapter,
        # so the loop itself never names a span or a capture flag
        self._trace = RunTrace(self.tracer)
        # call ids whose refusal quotes raw streamed JSON; content capture
        # shows a placeholder for these instead of copying it into the trace
        self._trace_withheld: set[str] = set()
        self.tools = tools if tools is not None else ALL_TOOLS
        self.permission = permission
        self.hooks = hooks
        self.max_tool_result_chars = max_tool_result_chars
        self._tool_by_name = {t.name: t for t in self.tools}
        # tool schemas are sent on every round and re-estimated by every
        # compression check; both read one cached pair, rebuilt only when the
        # tool catalog itself changes (a stable list also keeps the request
        # bytes identical so a provider's prefix cache can discount them)
        self._schema_cache: list[dict] = []
        self._schema_json: str = ""
        self._schema_sig: tuple | None = None
        self.messages: list[dict] = []
        self.context = ContextManager(max_tokens=max_context_tokens)
        self.max_rounds = max_rounds
        # P3: with a large MCP catalog only its index rides in the prompt and
        # each tool's full schema is pulled on demand by search_tools. A small
        # or MCP-free catalog leaves the sent bytes identical to the eager path.
        # loaded_names is only honored when it is a real collection (a mocked
        # parent hands us a stand-in attribute, not a set of names).
        self._loaded: set[str] = (
            set(loaded_names)
            if isinstance(loaded_names, (set, frozenset, list, tuple)) else set()
        )
        self._build_catalog()
        self.plan_mode = False  # toggled by /plan; while on, mutating tools are refused

        # wire up sub-agent capability
        for t in self.tools:
            if isinstance(t, AgentTool):
                t._parent_agent = self

        self._todo = next((t for t in self.tools if isinstance(t, TodoWriteTool)), None)

    def _full_messages(self) -> list[dict]:
        system = self._system
        # re-injected every round, like the task list below, so a toggle made
        # between turns takes effect on the very next request
        if self.plan_mode:
            system += "\n\n" + PLAN_MODE_PROMPT
        # the task list is re-injected every round, so the model always sees the
        # current state rather than a stale copy buried in old tool results
        if self._todo is not None:
            rendered = self._todo.render()
            if rendered:
                system += "\n\n# Current task list\n" + rendered
        return [{"role": "system", "content": system}] + self.messages

    def _trace_messages(self) -> list[dict]:
        """History for content capture, with bad-argument refusals withheld.

        The refusal quotes the raw streamed JSON so the model can repair it;
        the trace must not become a second copy of that text.
        """
        if not self._trace_withheld:
            return self._full_messages()
        return [
            {**m, "content": "[bad arguments withheld from trace]"}
            if m.get("role") == "tool" and m.get("tool_call_id") in self._trace_withheld
            else m
            for m in self._full_messages()
        ]

    def _should_defer(self, mcp: list[Tool]) -> bool:
        """Whether the MCP catalog is big enough to hide behind search_tools.

        ``CORECODER_TOOL_SEARCH`` is off|auto|on (default auto). auto engages
        only past a tool-count or schema-size threshold, so a small MCP setup
        - and every MCP-free run - stays on the byte-identical eager path.
        """
        if not mcp:
            return False
        mode = os.getenv("CORECODER_TOOL_SEARCH", "auto").strip().lower()
        if mode == "off":
            return False
        if mode == "on":
            return True

        def _env_int(key: str, default: int) -> int:
            raw = os.getenv(key, "").strip()
            try:
                return int(raw) if raw else default
            except ValueError:
                return default

        if len(mcp) >= _env_int("CORECODER_TOOL_SEARCH_MIN_COUNT", 10):
            return True
        pct = _env_int("CORECODER_TOOL_SEARCH_PCT", 8)
        schema_tokens = estimate_tokens(
            [{"content": json.dumps([t.schema() for t in mcp])}])
        return schema_tokens >= self.context.max_tokens * pct / 100

    def _build_catalog(self) -> None:
        """Split the tool list into an eager set and a deferred MCP index."""
        from .tools.search_tools import SearchToolsTool

        core = [t for t in self.tools if not t.name.startswith("mcp__")]
        mcp = [t for t in self.tools if t.name.startswith("mcp__")]
        self._mcp_list = mcp
        self._mcp_by_name = {t.name: t for t in mcp}
        self._defer_mcp = self._should_defer(mcp)
        if not self._defer_mcp:
            self._search_tool = None
            self._active_tools = list(self.tools)
            self._system = system_prompt(self.tools)
            return
        self._search_tool = SearchToolsTool(self)
        # search_tools is injected on demand, so it lives outside self.tools;
        # register it for execution just like the sub-agent back-reference
        self._tool_by_name[self._search_tool.name] = self._search_tool
        loaded = [t for t in mcp if t.name in self._loaded]
        self._active_tools = [*core, self._search_tool, *loaded]
        deferred_index = [t for t in mcp if t.name not in self._loaded]
        self._system = system_prompt([*core, self._search_tool], deferred=deferred_index)

    def _activate_matching(self, query: str) -> list[str]:
        """Pull deferred MCP tools onto the wire by name/description keyword."""
        if not self._defer_mcp:
            return []
        q = (query or "").strip().lower()
        hits = [
            t for t in self._mcp_list
            if t.name not in self._loaded
            and (not q or q in t.name.lower() or q in (t.description or "").lower())
        ]
        for t in hits:  # registration order keeps the appended tail prefix-stable
            self._loaded.add(t.name)
            self._active_tools.append(t)
        if hits:
            self._schema_sig = None  # the active set grew; rebuild the cache once
            log_event("tool_search", query=query, loaded=len(hits))
            self._trace.event("tool_search", query=query, loaded=len(hits))
        return [t.name for t in hits]

    def _schema_source(self) -> list[Tool]:
        """The list whose schemas are sent: the active set, or the raw catalog.

        While deferral is off this is ``self.tools`` itself, so catalog edits
        (append/replace) are reflected exactly as before; while on it is the
        append-only active set grown by search_tools.
        """
        return self._active_tools if self._defer_mcp else self.tools

    def _ensure_schema_cache(self) -> None:
        """Rebuild the cached schemas only when the sent catalog changed.

        The signature is the tuple of tool identities: adding, removing or
        swapping a tool object lands here, while an unchanged catalog reuses
        the prior list and its serialized form. A tool mutated in place (same
        object, new description) is not detected - none of the registration
        paths do that: MCP and the constructor always introduce new objects.
        """
        src = self._schema_source()
        sig = tuple(id(t) for t in src)
        if sig != self._schema_sig:
            self._schema_cache = [t.schema() for t in src]
            self._schema_json = json.dumps(self._schema_cache)
            self._schema_sig = sig

    def _tool_schemas(self) -> list[dict]:
        self._ensure_schema_cache()
        return self._schema_cache

    def _schema_json_str(self) -> str:
        self._ensure_schema_cache()
        return self._schema_json

    def _fixed_prompt_tokens(self) -> int:
        """Tokens every request carries that are not in `self.messages`."""
        system = self._full_messages()[0]["content"]
        return (estimate_tokens([{"role": "system", "content": system}])
                + estimate_tokens([{"content": self._schema_json_str()}]))

    def chat(self, user_input: str, on_token=None, on_tool=None, on_reasoning=None) -> str:
        """Process one user message. May involve multiple LLM/tool rounds."""
        from . import __version__

        self.messages.append({"role": "user", "content": user_input})
        self._trace.start_run(
            model=self._model, session_id=self.session_id,
            version=__version__, max_rounds=self.max_rounds, **self._labels())
        self._trace.capture_request(user_input)
        run = _RunStats(started=time.perf_counter(), cost_before=self._estimated_cost())
        try:
            answer = self._run_rounds(run, on_token, on_tool, on_reasoning)
            run.answer = answer
            return answer
        except BaseException as e:
            # BaseException, not Exception: a tool or a UI callback can die with
            # SystemExit or GeneratorExit, and those must not reach the summary
            # carrying whatever default the dataclass happened to start with
            run.outcome = _outcome_of(e)
            run.error_type = type(e).__name__
            raise
        finally:
            self._finish_run(run)

    @property
    def _model(self) -> str:
        """The model name as the audit line and the span both report it."""
        return getattr(self.llm, "model", "")

    @staticmethod
    def _labels() -> dict:
        """The trace-level grouping a run carries, read where the audit reads.

        ``CORECODER_BUILD`` is what separates a dogfood trace from a demo one in
        the list view; the trace tag carries the ``build:`` prefix because a bare
        word there is indistinguishable from any other tag. ``CORECODER_USER`` is
        opt-in: a person's name is not something to attach to telemetry by
        default, so it appears only when it was explicitly supplied.
        """
        labels: dict = {}
        build = os.getenv("CORECODER_BUILD", "").strip()
        if build:
            labels["tags"] = [f"build:{build}"]
        user = os.getenv("CORECODER_USER", "").strip()
        if user:
            labels["user_id"] = user
        return labels

    def _run_rounds(self, run, on_token, on_tool, on_reasoning) -> str:
        """Drive LLM and tool rounds until the model answers or the cap hits."""
        self.context.maybe_compress(
            self.messages, self.llm, fixed_tokens=self._fixed_prompt_tokens())

        truncated_rounds = 0  # M2 guard: consecutive truncation counter
        for round_idx in range(self.max_rounds):
            current_round = round_idx + 1
            run.rounds = current_round
            resp = self._model_round(current_round, run, on_token, on_reasoning)
            # M2: truncated response with tool_calls -> do NOT execute anything
            if resp.finish_reason == "length" and resp.tool_calls:
                truncated_rounds += 1
                self._refuse_truncated(resp, truncated_rounds, current_round)
                continue
            # reset consecutive counter on a successful non-truncated round
            truncated_rounds = 0

            # no tool calls -> LLM is done, return text
            if not resp.tool_calls:
                self.messages.append(resp.message)
                run.outcome = "answer"
                return resp.content

            self._tool_round(resp, on_tool, current_round, run)
            self.context.maybe_compress(
                self.messages, self.llm, fixed_tokens=self._fixed_prompt_tokens())

        if truncated_rounds:
            # the cap ran out on a truncated round, so no re-issue ever
            # happened: still raise, or a small max_rounds would bring back
            # the silent return this guard exists to remove
            raise TruncatedOutput(
                "model output truncated; raise max_tokens "
                "(CORECODER_MAX_TOKENS) or reduce the request"
            )
        log_event("max_rounds", level=logging.WARNING, rounds=self.max_rounds)
        run.outcome = "max_rounds"
        return "(reached maximum tool-call rounds)"

    def _model_round(self, current_round: int, run, on_token, on_reasoning):
        """One model call: trace it, log it, and account for its tokens.

        history + system + tool schemas, the same composition the compression
        calls and the 400 fallback use; _full_messages() already carries the
        system message, so adding the fixed overhead to it would count that
        prompt twice
        """
        estimated_prompt = estimate_tokens(self.messages) + self._fixed_prompt_tokens()
        call_started = time.perf_counter()
        call_cost_before = self._estimated_cost()
        retried = False
        with self._trace.llm_round(
            current_round, model=self._model, estimated_prompt=estimated_prompt,
            messages=self._trace_messages,
        ) as call:
            try:
                resp = self._chat_once(on_token, on_reasoning)
            except Exception as e:
                # provider tokenizers and our estimate never agree exactly, so a
                # rejection is the last line of defence: collapse once and
                # re-issue. A too-short history cannot free enough to help.
                if not is_context_length_error(e) or len(self.messages) <= 4:
                    raise
                resp, after_tokens = self._recover_overflow(
                    e, on_token, on_reasoning)
                retried = True
                # what the provider answered is the collapsed request, so the
                # number that ships must be the one measured after the collapse:
                # pairing the pre-rejection snapshot with this usage is the 919x
                # dirty pair the re-check window read in both sinks
                estimated_prompt = after_tokens
            metrics = _call_metrics(resp, call_started, call_cost_before,
                                    self._estimated_cost(), retried, estimated_prompt)
            log_event("llm_call", round=current_round, model=self._model,
                      schema_tokens=estimate_tokens(
                          [{"content": self._schema_json_str()}]), **metrics)
            call.record(metrics)
            call.capture_output(resp.content or "", _tool_call_view(resp))
        self._account_usage(run, resp)
        return resp

    def _chat_once(self, on_token, on_reasoning):
        return self.llm.chat(
            messages=self._full_messages(), tools=self._tool_schemas(),
            on_token=on_token, on_reasoning=on_reasoning)

    def _recover_overflow(self, error, on_token, on_reasoning):
        """The provider called the request too long: collapse once and retry.

        Returns the response and the estimate of the request that was actually
        re-issued, so the caller can report the number this reply belongs to
        rather than the one that got rejected.
        """
        before_messages = len(self.messages)
        before_tokens = estimate_tokens(self.messages) + self._fixed_prompt_tokens()
        self.context.force_collapse(self.messages, self.llm)
        after_tokens = estimate_tokens(self.messages) + self._fixed_prompt_tokens()
        error_type = type(error).__name__
        log_event(
            "context_overflow",
            messages_before=before_messages,
            messages_after=len(self.messages),
            estimated_before=before_tokens,
            estimated_after=after_tokens,
            error_type=error_type,
        )
        # the trace event stays without the estimates: a span event is a marker
        self._trace.event("context_overflow", messages_before=before_messages,
                          messages_after=len(self.messages), error_type=error_type)
        return self._chat_once(on_token, on_reasoning), after_tokens

    def _account_usage(self, run, resp) -> None:
        """Fold one call's usage into the run totals, then enforce the budget.

        Only reported usage becomes a total; unreported zeros stay unknown. The
        budget check runs after accounting so the round that tips the scale is
        the one that trips the fuse.
        """
        if resp.usage_reported:
            run.tokens_in += resp.prompt_tokens
            run.tokens_out += resp.completion_tokens
        else:
            run.usage_reported = False
        self._check_budget(run)

    def _check_budget(self, run) -> None:
        """Stop the run once its accrued cost crosses the configured ceiling."""
        if self.budget_ceiling is None:
            return
        spent = _cost_delta(run.cost_before, self._estimated_cost())
        if spent is None:
            return  # unpriced model: nothing mechanical to enforce against
        if spent > self.budget_ceiling:
            self._record("budget_exceeded", level=logging.WARNING,
                         spent_usd=round(spent, 6),
                         ceiling_usd=self.budget_ceiling, rounds=run.rounds)
            raise BudgetExceeded(
                f"budget ceiling exceeded: ${spent:.4f} > ${self.budget_ceiling:.4f}"
            )

    def _tool_round(self, resp, on_tool, current_round: int, run) -> None:
        """Execute one round of tool calls, then backfill any missing replies."""
        self.messages.append(resp.message)
        run.tool_calls += len(resp.tool_calls)
        try:
            self._execute_round(resp, on_tool, round_idx=current_round, stats=run)
        finally:
            # any escape from the round (Ctrl+C, a crashing on_tool or ask
            # callback, a BaseException from a tool) would leave the
            # assistant tool_calls message without replies and poison
            # every later request, so the backfill is unconditional and
            # idempotent: on a clean round it is a no-op
            self._answer_pending_tool_calls(resp.tool_calls)

    def _refuse_truncated(self, resp, consecutive: int, current_round: int) -> None:
        """Answer every truncated tool call with a hint instead of running it."""
        self._record("output_truncated", consecutive=consecutive,
                     tool_calls=len(resp.tool_calls), round=current_round)
        self._answer_truncated_tool_calls(resp)
        if consecutive >= 2:
            # the history is already valid, so a retry or /save still works
            raise TruncatedOutput(
                "model output truncated twice; raise max_tokens "
                "(CORECODER_MAX_TOKENS) or reduce the request"
            )

    def _record(self, name: str, level: int = logging.INFO, **fields) -> None:
        """One event for both sinks: the audit line and the run's span."""
        log_event(name, level=level, **fields)
        self._trace.event(name, **fields)

    def _finish_run(self, run) -> None:
        """Close the run: the audit summary first, then the root span."""
        try:
            self._log_run_summary(run)
        finally:
            fields = {
                "outcome": run.outcome,
                "rounds": run.rounds,
                "tool_calls": run.tool_calls,
                "tools_executed": run.executed,
                "tokens_in": run.tokens_in,
                "tokens_out": run.tokens_out,
                "usage_reported": run.usage_reported,
                "cost_usd": _cost_delta(run.cost_before, self._estimated_cost()),
            }
            if run.error_type is not None:
                fields["error"] = run.error_type
            if run.answer is not None:
                # the answer is content, so the capture switch filters it downstream
                # like any other text: nothing here decides whether it is recorded
                fields["output"] = run.answer
            self._trace.end_run(**fields)

    def _estimated_cost(self) -> float | None:
        return getattr(self.llm, "estimated_cost", None)

    def _log_run_summary(self, run) -> None:
        from . import __version__
        log_event(
            "run_summary",
            session=self.session_id,
            version=__version__,
            build=os.getenv("CORECODER_BUILD", ""),
            model=self._model,
            outcome=run.outcome,
            # the word "error" on its own is not actionable: a provider 400, a
            # crashed callback and a tool calling sys.exit would all read alike
            error_type=run.error_type,
            rounds=run.rounds,
            tool_calls=run.tool_calls,
            tools_executed=run.executed,
            tokens_in=run.tokens_in,
            tokens_out=run.tokens_out,
            usage_reported=run.usage_reported,
            cost_usd=_cost_delta(run.cost_before, self._estimated_cost()),
            duration_ms=round((time.perf_counter() - run.started) * 1000, 3),
        )

    def _answer_truncated_tool_calls(self, resp) -> None:
        """M2: record a truncated response and reply to every tool call with a
        re-issue hint, so nothing executes and the history stays valid."""
        self.messages.append(resp.message)
        for tc in resp.tool_calls:
            self.messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": (
                    "[truncated by max_tokens; re-issue the call "
                    "with smaller arguments]"
                ),
            })

    def _execute_round(self, resp, on_tool=None, round_idx=None, stats=None) -> None:
        """Execute one tool-call round: single calls inline, multiple calls in
        parallel, appending one tool reply per call to the history."""
        if len(resp.tool_calls) == 1:
            self._execute_single(resp.tool_calls[0], on_tool, round_idx, stats)
            return
        # parallel execution for multiple tool calls
        results = self._exec_tools_parallel(
            resp.tool_calls, on_tool, round_idx, stats=stats)
        for tc, result in zip(resp.tool_calls, results):
            self._append_tool_reply(tc, result)

    def _execute_single(self, tc, on_tool, round_idx, stats) -> None:
        """Run one call inline, closing its span even when the call escapes."""
        if on_tool:
            on_tool(tc.name, tc.arguments)
        span = self._trace.tool_span(
            tool=tc.name, round_idx=round_idx, args_chars=self._args_chars(tc))
        try:
            result = self._settle_and_run(tc, round_idx, stats, span=span)
        except BaseException as e:
            # an escape (Ctrl+C, a BaseException from a tool or a hook) skips
            # _log_result, which is what normally ends the span. Left open it
            # would lose the call from the trace and keep naming it in every
            # later audit line, so close it here; end() is idempotent.
            self._trace.tool_failed(span, e)
            raise
        self._append_tool_reply(tc, result)

    def _settle_and_run(self, tc, round_idx, stats, span=None) -> str:
        """Settle consent for one call, execute it, record the outcome."""
        # a call with broken arguments is rejected before hooks and
        # consent: no side effect, and nobody is asked to approve junk
        start = time.perf_counter()
        result, decision = self._decide(tc)
        # sub-millisecond decisions (a fast hook, an auto-approved call) must
        # not collapse to a misleading zero, same as duration_ms below
        decision_ms = round((time.perf_counter() - start) * 1000, 3)
        self._log_decision(tc, decision, decision_ms, round_idx)
        meta: dict = {"decision_ms": decision_ms}
        if result is None:
            result = self._exec_tool(tc, meta)
            self._post_hooks(tc, result)
            if stats is not None and not meta.get("refused"):
                stats.executed += 1
        else:
            meta.update(chars_in=len(result), chars_out=len(result),
                        error_text=result.startswith("Error:"))
        self._log_result(tc, decision, meta, round_idx, result=result, span=span)
        return result

    def _append_tool_reply(self, tc, result: str) -> None:
        self.messages.append({
            "role": "tool",
            "tool_call_id": tc.id,
            "content": result,
        })

    @staticmethod
    def _round_fields(round_idx) -> dict:
        # an omitted round means "the caller did not know", not round zero
        return {} if round_idx is None else {"round": round_idx}

    @staticmethod
    def _args_chars(tc) -> int:
        raw = getattr(tc, "raw_arguments", "") or ""
        if raw:
            return len(raw)
        arguments = getattr(tc, "arguments", None) or {}
        if not arguments:
            return 0
        try:
            return len(json.dumps(arguments, ensure_ascii=False))
        except (TypeError, ValueError):
            return 0

    def _log_decision(self, tc, decision: str, decision_ms: float, round_idx=None) -> None:
        log_event(
            "tool_decision", **self._round_fields(round_idx), tool=tc.name,
            call_id=tc.id, decision=decision, args_chars=self._args_chars(tc),
            decision_ms=decision_ms,
        )

    def _log_result(self, tc, decision: str, meta: dict, round_idx=None,
                    result: str | None = None, span=None) -> None:
        """Record one call's outcome: the audit line first, then its span."""
        fields = _result_fields(decision, meta)
        if span is None:
            span = self._trace.tool_span(
                tool=tc.name, round_idx=round_idx, args_chars=self._args_chars(tc),
                measured_ms=fields["duration_ms"], started_at=meta.get("started_at"))
        # logged while the span is open: the audit line then carries the same
        # trace_id/span_id as the trace record describing this call
        log_event(
            "tool_result", **self._round_fields(round_idx), tool=tc.name,
            call_id=tc.id, **fields,
        )
        if decision == "bad_arguments":
            self._trace_withheld.add(tc.id)
        self._trace.tool_finished(
            span, tool=tc.name, call_id=tc.id, decision=decision, attrs=fields,
            request_text=_request_text(tc), response_text=result)

    @staticmethod
    def _bad_arguments(tc) -> str | None:
        """Reject a call whose JSON never parsed (truncated or malformed).

        Without this, `json.loads` failure silently becomes `arguments={}`,
        built-in tools are stopped by `inspect.signature.bind`, but MCP tools
        (`execute(**kwargs)`) bind {} and run with their defaults.
        """
        error = getattr(tc, "arguments_error", "")
        if not error:
            return None
        raw = getattr(tc, "raw_arguments", "") or error
        # the tail is where a truncated JSON usually breaks (unterminated
        # string, dangling brace), so a head-only excerpt hides the cause
        if len(raw) <= 200:
            shown = repr(raw)
        else:
            shown = f"{raw[:120]!r}... ({len(raw)} chars total) ...{raw[-80:]!r}"
        return (
            f"Error: bad arguments for {tc.name}: the arguments were cut off or "
            f"are not a valid JSON object ({shown}); the call was NOT executed. "
            "Re-issue it with one complete, smaller JSON object."
        )

    def _pre_hooks(self, tc) -> str | None:
        """PreToolUse hooks, fired before consent. A string return blocks the
        call and becomes the tool result the model sees; None lets it through."""
        if self.hooks is None:
            return None
        return self.hooks.run_pre(tc.name, tc.arguments)

    def _post_hooks(self, tc, result: str):
        """PostToolUse hooks observe a finished call; they can never block."""
        if self.hooks is not None:
            self.hooks.run_post(tc.name, tc.arguments, result)

    def _permit(self, tc) -> str | None:
        """Consent check for one call. None means go ahead; a string is the
        refusal, returned as the tool result instead of executing."""
        return self._plan_mode_block(tc) or self._check_permission(tc)

    def _plan_mode_block(self, tc) -> str | None:
        """Plan mode outranks consent, even --yes: while it's on nothing mutates."""
        if self.plan_mode and tc.name not in Permission.READ_ONLY:
            return (
                "Plan mode is on, so this call was refused: plan mode is "
                "read-only. Do not retry it. Keep investigating with the "
                "read-only tools, then present the plan and stop. The user can "
                'approve it by typing "approve", or exit plan mode with /plan.'
            )
        return None

    def _check_permission(self, tc) -> str | None:
        if self.permission is None:
            return None
        return self.permission.check(tc.name, tc.arguments)

    def _decide(self, tc) -> tuple[str | None, str]:
        """Settle one call before execution: the refusal text (or None) and
        the reason. Broken arguments short-circuit hooks and consent."""
        result = self._bad_arguments(tc)
        if result is not None:
            return result, "bad_arguments"
        result = self._pre_hooks(tc)
        if result is not None:
            return result, "pre_hook"
        result = self._plan_mode_block(tc)
        if result is not None:
            return result, "plan_mode"
        result = self._check_permission(tc)
        if result is not None:
            return result, "denied"
        return None, "execute"

    def _exec_tool(self, tc, meta: dict | None = None) -> str:
        """Execute one tool call and govern its result before it hits history."""
        meta = {} if meta is None else meta
        start = time.perf_counter()
        started_at = time.time()
        try:
            raw = self._exec_tool_raw(tc, meta)
        finally:
            # sub-millisecond tools must not collapse to a misleading zero
            meta["duration_ms"] = round((time.perf_counter() - start) * 1000, 3)
            # wall-clock anchor for a span opened later, on the caller's thread
            meta["started_at"] = started_at
        return self._govern_result(raw, meta)

    def _govern_result(self, result: str, meta: dict | None = None) -> str:
        """Clamp and redact a tool result: the single exit for every path."""
        meta = {} if meta is None else meta
        limit = self.max_tool_result_chars
        chars_in = len(result)
        truncated = chars_in > limit
        meta["chars_in"] = chars_in
        if truncated:
            head = limit * 2 // 3
            tail = limit - head
            result = (
                result[:head]
                + f"\n... (truncated: {chars_in} chars total, "
                  f"showing first {head} and last {tail}) ...\n"
                + result[-tail:]
            )
        text = _redact(result)
        meta["truncated"] = truncated
        meta["redacted"] = text != result
        meta["chars_out"] = len(text)
        meta["error_text"] = text.startswith("Error:")
        return text

    def _exec_tool_raw(self, tc, meta: dict | None = None) -> str:
        """Execute a single tool call, returning the raw result string."""
        meta = {} if meta is None else meta
        tool = self._tool_by_name.get(tc.name)
        if tool is None:
            meta["error_type"] = "unknown_tool"
            return f"Error: unknown tool '{tc.name}'"
        # second line of defence: every execution path lands here, including
        # parallel workers, so broken arguments can never reach a tool
        bad = self._bad_arguments(tc)
        if bad is not None:
            meta["error_type"] = "bad_arguments"
            return bad
        # validate arguments first so a TypeError raised *inside* the tool isn't
        # mislabelled as a bad-arguments error from the caller
        try:
            inspect.signature(tool.execute).bind(**tc.arguments)
        except TypeError as e:
            meta["error_type"] = "TypeError"
            return f"Error: bad arguments for {tc.name}: {e}"
        # a tool that blows up gets reported back as text, never kills the loop
        try:
            out = tool.execute(**tc.arguments)
        except Exception as e:  # noqa: BLE001
            meta["error_type"] = type(e).__name__
            return f"Error executing {tc.name}: {e}"
        if isinstance(out, ToolRefused):
            meta["refused"] = True
        return out

    @staticmethod
    def _await_future(future, poll: float = 0.1):
        """Wait for one worker while staying responsive to Ctrl+C.

        A bare `future.result()` blocks in a lock wait, where a pending
        interrupt is only delivered once that wait returns (a 2s tool would
        swallow Ctrl+C for 2s). Polling lets the interpreter check the pending
        signal between waits.
        """
        while True:
            try:
                return future.result(timeout=poll)
            except concurrent.futures.TimeoutError:
                if future.done():
                    # re-raise whatever the worker stored (a TimeoutError from
                    # the tool must not be mistaken for our poll timing out)
                    return future.result()

    def _exec_tools_parallel(self, tool_calls, on_tool=None, round_idx=None,
                             stats=None) -> list[str]:
        """Run multiple tool calls concurrently using threads.

        This is inspired by Claude Code's StreamingToolExecutor which starts
        executing tools while the model is still generating.  We simplify to:
        when the model returns N tool calls at once, run them in parallel.

        Interrupting (Ctrl+C) stops waiting immediately and cancels queued
        calls. A call already running keeps going: Python threads cannot be
        killed, so it may still write to the workspace after chat() returns;
        its result is discarded and the history gets `[interrupted]`.
        """
        # hooks and consent are settled up front on this thread: prompting
        # from pool workers would interleave several prompts on one terminal.
        # broken arguments short-circuit both: they are refused, not approved
        results, metas = self._settle_batch(tool_calls, on_tool, round_idx)
        self._run_workers(tool_calls, metas, results, round_idx)
        for i in metas:
            self._post_hooks(tool_calls[i], results[i])
        if stats is not None:
            stats.executed += sum(
                1 for meta in metas.values() if not meta.get("refused"))
        return results

    def _settle_batch(self, tool_calls, on_tool, round_idx) -> tuple[list, dict]:
        """Announce and settle every call, then record the ones already decided."""
        for tc in tool_calls:
            if on_tool:
                on_tool(tc.name, tc.arguments)
        settled: list[tuple[str | None, str, float]] = []
        results: list[str | None] = []
        for tc in tool_calls:
            start = time.perf_counter()
            result, decision = self._decide(tc)
            decision_ms = round((time.perf_counter() - start) * 1000, 3)
            self._log_decision(tc, decision, decision_ms, round_idx)
            settled.append((result, decision, decision_ms))
            results.append(result)
        metas: dict = {}
        for i, tc in enumerate(tool_calls):
            result, decision, decision_ms = settled[i]
            if result is None:
                metas[i] = {"decision_ms": decision_ms}
                continue
            meta = {"decision_ms": decision_ms, "chars_in": len(result),
                    "chars_out": len(result), "error_text": result.startswith("Error:")}
            self._log_result(tc, decision, meta, round_idx, result=result)
        return results, metas

    def _run_workers(self, tool_calls, metas, results, round_idx) -> None:
        """Submit the settled calls, record each as it finishes, follow the cd.

        The tracked cwd is thread-local, so pool workers would otherwise start
        from the launch directory: hand the session cwd in, and merge any cd
        back in call order so the batch behaves like a sequence.
        """
        session_cwd = get_tracked_cwd()
        # not a `with` block: its __exit__ calls shutdown(wait=True), so Ctrl+C
        # would block until every running tool returned. Queued calls are
        # cancelled instead; a call already running cannot be killed (threads
        # cannot be interrupted in Python), so it finishes in the background
        # and its result is dropped. Pre-bound so an interrupt during submit
        # still reports submitted=0 instead of raising NameError.
        futures: dict = {}
        pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=8, thread_name_prefix="cc-tool")
        try:
            futures = {
                i: pool.submit(self._worker_call, i, tc, session_cwd, metas[i])
                for i, tc in enumerate(tool_calls)
                if i in metas
            }
            for i, future in futures.items():
                i, results[i], worker_cwd = self._await_future(future)
                self._log_result(tool_calls[i], "execute", metas[i], round_idx,
                                 result=results[i])
                if worker_cwd is not None:
                    session_cwd = worker_cwd
                    set_tracked_cwd(worker_cwd)
        except BaseException:
            inflight = sum(not f.done() for f in futures.values())
            self._record("batch_interrupted", level=logging.WARNING,
                         submitted=len(futures), inflight=inflight)
            raise
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    def _worker_call(self, i, tc, base_cwd, meta):
        """One pool slot: run the tool, then report the cwd it left behind."""
        is_bash = tc.name == "bash"
        if is_bash and base_cwd is not None:
            set_tracked_cwd(base_cwd)
        out = self._exec_tool(tc, meta)
        if not is_bash:
            return i, out, None
        cwd_after = get_tracked_cwd()
        # only a worker that actually moved the dir reports back; a
        # sibling that merely inherited the base must not revert a cd
        return i, out, cwd_after if cwd_after != base_cwd else None

    def _answer_pending_tool_calls(self, tool_calls):
        """Backfill a tool reply for every call that didn't get one.

        OpenAI-compatible APIs reject a request where an assistant message has
        tool_calls without a matching tool reply for each id, so this keeps the
        history valid when execution is interrupted partway through.
        """
        answered = {m.get("tool_call_id") for m in self.messages if m.get("role") == "tool"}
        pending = [tc for tc in tool_calls if tc.id not in answered]
        for tc in pending:
            self.messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": "[interrupted]",
            })
        if pending:
            self._record("backfill", count=len(pending))

    def reset(self):
        """Clear conversation history."""
        self.messages.clear()
        # a fresh conversation starts from the minimal catalog again: tools the
        # previous turn searched for are not carried over unless still eager
        if self._defer_mcp:
            self._loaded.clear()
            self._schema_sig = None
            self._build_catalog()
        # a user-visible reset must not leak the dead conversation's checklist
        # into the next system prompt
        todo = self._tool_by_name.get("todo_write")
        if isinstance(todo, TodoWriteTool):
            todo._tasks = []


# Secrets that most often leak through command output or remote tool results.
# Kept as whole-token patterns so ordinary words survive the pass.
_REDACTIONS = (
    (re.compile(r"sk-[A-Za-z0-9_-]{16,}"), "[REDACTED:api_key]"),
    (re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{8,}", re.IGNORECASE), "Bearer [REDACTED]"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "[REDACTED:aws_key]"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
     "[REDACTED:private_key]"),
)


def _redact(text: str) -> str:
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def _request_text(tc) -> str:
    """A call's arguments as text for content capture, secrets redacted."""
    try:
        return _redact(json.dumps(tc.arguments, ensure_ascii=False))
    except (TypeError, ValueError):
        return _redact(str(tc.arguments))


def _tool_call_view(resp) -> list | None:
    """The model's selection intent for the round's output, arguments redacted.

    ``None`` when the round called nothing, so a pure-text round keeps its plain
    string output. Only ``tc.arguments`` (the parsed dict, ``{}`` for broken
    JSON) is rendered, so a truncated stream's raw text never reaches the trace.
    """
    if not resp.tool_calls:
        return None
    return [{"name": tc.name, "arguments": _request_text(tc)}
            for tc in resp.tool_calls]


def _result_fields(decision: str, meta: dict) -> dict:
    """The outcome vocabulary one tool call reports to both sinks."""
    refused = decision == "execute" and meta.get("refused", False)
    effective = "tool_refused" if refused else decision
    if effective == "execute":
        outcome = "error" if meta.get("error_type") else "ok"
    else:
        outcome = "skipped"
    return {
        "effective_decision": effective,
        "outcome": outcome,
        "error_text": bool(meta.get("error_text", False)),
        "duration_ms": meta.get("duration_ms", 0),
        "decision_ms": meta.get("decision_ms", 0),
        "chars_in": meta.get("chars_in", 0),
        "chars_out": meta.get("chars_out", 0),
        "truncated": bool(meta.get("truncated", False)),
        "redacted": bool(meta.get("redacted", False)),
        "error_type": meta.get("error_type", ""),
    }


def _call_metrics(resp, call_started: float, cost_before, cost_after,
                  retried: bool, estimated_prompt: int) -> dict:
    """The fields one model call reports to both sinks.

    ``estimated_prompt`` belongs here and not at the call site on purpose: the
    audit line and the round's span must read the same number out of the same
    dict, otherwise one sink can be repaired while the other keeps a stale one.
    """
    return {
        "estimated_prompt": estimated_prompt,
        "prompt_tokens": resp.prompt_tokens if resp.usage_reported else None,
        "completion_tokens": resp.completion_tokens if resp.usage_reported else None,
        "usage_reported": resp.usage_reported,
        "finish_reason": resp.finish_reason,
        "tool_calls": len(resp.tool_calls),
        "duration_ms": round((time.perf_counter() - call_started) * 1000, 3),
        "retried": retried,
        "cost_usd": _cost_delta(cost_before, cost_after),
    }


def _outcome_of(exc: BaseException) -> str:
    """The run-summary vocabulary for a failure that escaped the round loop."""
    if isinstance(exc, TruncatedOutput):
        return "truncated"
    if isinstance(exc, BudgetExceeded):
        return "budget"
    if isinstance(exc, KeyboardInterrupt):
        return "interrupted"
    return "error"


def _cost_delta(before: float | None, after: float | None) -> float | None:
    """Per-call cost: a delta of the running estimate, never an absolute total."""
    if before is None or after is None:
        return None
    return round(after - before, 6)
