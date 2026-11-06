"""Static risk audit for user strategy scripts.

Scripts run as full, unsandboxed Python inside the process that holds the
decrypted wallet key (see script_sandbox for why). This module is what
replaced the language sandbox: instead of restricting what a script may do,
it READS the script and reports what it appears to do, so the user reviewing
a pasted or AI-generated strategy sees the alarming parts before enabling it.

Be honest about the limits. This is an AST scan:

  • It cannot see through obfuscation. So obfuscation is itself a CRITICAL
    finding — a strategy script has no legitimate reason to base64-decode a
    blob, resolve attributes from computed strings, or exec() anything. A
    script that hides what it does is reported as hiding what it does, which
    is the only honest thing a static tool can say about it.
  • It is not a permission system. Nothing here blocks execution; the UI
    surfaces findings and makes the user confirm. A determined attacker who
    gets you to run their script wins regardless.
  • Clean findings mean "nothing alarming was recognized", never "safe".

The categories are ordered by what would actually cost the user money:
credential access and network egress first, then code that can escape review
(dynamic execution), then process/filesystem, then the merely noteworthy.
"""
from __future__ import annotations

import ast
import re
import sys
from typing import Any, Optional

# Refuse a deeply nested script at the default limit rather than let a library
# that raised the interpreter-wide limit turn it into a C-stack overflow that
# kills the backend with no traceback.
_RECURSION_LIMIT = 1000


def pin_recursion_limit() -> None:
    """Put the recursion limit back. Cheap and idempotent; called at import here
    and in script_sandbox, and before every audit."""
    if sys.getrecursionlimit() > _RECURSION_LIMIT:
        sys.setrecursionlimit(_RECURSION_LIMIT)


pin_recursion_limit()

# Severity ranks, used for sorting and for the UI's headline verdict.
_RANK = {"critical": 0, "warning": 1, "info": 2}

MAX_FINDINGS = 60

# ── module-level risk table ──────────────────────────────────────────────
# Keyed by the ROOT module name; a dotted import resolves to its root before
# lookup, so `from urllib.request import urlopen` and `import urllib` both
# land on "urllib".
_MODULE_RISK: dict[str, tuple[str, str, str]] = {
    # module: (severity, category, what it means)
    # — network egress: the transport any exfiltration needs
    "socket": ("critical", "network", "raw network sockets"),
    "ssl": ("warning", "network", "TLS sockets"),
    "http": ("critical", "network", "HTTP client/server"),
    "urllib": ("critical", "network", "HTTP requests"),
    "urllib3": ("critical", "network", "HTTP requests"),
    "requests": ("critical", "network", "HTTP requests"),
    "httpx": ("critical", "network", "HTTP requests"),
    "aiohttp": ("critical", "network", "HTTP requests"),
    "websocket": ("critical", "network", "websocket connections"),
    "websockets": ("critical", "network", "websocket connections"),
    "ftplib": ("critical", "network", "FTP transfers"),
    "smtplib": ("critical", "network", "sending email"),
    "poplib": ("critical", "network", "reading email"),
    "imaplib": ("critical", "network", "reading email"),
    "telnetlib": ("critical", "network", "telnet"),
    "xmlrpc": ("critical", "network", "XML-RPC calls"),
    "paramiko": ("critical", "network", "SSH connections"),
    "boto3": ("critical", "network", "AWS API calls"),
    "webbrowser": ("critical", "network", "opening URLs in your browser"),
    "dns": ("critical", "network", "DNS queries (a known exfiltration channel)"),
    # — dynamic execution / obfuscation: defeats this very audit
    "importlib": ("critical", "dynamic", "importing modules by computed name"),
    "marshal": ("critical", "dynamic", "loading raw code objects"),
    "pickle": ("critical", "dynamic", "unpickling (executes arbitrary code)"),
    "dill": ("critical", "dynamic", "unpickling (executes arbitrary code)"),
    "shelve": ("critical", "dynamic", "unpickling (executes arbitrary code)"),
    "base64": ("critical", "dynamic", "base64 decoding (hides what the code does)"),
    "binascii": ("critical", "dynamic", "binary/hex decoding (hides what the code does)"),
    "codecs": ("warning", "dynamic", "codec decoding (can hide code)"),
    "zlib": ("warning", "dynamic", "decompression (can hide code)"),
    "lzma": ("warning", "dynamic", "decompression (can hide code)"),
    "bz2": ("warning", "dynamic", "decompression (can hide code)"),
    # — process control / native code
    "subprocess": ("critical", "process", "running external programs"),
    "multiprocessing": ("critical", "process", "spawning processes"),
    "ctypes": ("critical", "process", "calling native code directly"),
    "cffi": ("critical", "process", "calling native code directly"),
    "pty": ("critical", "process", "pseudo-terminals"),
    "winreg": ("critical", "process", "reading/writing the Windows registry"),
    "signal": ("warning", "process", "process signals"),
    # — credentials / secrets
    "keyring": ("critical", "wallet", "reading the OS credential store"),
    "getpass": ("warning", "wallet", "prompting for or reading a password"),
    "secrets": ("info", "wallet", "cryptographic randomness"),
    # — filesystem
    "shutil": ("critical", "filesystem", "copying/moving/deleting file trees"),
    "pathlib": ("warning", "filesystem", "filesystem paths"),
    "glob": ("warning", "filesystem", "enumerating files on disk"),
    "tempfile": ("info", "filesystem", "temporary files"),
    "sqlite3": ("warning", "filesystem", "opening databases directly"),
    # — introspection / interpreter internals
    "inspect": ("warning", "introspection", "inspecting live objects and frames"),
    "gc": ("warning", "introspection", "walking every object in memory"),
    "traceback": ("info", "introspection", "traceback inspection"),
    "platform": ("info", "env", "machine fingerprinting"),
    "getopt": ("info", "env", "argument parsing"),
    "uuid": ("info", "env", "machine identifiers"),
}

