"""Compile / execute user strategy scripts, and contain how long they run.

A user script is ordinary Python that defines `decide(ctx)` (plus optional
`decide_market` / `manage` / `decide_signal` / `supervise` /
`on_start` / `on_fill` / `on_settle` hooks).

NO LANGUAGE SANDBOX. Scripts run as full Python with the real builtins, in
the same process as the trading engine — which is the process holding the
decrypted wallet key. This is deliberate: the AST whitelist that used to
gate scripts blocked imports, classes, and most of the standard library,
which made every non-trivial strategy fight the tool, while never being a
real security boundary (CPython sandboxes are escapable, and the module said
so). Rather than pretend, the app now REVIEWS instead of restricting:
`script_audit.py` statically scans every script for network, filesystem,
subprocess, dynamic-exec, environment and wallet/key access — plus
obfuscation, which is flagged as its own critical finding — and the Scripts
page shows those findings before the script is enabled or armed.

Read a script before you run it. The audit is a smoke detector, not a lock.

WHAT THIS MODULE STILL GUARANTEES is liveness, not safety: a script cannot
wedge the engine.

  • Live engine — every hook runs in a joined daemon thread with a hard
    wall-clock timeout (`script_engine._run_bounded`). A hook that blocks
    past it is abandoned and the script is auto-disabled, so the master loop
    keeps its cadence no matter what the script does.
  • Backtest — thousands of hook calls per run, far too many to give each
    its own thread, so the replay path opts into `traced=True`: a wall-clock
    tracer that raises inside the script's own frame. Without it a single
    `while True:` in decide() would hang the backend's RPC forever.

The MONEY rails (max entry price, max size, max open, per-script daily-loss
breaker, one-entry-per-window) live in script_engine and apply to every
script unconditionally. Removing the language sandbox did not touch them.
"""
from __future__ import annotations

import ast
import hashlib
import math
import statistics
import sys
import threading
import time
from typing import Any, Callable, Optional

import script_audit

# This module parses and compiles user source too; see script_audit for why
# the recursion limit has to be pinned before any of it.
script_audit.pin_recursion_limit()

MAX_CODE_BYTES = 128 * 1024
HOOK_NAMES = ("decide", "decide_market", "manage", "decide_signal",
              "supervise", "on_start", "on_fill", "on_settle")
# A script must define at least one of these to DO anything.
ENTRY_HOOKS = ("decide", "decide_market", "manage", "decide_signal",
               "supervise")

# Names injected into every script's globals. Scripts may `import` anything
# they like now; these stay because they cover the common cases without a
# import line, and because every shipped example and every script written
# against the old sandbox relies on them.
INJECTED_GLOBALS = ("math", "statistics", "state", "log", "ctx")


class ScriptError(Exception):
    """A script failed validation, compilation, or raised at runtime."""


class ScriptBudgetExceeded(BaseException):
    """A hook call exceeded its wall-clock budget (runaway-loop guard).

    Deliberately NOT an Exception subclass. The tracer raises this INSIDE the
    script's own frame, so a script's `except Exception:` would otherwise
    swallow it — and because CPython drops the trace function the moment a
    trace callback raises, the budget would then be gone for the rest of that
    call (a runaway loop running unbounded, hanging the backtest).
    BaseException walks straight past every `except Exception`, and
    call_budgeted() re-checks after the call for anything that still gets by.

    Engine code that means "any script failure" must catch ScriptError AND
    this (or BaseException) — see _run_bounded in script_engine.py."""


# "Any script failure" for engine/backtest call sites. ScriptBudgetExceeded is
# intentionally outside the Exception hierarchy, so `except ScriptError` — and
# `except Exception` — miss it. Catch this tuple wherever the intent is "the
# script misbehaved, disable it and carry on".
SCRIPT_FAILURES = (ScriptError, ScriptBudgetExceeded)


# ───────── header / metadata ─────────────────────────────────────────────


def parse_header(code: str) -> dict:
    """Read the `# krypt-script v1` metadata header from the top comment
    block: name / description lines. Missing header is not fatal (the save
    path fills defaults) but the AI import flow requires it."""
    meta = {"version": None, "name": "", "description": ""}
    for line in code.splitlines()[:15]:
        s = line.strip()
        if not s.startswith("#"):
            if s:  # first non-comment, non-blank line ends the header block
                break
            continue
        body = s.lstrip("#").strip()
        low = body.lower()
        if low.startswith("krypt-script"):
            meta["version"] = body.split()[-1] if len(body.split()) > 1 else "v1"
        elif low.startswith("name:"):
            meta["name"] = body[5:].strip()
        elif low.startswith("description:"):
            meta["description"] = body[12:].strip()
    return meta


