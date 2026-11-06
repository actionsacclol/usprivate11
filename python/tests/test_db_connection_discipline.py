"""Static discipline checks on how the codebase uses `db.get_db()`.

`db.get_db()` reuses one connection per WORKER thread (see the long comment
above it in db.py). Two properties have to hold for that to stay safe, and
neither is visible at any single call site — so they are checked here, over the
AST of every backend module, and a regression fails the suite instead of
corrupting a trade ledger in production.

1. NO TRANSACTION HELD OPEN ACROSS AN `await`.
   `with db.get_db() as conn:` opens a transaction and commits it on the way
   out. If the block awaits, another coroutine runs inside that window; on a
   shared connection its writes join the SAME transaction and its block exit
   commits the suspended coroutine's half-written rows. Two such blocks exist
   today and are listed in ALLOWED_AWAIT_BLOCKS below — which is precisely why
   reuse is currently limited to threads with no running event loop. This test
   stops the list from growing, and empties it when those two are fixed.

2. NO NESTED `with get_db()` ON ONE THREAD.
   A helper that opens its own connection while its caller holds one relies on
   the inner block committing/rolling back independently. `get_db()` honours
   that (an already-checked-out cache hands out a private connection), but the
   nesting is worth knowing about, so it is pinned at zero.
"""
from __future__ import annotations

import ast
from pathlib import Path

PY = Path(__file__).resolve().parent.parent

# Keyed by (module, enclosing function) rather than line number, because these
# files are edited often and a drifting line number is a false alarm, not a
# regression. Both blocks hold a write transaction open across a network call;
# fixing them (fetch outside the block, or split it in two around the await) is
# what lets `db.get_db()` reuse a connection on the loop thread too.
# Empty as of 2026-09-01: process_whale_trades resolves categories between
# two short transactions, and poll_open_orders no longer awaits inside its
# write pass. Keep it empty — an entry here is a stall, not a style nit.
ALLOWED_AWAIT_BLOCKS: set[tuple[str, str]] = set()

# Context managers that yield a sqlite connection from db.get_db().
_DB_CTX = {"get_db", "_conn"}


def _is_db_ctx(item: ast.withitem) -> bool:
    call = item.context_expr
    if not isinstance(call, ast.Call):
        return False
    fn = call.func
    if isinstance(fn, ast.Name):
        return fn.id in _DB_CTX
    if isinstance(fn, ast.Attribute):
        return fn.attr in _DB_CTX
    return False


class _AwaitsHere(ast.NodeVisitor):
    """Suspension points in THIS execution context.

    Nested `def` / `async def` / `lambda` bodies are skipped: they are a
    different frame, and if one is actually awaited inside the block that shows
    up as an `Await` node at the call site anyway.
    """

    def __init__(self) -> None:
        self.hits: list[tuple[str, int]] = []

    def visit_FunctionDef(self, node): return
    def visit_AsyncFunctionDef(self, node): return
    def visit_Lambda(self, node): return

    def visit_Await(self, node):
        self.hits.append(("await", node.lineno))
        self.generic_visit(node)

    def visit_AsyncFor(self, node):
        self.hits.append(("async for", node.lineno))
        self.generic_visit(node)

    def visit_AsyncWith(self, node):
        self.hits.append(("async with", node.lineno))
        self.generic_visit(node)


class _Scan(ast.NodeVisitor):
    def __init__(self, module: str) -> None:
        self.module = module
        # (module, enclosing function, with-line, kind, await-line)
        self.awaits: list[tuple[str, str, int, str, int]] = []
        self.nested: list[tuple[str, int, int]] = []
        self._open: list[int] = []
        self._fn: list[str] = []

    def _func(self, node):
        self._fn.append(node.name)
        self.generic_visit(node)
        self._fn.pop()

    visit_FunctionDef = _func
    visit_AsyncFunctionDef = _func

    def _with(self, node):
        if not any(_is_db_ctx(i) for i in node.items):
            self.generic_visit(node)
            return
        if self._open:
            self.nested.append((self.module, self._open[-1], node.lineno))
        finder = _AwaitsHere()
        for stmt in node.body:
            finder.visit(stmt)
        # The OUTERMOST enclosing def is the stable name; a closure defined
        # inside it can be renamed without changing what the block does.
        where = self._fn[0] if self._fn else "<module>"
        for kind, line in finder.hits:
            self.awaits.append((self.module, where, node.lineno, kind, line))
        self._open.append(node.lineno)
        for stmt in node.body:
            self.visit(stmt)
        self._open.pop()

    visit_With = _with
    visit_AsyncWith = _with


def _modules() -> list[Path]:
    return sorted(p for p in PY.glob("*.py") if not p.name.startswith("_"))


def _scan_all() -> tuple[list, list]:
    awaits, nested = [], []
    for path in _modules():
        scan = _Scan(path.name)
        scan.visit(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        awaits += scan.awaits
        nested += scan.nested
    return awaits, nested


def test_scan_actually_reaches_the_backend():
    """A silently-empty scan would make every assertion below vacuous."""
    names = {p.name for p in _modules()}
    assert {"db.py", "trader.py", "scanner.py", "service.py"} <= names
    assert len(names) > 20


def test_no_new_transaction_held_across_an_await():
    unexpected = [
        hit for hit in _scan_all()[0]
        if (hit[0], hit[1]) not in ALLOWED_AWAIT_BLOCKS
    ]
    assert not unexpected, (
        "a db transaction is held open across a suspension point:\n"
        + "\n".join(
            f"  {mod}:{w} in {fn}() -> {kind} at line {a}"
            for mod, fn, w, kind, a in unexpected
        )
        + "\n\nAnother coroutine resuming inside that window shares the "
          "transaction on a reused connection, and its block exit commits the "
          "suspended one's half-written rows. Fetch outside the block, or "
          "split it into two blocks around the await."
    )


def test_allow_list_has_no_stale_entries():
    """When a listed block is fixed, its entry must go — otherwise the list
    keeps granting an exemption to code that no longer needs one."""
    live = {(mod, fn) for mod, fn, _w, _k, _a in _scan_all()[0]}
    stale = sorted(ALLOWED_AWAIT_BLOCKS - live)
    assert not stale, (
        f"ALLOWED_AWAIT_BLOCKS entries no longer hold a transaction across an "
        f"await (fixed, or renamed): {stale}. Delete them — and if the set is "
        "then empty, db.get_db() can reuse its connection on the loop thread "
        "too (drop the _reuse_ok() guard in db.py)."
    )


def test_no_nested_get_db_blocks():
    nested = _scan_all()[1]
    assert not nested, (
        "a `with get_db()` block opens another one inside itself:\n"
        + "\n".join(f"  {m}: outer line {o} -> inner line {i}" for m, o, i in nested)
    )