# ── dotted-call risk table ───────────────────────────────────────────────
# Matched against the resolved dotted name of a call or attribute access.
_CALL_RISK: dict[str, tuple[str, str, str]] = {
    "os.system": ("critical", "process", "running a shell command"),
    "os.popen": ("critical", "process", "running a shell command"),
    "os.spawnl": ("critical", "process", "spawning a process"),
    "os.spawnv": ("critical", "process", "spawning a process"),
    "os.execl": ("critical", "process", "replacing this process"),
    "os.execv": ("critical", "process", "replacing this process"),
    "os.fork": ("critical", "process", "forking this process"),
    "os.kill": ("critical", "process", "killing a process"),
    "os.remove": ("critical", "filesystem", "deleting a file"),
    "os.unlink": ("critical", "filesystem", "deleting a file"),
    "os.rmdir": ("critical", "filesystem", "deleting a directory"),
    "os.removedirs": ("critical", "filesystem", "deleting directories"),
    "os.rename": ("critical", "filesystem", "renaming a file"),
    "os.replace": ("critical", "filesystem", "overwriting a file"),
    "os.truncate": ("critical", "filesystem", "truncating a file"),
    "os.chmod": ("critical", "filesystem", "changing file permissions"),
    "os.walk": ("warning", "filesystem", "enumerating files on disk"),
    "os.listdir": ("warning", "filesystem", "enumerating files on disk"),
    "os.environ": ("warning", "env", "reading environment variables"),
    "os.getenv": ("warning", "env", "reading environment variables"),
    "os.putenv": ("warning", "env", "setting environment variables"),
    "sys.argv": ("info", "env", "reading process arguments"),
    "sys.modules": ("critical", "introspection",
                    "reaching into already-imported app modules"),
    "sys._getframe": ("critical", "introspection", "walking caller frames"),
    "sys.settrace": ("critical", "introspection", "installing a trace hook"),
    "sys.setprofile": ("critical", "introspection", "installing a profile hook"),
    "sys.exit": ("warning", "process", "exiting the app"),
    "time.sleep": ("warning", "performance",
                   "sleeping inside a hook — this blocks the engine loop"),
    "socket.gethostname": ("warning", "env", "machine fingerprinting"),
}

# Bare builtins that matter regardless of how they're reached.
_BUILTIN_RISK: dict[str, tuple[str, str, str]] = {
    "eval": ("critical", "dynamic", "evaluating code built at runtime"),
    "exec": ("critical", "dynamic", "executing code built at runtime"),
    "compile": ("critical", "dynamic", "compiling code built at runtime"),
    "__import__": ("critical", "dynamic", "importing by computed name"),
    "breakpoint": ("warning", "process", "dropping into a debugger"),
    "globals": ("warning", "introspection", "reading the module globals dict"),
    "vars": ("warning", "introspection", "reading an object's attribute dict"),
    "memoryview": ("info", "introspection", "raw memory views"),
}