def find_ctx_fields(code: str) -> list[str]:
    """Static scan of the ctx fields a script reads (`ctx["x"]` and
    `ctx.get("x")`), so validation can warn when a field isn't recorded in
    ticks (it would be None in backtest — fail-closed, same as rules)."""
    fields: set[str] = set()
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Subscript)
                and isinstance(node.value, ast.Name) and node.value.id == "ctx"
                and isinstance(node.slice, ast.Constant)
                and isinstance(node.slice.value, str)):
            fields.add(node.slice.value)
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "ctx"
                and node.args and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)):
            fields.add(node.args[0].value)
    return sorted(fields)


# ───────── validation ────────────────────────────────────────────────────


def validate(code: str) -> list[str]:
    """Errors that make a script unrunnable: too big, doesn't parse, or
    defines none of the hooks the engine calls. Returns [] when the script
    will at least load.

    This is a CORRECTNESS check, not a safety one — what a script is allowed
    to DO is no longer restricted here. For "is this script doing something
    alarming", call script_audit.audit(); its findings are surfaced beside
    these errors in the UI and never block a save.
    """
    if len(code.encode("utf-8", "replace")) > MAX_CODE_BYTES:
        return [f"script exceeds {MAX_CODE_BYTES // 1024}KB"]
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return [f"syntax error line {e.lineno}: {e.msg}"]
    defined = {n.name for n in tree.body
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    if not defined & set(ENTRY_HOOKS):
        return ["script must define at least one of: decide(ctx), "
                "decide_market(market), manage(position, ctx), "
                "decide_signal(signal), supervise(app)"]
    # The engine calls hooks synchronously; a coroutine function would return
    # an un-awaited coroutine that silently does nothing. Catch it at save.
    async_hooks = sorted(
        n.name for n in tree.body
        if isinstance(n, ast.AsyncFunctionDef) and n.name in HOOK_NAMES)
    if async_hooks:
        return [f"hook(s) {', '.join(async_hooks)} are 'async def' — the engine "
                "calls hooks synchronously, so an async hook would never run. "
                "Use a plain 'def'."]
    return []


# ───────── budgeted execution ────────────────────────────────────────────


class _Tracer:
    """Wall-clock tracer with a sticky `tripped` flag. The flag is what makes
    the budget survive a script that intercepts the raise: once tripped, the
    call is rejected even if the exception never reaches call_budgeted.

    Time only — there is no step cap. A step cap punishes a legitimately
    heavy computation for being efficient about wall-clock, and the clock is
    the thing the engine actually cares about."""

    __slots__ = ("count", "deadline", "tripped", "why")

    def __init__(self, deadline: float) -> None:
        self.count = 0
        self.deadline = deadline
        self.tripped = False
        self.why = ""

    def __call__(self, frame, event, arg):  # noqa: ANN001 - trace signature
        self.count += 1
        if self.count % 64 == 0 and time.perf_counter() > self.deadline:
            self.tripped = True
            self.why = "script exceeded its per-call time budget"
            raise ScriptBudgetExceeded(self.why)
        return self


def call_budgeted(fn: Callable, *args: Any, budget_ms: float = 250.0,
                  traced: bool = False) -> Any:
    """Call a script hook. With `traced=False` (the live engine) the hook runs
    bare and the caller provides containment — script_engine runs every hook
    in a joined daemon thread with a hard timeout. With `traced=True` (the
    backtest, which makes thousands of calls and cannot afford a thread each)
    a wall-clock tracer bounds the call from inside.

    Two layers when traced, because the tracer alone is defeatable: it raises
    ScriptBudgetExceeded (a BaseException, so `except Exception:` misses it),
    and afterwards we reject any call that tripped the budget or that lost its
    trace function — CPython unsets tracing as soon as a trace callback raises,
    so a call that comes back untraced ran part of its work unmetered."""
    if not traced:
        return fn(*args)
    deadline = time.perf_counter() + budget_ms / 1000.0
    tracer = _Tracer(deadline)
    old = sys.gettrace()
    sys.settrace(tracer)
    lost = False
    try:
        out = fn(*args)
    finally:
        # Sample BEFORE restoring `old`, or the comparison is meaningless.
        lost = sys.gettrace() is not tracer
        sys.settrace(old)
    if tracer.tripped:
        raise ScriptBudgetExceeded(tracer.why or "script exceeded its per-call budget")
    if lost:
        raise ScriptBudgetExceeded(
            "script disabled its own time budget (the trace function was lost "
            "mid-call — usually a swallowed budget error)"
        )
    return out


# ───────── compile / module container ────────────────────────────────────


def code_hash(code: str) -> str:
    h = hashlib.sha256()
    h.update(code.encode("utf-8", "replace"))
    return h.hexdigest()


class CompiledScript:
    """A compiled script module: its hooks, persistent state dict, and log
    sink. One instance per (script, code-hash).

    `traced` is a per-instance default for hook calls: False for the live
    engine (thread-bounded by the caller), True for the backtest."""

    def __init__(self, script_id: str, code: str, *,
                 log_sink: Optional[Callable[[str], None]] = None,
                 state: Optional[dict] = None, traced: bool = False):
        self.script_id = script_id
        self.traced = bool(traced)
        self.hash = code_hash(code)
        self.state: dict = state if isinstance(state, dict) else {}
        # Set when a hook of this module blew its wall-clock timeout and its
        # thread was ABANDONED (still running, still holding `state`). We
        # cannot kill that thread, so the module is retired instead: no
        # further hook may be called on it, and script_engine drops it from
        # the compile cache so its (concurrently mutated) state is never
        # persisted and a re-enable compiles a fresh module.
        self.abandoned = threading.Event()
        self._log_lines: list[str] = []
        self._log_sink = log_sink
        self._log_count = 0

        errors = validate(code)
        if errors:
            raise ScriptError("; ".join(errors[:5]))

        def _log(*parts: Any) -> None:
            # Rate/volume-capped: a chatty script can't flood the event bus
            # or the backtest payload.
            self._log_count += 1
            if self._log_count > 2000:
                return
            msg = " ".join(str(p) for p in parts)[:400]
            self._log_lines.append(msg)
            if len(self._log_lines) > 200:
                del self._log_lines[:100]
            if self._log_sink:
                try:
                    self._log_sink(msg)
                except Exception:
                    pass

        # Real builtins — full Python. `print` is redirected to the script's
        # log sink so a print() lands in the Scripts page's log panel instead
        # of the backend's stdout, where the user would never find it.
        import builtins as _builtins
        g: dict[str, Any] = {"__builtins__": dict(_builtins.__dict__)}
        g["__builtins__"]["print"] = _log
        g.update({
            "math": math, "statistics": statistics,
            "state": self.state, "log": _log,
        })
        self.globals = g

        code_obj = compile(code, f"<script:{script_id[:8]}>", "exec")
        # Module body executes once. Bounded on the same terms as a hook call
        # (the live engine wraps this whole constructor in a timeout thread).
        call_budgeted(eval, code_obj, g, budget_ms=2000.0, traced=self.traced)
        self.hooks: dict[str, Callable] = {}
        for name in HOOK_NAMES:
            fn = g.get(name)
            if callable(fn):
                self.hooks[name] = fn
        if not set(self.hooks) & set(ENTRY_HOOKS):
            raise ScriptError(
                "script must define at least one of: decide, decide_market, "
                "manage, decide_signal, supervise")

    def drain_logs(self) -> list[str]:
        out = self._log_lines[:]
        self._log_lines.clear()
        return out

    def call(self, hook: str, *args: Any, budget_ms: float = 250.0) -> Any:
        """Invoke a hook. Raises ScriptError on any in-script failure and
        ScriptBudgetExceeded (a BaseException) when a traced call runs past
        its budget — callers catch SCRIPT_FAILURES and isolate (disable the
        script), never crash."""
        if self.abandoned.is_set():
            # A previous hook is still running away with this module's state.
            # Calling into it again would race that thread (and re-enter user
            # code the engine has already given up on).
            raise ScriptError(
                f"{hook}() refused: a previous hook overran its time budget "
                "and its thread was abandoned — this module is retired")
        fn = self.hooks.get(hook)
        if fn is None:
            return None
        try:
            return call_budgeted(fn, *args, budget_ms=budget_ms,
                                 traced=self.traced)
        except ScriptBudgetExceeded:
            raise
        except Exception as e:
            raise ScriptError(f"{hook}() raised {type(e).__name__}: {e}") from e