# Attribute names that expose interpreter/object internals from ANY object.
_ATTR_RISK: dict[str, tuple[str, str, str]] = {
    "__globals__": ("critical", "introspection",
                    "reading a function's globals (reaches app internals)"),
    "__subclasses__": ("critical", "introspection",
                       "enumerating every loaded class"),
    "__builtins__": ("critical", "introspection", "reaching the real builtins"),
    "__code__": ("critical", "introspection", "reading a function's bytecode"),
    "__closure__": ("critical", "introspection", "reading captured variables"),
    "__dict__": ("warning", "introspection", "reading an object's attribute dict"),
    "__class__": ("info", "introspection", "class introspection"),
    "__mro__": ("warning", "introspection", "walking the class hierarchy"),
    "f_back": ("critical", "introspection", "walking caller frames"),
    "f_globals": ("critical", "introspection", "reading a caller's globals"),
    "f_locals": ("critical", "introspection", "reading a caller's locals"),
    "gi_frame": ("critical", "introspection", "reading a generator's frame"),
    "cr_frame": ("critical", "introspection", "reading a coroutine's frame"),
    "fromhex": ("warning", "dynamic", "decoding hex (can hide code)"),
}

# ── the app's own modules ────────────────────────────────────────────────
# A script importing these reaches PAST the documented hook API into the
# running bot. `polymarket_auth` is the one that holds the decrypted key.
_APP_MODULES: dict[str, tuple[str, str]] = {
    "polymarket_auth": ("critical", "the module holding your DECRYPTED API KEY"),
    "polymarket_api": ("critical", "the authenticated exchange client "
                                   "(can place orders outside the rails)"),
    "trader": ("critical", "the order-placing engine (bypasses the script rails)"),
    "crypto15m_trader": ("critical", "the crypto order engine (bypasses the rails)"),
    "service": ("critical", "the backend service (full app control)"),
    "script_engine": ("critical", "the script engine itself"),
    "db": ("warning", "the app database directly"),
    "config": ("warning", "the app configuration directly"),
    "remote": ("critical", "the chat remote-control brain"),
    "bot_secrets": ("critical", "the stored chat-bot tokens"),
    "bot_discord": ("warning", "the Discord bot transport"),
    "bot_telegram": ("warning", "the Telegram bot transport"),
}

# Identifier / string fragments that look like credentials. Matched
# case-insensitively against variable names, attribute names, dict keys and
# string literals. Deliberately broad: a false positive costs one glance.
_SECRET_PAT = re.compile(
    r"(private[_-]?key|privkey|secret[_-]?key|api[_-]?secret|api[_-]?passphrase"
    r"|mnemonic|seed[_-]?phrase|passphrase|keystore|wallet[_-]?key"
    r"|signing[_-]?key|proxy[_-]?wallet|credential|\.env\b|settings\.json"
    r"|POLY_[A-Z_]*KEY|PK_[A-Z_]*)",
    re.IGNORECASE,
)

# A long, alphabet-restricted literal is the classic payload shape.
_B64_PAT = re.compile(r"^[A-Za-z0-9+/=\s]{200,}$")
_HEX_PAT = re.compile(r"^(?:0x)?[0-9a-fA-F\s]{200,}$")


def _dotted(node: ast.AST) -> Optional[str]:
    """Dotted name for an attribute chain rooted at a plain Name."""
    parts: list[str] = []
    cur: Any = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if not isinstance(cur, ast.Name):
        return None
    parts.append(cur.id)
    return ".".join(reversed(parts))


class _Scan(ast.NodeVisitor):
    def __init__(self) -> None:
        self.findings: list[dict] = []
        # local binding -> real dotted module path, so `import requests as r`
        # still resolves `r.get` to requests.
        self.aliases: dict[str, str] = {}
        self.categories: set[str] = set()

    # — emit —
    def add(self, node: ast.AST, severity: str, category: str,
            message: str) -> None:
        self.categories.add(category)
        self.findings.append({
            "severity": severity,
            "category": category,
            "line": int(getattr(node, "lineno", 0) or 0),
            "message": message,
        })

    def _resolve(self, dotted: str) -> str:
        """Rewrite an aliased root back to the real module path."""
        head, _, tail = dotted.partition(".")
        real = self.aliases.get(head)
        if not real:
            return dotted
        return f"{real}.{tail}" if tail else real

    def _check_module(self, node: ast.AST, mod: str, *, via: str) -> None:
        root = mod.split(".")[0]
        if root in _APP_MODULES:
            sev, what = _APP_MODULES[root]
            self.add(node, sev, "wallet",
                     f"{via} the app's own '{root}' module — {what}. The hook "
                     "API never requires this.")
            return
        risk = _MODULE_RISK.get(root)
        if risk:
            sev, cat, what = risk
            self.add(node, sev, cat, f"{via} '{mod}' — {what}.")

    # — imports —
    def visit_Import(self, node: ast.Import) -> None:
        for a in node.names:
            self.aliases[a.asname or a.name.split(".")[0]] = a.name
            self._check_module(node, a.name, via="imports")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        mod = node.module or ""
        if node.level:  # relative import inside a single-file script
            self.add(node, "critical", "dynamic",
                     "uses a relative import, which only resolves against app "
                     "internals.")
        for a in node.names:
            self.aliases[a.asname or a.name] = f"{mod}.{a.name}" if mod else a.name
        if mod:
            self._check_module(node, mod, via="imports from")
        self.generic_visit(node)

    # — calls —
    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        name = _dotted(func)
        if name:
            resolved = self._resolve(name)
            risk = _CALL_RISK.get(resolved)
            if risk:
                sev, cat, what = risk
                self.add(node, sev, cat, f"calls {resolved}() — {what}.")
            elif resolved.split(".")[0] in _MODULE_RISK and "." in resolved:
                sev, cat, what = _MODULE_RISK[resolved.split(".")[0]]
                self.add(node, sev, cat, f"calls {resolved}() — {what}.")

        if isinstance(func, ast.Name):
            risk = _BUILTIN_RISK.get(func.id)
            if risk:
                sev, cat, what = risk
                self.add(node, sev, cat, f"calls {func.id}() — {what}.")
            elif func.id == "open":
                self._check_open(node)
            elif func.id in ("getattr", "setattr", "delattr"):
                # A CONSTANT attribute name is ordinary code. A computed one is
                # exactly how a script reaches an attribute this audit can't see.
                if len(node.args) >= 2 and not (
                        isinstance(node.args[1], ast.Constant)
                        and isinstance(node.args[1].value, str)):
                    self.add(node, "critical", "dynamic",
                             f"calls {func.id}() with a COMPUTED attribute name "
                             "— this hides which attribute is touched.")
        # base64.b64decode / codecs.decode etc. are caught by the module rule
        # above; `bytes.fromhex` and `str.encode().decode()` chains are caught
        # by the attribute rule.
        self.generic_visit(node)

    def _check_open(self, node: ast.Call) -> None:
        mode = "r"
        if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
            mode = str(node.args[1].value or "r")
        for kw in node.keywords:
            if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                mode = str(kw.value.value or "r")
        writing = any(c in mode for c in "wax+")
        target = ""
        if node.args and isinstance(node.args[0], ast.Constant):
            target = f" ('{str(node.args[0].value)[:80]}')"
        if writing:
            self.add(node, "critical", "filesystem",
                     f"opens a file for WRITING{target}.")
        else:
            self.add(node, "warning", "filesystem",
                     f"reads a file from disk{target}.")

    # — attributes —
    def visit_Attribute(self, node: ast.Attribute) -> None:
        risk = _ATTR_RISK.get(node.attr)
        if risk:
            sev, cat, what = risk
            self.add(node, sev, cat, f"accesses .{node.attr} — {what}.")
        # Attributes that are risky as bare READS, not just as calls
        # (`os.environ["X"]`, `sys.argv[1]`, `sys.modules[...]`). Everything
        # else in _CALL_RISK is reported by the call visitor, which words it
        # better; reporting it here too would just duplicate the row.
        dotted = _dotted(node)
        if dotted:
            resolved = self._resolve(dotted)
            if resolved in ("os.environ", "sys.argv", "sys.modules"):
                sev, cat, what = _CALL_RISK[resolved]
                self.add(node, sev, cat, f"reads {resolved} — {what}.")
        if _SECRET_PAT.search(node.attr):
            self.add(node, "critical", "wallet",
                     f"accesses '.{node.attr}', a credential-shaped name.")
        self.generic_visit(node)

    # — names & literals —
    def visit_Name(self, node: ast.Name) -> None:
        if _SECRET_PAT.search(node.id):
            self.add(node, "critical", "wallet",
                     f"uses the credential-shaped identifier '{node.id}'.")
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str):
            s = node.value
            if _SECRET_PAT.search(s):
                self.add(node, "critical", "wallet",
                         f"contains a credential-shaped string: "
                         f"'{s[:60]}'.")
            elif len(s) >= 200 and (_B64_PAT.match(s) or _HEX_PAT.match(s)):
                self.add(node, "critical", "dynamic",
                         f"contains a {len(s)}-character encoded blob — a "
                         "strategy has no reason to carry an opaque payload.")
            elif re.match(r"^(https?|ftp|ws)://", s, re.IGNORECASE):
                self.add(node, "warning", "network",
                         f"contains a URL: '{s[:80]}'.")
        self.generic_visit(node)

    # — global mutation of app state —
    def visit_Global(self, node: ast.Global) -> None:
        self.generic_visit(node)


# What "we could not read this file" looks like when it arrives. `ast.parse`
# raises more than SyntaxError, and each of the others used to reach the save
# RPC as a 500 the user could not read:
#   RecursionError — a deeply nested expression, well under the source-size
#     cap, exhausts the parser's (or the walker's) stack. The default
#     recursion limit is 1,000; a script does not have to be large to pass it.
#   ValueError     — a NUL byte in the source, on Python 3.10/3.11. Later
#     versions report the same thing as a SyntaxError.
#   MemoryError    — the same class of resource exhaustion, one rung up.
_UNREADABLE = (SyntaxError, ValueError, RecursionError, MemoryError)

# Walking the tree can only ever run out of stack or memory. A ValueError from
# _Scan would be a bug in the SCANNER, and quietly reporting that as "your
# script is unparseable" would hide it from us and lie to the user, so it is
# deliberately not caught here.
_UNWALKABLE = (RecursionError, MemoryError)


def _unreadable(e: BaseException) -> dict:
    """The verdict for source we could not read.

    Refusing loudly is the honest answer. "Nothing found" on code that was
    never audited would read as a clean bill of health, so this carries a
    CRITICAL finding: `ok` is False, and an armed script whose code changes to
    something unreadable disarms itself rather than hot-swapping into it.
    """
    lineno = getattr(e, "lineno", None) if isinstance(e, SyntaxError) else None
    where = f" (syntax error line {lineno})" if lineno else ""
    if isinstance(e, RecursionError):
        detail = " It is nested too deeply to read."
    elif isinstance(e, SyntaxError):
        detail = ""
    else:
        detail = f" The parser rejected it outright ({type(e).__name__})."
    return {
        "ok": False, "parsed": False,
        "critical": 1, "warning": 0, "info": 0,
        "categories": ["audit"],
        "findings": [{
            "severity": "critical", "category": "audit", "line": lineno or 0,
            "message": ("This script could not be read, so it was not "
                        "audited — refused." + detail),
        }],
        "summary": f"Could not parse the script{where} — it was not audited.",
    }


# ══════════════════════════════════════════════════════════════════════════
# Agent-authored code: an ALLOWLIST, not a smoke detector
#
# Everything above reports what a script appears to do, for a human reading
# it. An AI agent's script has no human reading it before its backtest runs,
# and a deny-list cannot enumerate every way plain Python reaches the wallet
# key (the 2026-10-11/12 audits found several: `statistics.sys.modules`,
# a generator's `gi_frame.f_back.f_globals`, `"{0.gi_frame}".format(g)`).
# So code an agent writes must stay inside a small pure-computation subset:
#
#   • imports only from AGENT_MODULES, and only public names;
#   • a module object is only ever the base of an attribute chain, and every
#     link of that chain is resolved HERE against the real module, so
#     `statistics.sys` (a module outside the set) is refused by what it IS,
#     not by its spelling;
#   • no attribute starting with "_" (dunders and privates), none of the
#     frame / code / traceback / generator internals, no str.format /
#     format_map (they walk attributes named inside a string);
#   • no dunder names, no dunder string literals;
#   • none of the builtins that reach outside the computation (getattr,
#     setattr, delattr, vars, globals, locals, eval, exec, compile, open,
#     __import__, breakpoint, input, help, dir, memoryview, exit, quit…).
#
# User-authored scripts are NOT held to this: they are full Python by design
# (script_sandbox), and the user reads them. This applies where the author is
# an agent: save_script, backtest_script, and the engine before it compiles a
# row an agent wrote.
# ══════════════════════════════════════════════════════════════════════════

# Pure computation only. `typing` is deliberately absent: get_type_hints() and
# ForwardRef evaluate annotation STRINGS, which is an eval. `operator` is absent
# because attrgetter() is getattr by another name; `dataclasses` because it
# exec()s generated source; `string` because Formatter walks attributes.
AGENT_MODULES = frozenset({
    "math", "statistics", "datetime", "collections", "itertools", "functools",
    "re", "json", "random", "bisect", "heapq", "decimal", "fractions",
})

# The modules every script already has without an import line
# (script_sandbox.INJECTED_GLOBALS). They are module objects too, so the same
# chain rule applies to them: `statistics.sys` is refused.
_AGENT_INJECTED_MODULES = ("math", "statistics")

_AGENT_DENIED_BUILTINS = frozenset({
    "getattr", "setattr", "delattr", "vars", "globals", "locals", "eval",
    "exec", "compile", "open", "__import__", "breakpoint", "input", "help",
    "dir", "memoryview", "exit", "quit", "license", "credits", "copyright",
    "__build_class__", "__loader__", "__spec__", "__builtins__",
})

# Interpreter internals reachable WITHOUT a dunder: frames, code objects,
# tracebacks, generator/coroutine frames. `format`/`format_map` resolve
# "{0.attr}" fields by attribute walk inside a string the AST can't see.
_AGENT_DENIED_ATTR_PREFIXES = ("_", "f_", "gi_", "cr_", "ag_", "tb_", "co_")
_AGENT_DENIED_ATTRS = frozenset({"format", "format_map", "mro", "with_traceback"})

# Dunder METHODS a script's own class may define. They only change how the
# script's own objects behave; anything else (__del__, __getattr__,
# __init_subclass__, __set_name__, __class_getitem__, __reduce__…) runs at
# times the engine doesn't bound or serves no strategy purpose.
_AGENT_DUNDER_DEFS = frozenset({
    "__init__", "__repr__", "__str__", "__eq__", "__ne__", "__lt__", "__le__",
    "__gt__", "__ge__", "__hash__", "__len__", "__iter__", "__next__",
    "__contains__", "__getitem__", "__setitem__", "__bool__", "__call__",
})


def _agent_module_ok(mod: Any) -> bool:
    import types
    if not isinstance(mod, types.ModuleType):
        return True
    name = str(getattr(mod, "__name__", "") or "")
    parts = name.split(".")
    return parts[0] in AGENT_MODULES and not any(p.startswith("_") for p in parts)


def _agent_attr_denied(attr: str) -> bool:
    return attr.startswith(_AGENT_DENIED_ATTR_PREFIXES) or attr in _AGENT_DENIED_ATTRS


class _AgentCheck(ast.NodeVisitor):
    def __init__(self) -> None:
        self.errors: list[str] = []
        # local name -> the real module object it is bound to
        self.modules: dict[str, Any] = {}
        import importlib
        for m in _AGENT_INJECTED_MODULES:
            self.modules[m] = importlib.import_module(m)
        # Attribute nodes already resolved as part of a longer chain
        self._chained: set[int] = set()

    def err(self, node: ast.AST, msg: str) -> None:
        self.errors.append(f"line {int(getattr(node, 'lineno', 0) or 0)}: {msg}")

    # — imports —
    def _import_module(self, node: ast.AST, name: str) -> Any:
        import importlib
        parts = name.split(".")
        if parts[0] not in AGENT_MODULES or any(p.startswith("_") for p in parts):
            self.err(node, f"imports '{name}' — scripts written by an AI agent may "
                           f"only import {', '.join(sorted(AGENT_MODULES))}.")
            return None
        try:
            mod = importlib.import_module(name)
        except Exception:  # noqa: BLE001 — not importable is a refusal
            self.err(node, f"imports '{name}', which does not exist.")
            return None
        if not _agent_module_ok(mod):
            self.err(node, f"imports '{name}', which is outside the allowed set.")
            return None
        return mod

    def visit_Import(self, node: ast.Import) -> None:
        for a in node.names:
            mod = self._import_module(node, a.name)
            if mod is None:
                continue
            if a.asname:
                self.modules[a.asname] = mod
            else:
                import importlib
                root = a.name.split(".")[0]
                self.modules[root] = importlib.import_module(root)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level or not node.module:
            self.err(node, "uses a relative import.")
            return
        mod = self._import_module(node, node.module)
        if mod is None:
            return
        for a in node.names:
            if a.name == "*":
                self.err(node, f"uses 'from {node.module} import *' — name what you use.")
                continue
            if a.name.startswith("_"):
                self.err(node, f"imports the private name '{a.name}'.")
                continue
            if hasattr(mod, a.name):
                obj = getattr(mod, a.name)
            else:
                obj = self._import_module(node, f"{node.module}.{a.name}")
                if obj is None:
                    continue
            if not _agent_module_ok(obj):
                self.err(node, f"imports '{node.module}.{a.name}', a module outside "
                               "the allowed set.")
                continue
            import types
            if isinstance(obj, types.ModuleType):
                self.modules[a.asname or a.name] = obj
        self.generic_visit(node)

    # — attribute chains —
    def _chain(self, node: ast.Attribute) -> tuple[Optional[str], list[ast.Attribute]]:
        links: list[ast.Attribute] = []
        cur: Any = node
        while isinstance(cur, ast.Attribute):
            links.append(cur)
            cur = cur.value
        links.reverse()
        return (cur.id if isinstance(cur, ast.Name) else None), links

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr in ("format", "format_map"):
            self.err(node, f"calls '.{node.attr}' — it looks attributes up by names "
                           "written inside a string. Use an f-string instead.")
        elif _agent_attr_denied(node.attr):
            self.err(node, f"accesses '.{node.attr}' — interpreter internals and "
                           "private attributes are off limits to agent scripts.")
        if id(node) not in self._chained:
            root, links = self._chain(node)
            for ln in links:
                self._chained.add(id(ln))
            if root is not None and root in self.modules:
                obj = self.modules[root]
                path = root
                for ln in links:
                    path = f"{path}.{ln.attr}"
                    if _agent_attr_denied(ln.attr):
                        break       # reported on its own node
                    if not hasattr(obj, ln.attr):
                        self.err(ln, f"'{path}' does not exist.")
                        break
                    obj = getattr(obj, ln.attr)
                    if not _agent_module_ok(obj):
                        self.err(ln, f"'{path}' is a module outside the allowed set.")
                        break
                else:
                    import types
                    if isinstance(obj, types.ModuleType):
                        self.err(node, f"uses the module '{path}' as a value — only "
                                       "its functions and constants may be used.")
        self.generic_visit(node)

    # — names —
    def visit_Name(self, node: ast.Name) -> None:
        if node.id.startswith("__") or node.id in _AGENT_DENIED_BUILTINS:
            self.err(node, f"uses '{node.id}', which agent scripts may not use.")
        elif node.id in self.modules and isinstance(node.ctx, ast.Load):
            parent = getattr(node, "_kp_parent", None)
            if not (isinstance(parent, ast.Attribute) and parent.value is node):
                self.err(node, f"uses the module '{node.id}' as a value — only its "
                               "functions and constants may be used.")
        self.generic_visit(node)

    def _check_def_name(self, node: ast.AST, name: str, in_class: bool) -> None:
        if name.startswith("__") and not (in_class and name in _AGENT_DUNDER_DEFS):
            self.err(node, f"defines '{name}', which agent scripts may not define.")
        elif name in _AGENT_DENIED_BUILTINS:
            self.err(node, f"defines '{name}', the name of a refused builtin.")

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._check_def_name(node, node.name,
                             isinstance(getattr(node, "_kp_parent", None), ast.ClassDef))
        for a in (node.args.posonlyargs + node.args.args + node.args.kwonlyargs
                  + [x for x in (node.args.vararg, node.args.kwarg) if x]):
            if a.arg.startswith("__"):
                self.err(a, f"uses the parameter name '{a.arg}'.")
        self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._check_def_name(node, node.name, False)
        self.generic_visit(node)

    def visit_MatchClass(self, node: Any) -> None:
        # `case C(attr=x)` reads `attr` off the subject by NAME.
        for a in getattr(node, "kwd_attrs", []) or []:
            if _agent_attr_denied(a) or a.startswith("_"):
                self.err(node, f"matches on the attribute '{a}'.")
        self.generic_visit(node)

    def visit_keyword(self, node: ast.keyword) -> None:
        if node.arg and node.arg.startswith("__"):
            self.err(node, f"passes the keyword '{node.arg}'.")
        self.generic_visit(node)

    def visit_alias(self, node: ast.alias) -> None:
        if node.asname and (node.asname.startswith("__")
                            or node.asname in _AGENT_DENIED_BUILTINS):
            self.err(node, f"imports under the name '{node.asname}'.")

    # — literals —
    def visit_Constant(self, node: ast.Constant) -> None:
        v = node.value
        if isinstance(v, str) and "__" in v:
            self.err(node, "contains a string with '__' in it — dunder names in "
                           "strings are how attribute lookups get hidden.")
        elif isinstance(v, bytes) and b"__" in v:
            self.err(node, "contains bytes with '__' in them.")


def validate_agent(code: str) -> list[str]:
    """Errors that make `code` unacceptable FROM AN AI AGENT ([] = allowed).

    The allowlist described above. Parse failures are errors too: code that
    could not be read was not checked."""
    pin_recursion_limit()
    try:
        tree = ast.parse(code or "")
    except _UNREADABLE as e:
        line = getattr(e, "lineno", None) if isinstance(e, SyntaxError) else None
        return [f"the script could not be read{f' (line {line})' if line else ''}, "
                "so it was not checked — refused."]
    try:
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                child._kp_parent = parent  # type: ignore[attr-defined]
        chk = _AgentCheck()
        chk.visit(tree)
    except _UNWALKABLE:
        return ["the script is nested too deeply to check — refused."]
    seen: set[str] = set()
    out: list[str] = []
    for e in chk.errors:
        if e not in seen:
            seen.add(e)
            out.append(e)
    return out[:40]


def audit(code: str) -> dict:
    """Scan `code` and return a verdict.

    {"ok": bool,                  # parsed and nothing critical
     "parsed": bool,
     "critical": int, "warning": int, "info": int,
     "categories": ["network", "wallet", ...],
     "findings": [{severity, category, line, message}, ...],
     "summary": "one line for the UI"}
    """
    pin_recursion_limit()
    try:
        tree = ast.parse(code or "")
    except _UNREADABLE as e:
        return _unreadable(e)

    scan = _Scan()
    try:
        scan.visit(tree)
    except _UNWALKABLE as e:
        # A tree that PARSED can still be too deep to walk: "1+1+1+..." forty
        # thousand characters long is one left-nested BinOp per term, well
        # inside the source-size cap, and generic_visit recurses once per
        # node. Same blast radius as a parse failure — a 500 out of the save
        # RPC — and the same honest answer: we could not read it.
        return _unreadable(e)
    findings = scan.findings

    # Combination rule. Either half alone has innocent readings; together they
    # are the shape of a key-exfiltration script, and that deserves to be said
    # in one sentence rather than left for the user to assemble from a list.
    cats = scan.categories
    if "wallet" in cats and "network" in cats:
        findings.insert(0, {
            "severity": "critical", "category": "exfiltration", "line": 0,
            "message": "This script BOTH touches credential-shaped names/app "
                       "internals AND opens network connections. That is the "
                       "shape of a key-stealing script. Do not run it unless "
                       "you wrote it and know exactly why it does both.",
        })
        cats.add("exfiltration")

    # Dedupe identical (line, message) pairs — a name repeated in a loop
    # shouldn't produce twenty rows of the same sentence.
    seen: set[tuple[int, str]] = set()
    deduped: list[dict] = []
    for f in findings:
        key = (f["line"], f["message"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(f)

    deduped.sort(key=lambda f: (_RANK.get(f["severity"], 3), f["line"]))
    truncated = max(0, len(deduped) - MAX_FINDINGS)
    deduped = deduped[:MAX_FINDINGS]

    n_crit = sum(1 for f in deduped if f["severity"] == "critical")
    n_warn = sum(1 for f in deduped if f["severity"] == "warning")
    n_info = sum(1 for f in deduped if f["severity"] == "info")

    if truncated:
        deduped.append({
            "severity": "info", "category": "audit", "line": 0,
            "message": f"…and {truncated} more finding(s) not shown.",
        })

    if n_crit:
        summary = (f"{n_crit} critical finding(s) — this script does things a "
                   "trading strategy does not need to do. Read it before "
                   "enabling it.")
    elif n_warn:
        summary = (f"{n_warn} thing(s) worth a look — nothing critical, but "
                   "not purely a strategy either.")
    else:
        summary = ("Nothing alarming recognized: no network, filesystem, "
                   "process, credential or dynamic-execution access. This is "
                   "not proof it is safe — only that nothing known-dangerous "
                   "was spotted.")

    return {
        "ok": n_crit == 0,
        "parsed": True,
        "critical": n_crit, "warning": n_warn, "info": n_info,
        "categories": sorted(cats),
        "findings": deduped,
        "summary": summary,
    }
