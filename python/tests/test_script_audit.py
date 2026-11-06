"""The audit's refusal path — what happens to source it cannot read.

`audit()` is called on the save RPC before a script is written, so anything
it raises is a 500 the user cannot read and cannot act on. `ast.parse` raises
more than `SyntaxError`, and a tree that parses can still be too deep to
walk; both used to escape. The rule this file pins is the module's own: a
verdict we could not reach is stated as a refusal, never as silence.

Every source here is comfortably inside `script_sandbox.MAX_CODE_BYTES`
(128 KB) — none of this needs a large file.
"""
from __future__ import annotations

import ast
import sys

import pytest

import script_audit as sa
import script_sandbox


def _under_the_cap(src: str) -> str:
    assert len(src.encode()) < script_sandbox.MAX_CODE_BYTES, "not a fair test"
    return src


def _assert_refused(r: dict) -> None:
    assert r["ok"] is False, "unreadable source was not refused"
    assert r["parsed"] is False
    assert r["critical"] == 1, "a refusal with no critical finding reads as clean"
    assert "not audited" in r["summary"]
    assert r["findings"] and r["findings"][0]["severity"] == "critical"
    assert "refused" in r["findings"][0]["message"]


def test_a_deeply_nested_expression_is_refused_not_raised():
    """Python's default recursion limit is 1,000, so 1,500 nested parens is a
    3 KB script that used to take the parser down and the save RPC with it."""
    _assert_refused(sa.audit(_under_the_cap("(" * 1500 + "1" + ")" * 1500)))


def test_a_nul_byte_is_refused_not_raised():
    """On 3.10/3.11 this is a ValueError out of ast.parse, not a SyntaxError;
    later versions report it as a SyntaxError. Both are refusals here."""
    _assert_refused(sa.audit("x = 1\x00\ndef decide(ctx):\n    return None\n"))


def test_a_tree_too_deep_to_WALK_is_refused_too():
    """This one PARSES. "1+1+1+..." is one left-nested BinOp per term, so the
    40 KB source below builds a 20,000-deep tree and it is generic_visit that
    runs out of stack. Same blast radius, same answer."""
    _assert_refused(sa.audit(_under_the_cap("x = 1" + "+1" * 20000 + "\n")))


def test_a_library_raising_the_recursion_limit_cannot_switch_the_refusal_off():
    """py_ecc (eth-account >= 0.14, so every CI-built installer) sets the
    limit to 100,000 at import. Under that limit the tree above walks fine,
    and on 3.11 a 60,000-term line overflows the C stack inside ast.parse and
    kills the backend outright. The audit puts the limit back before parsing."""
    old = sys.getrecursionlimit()
    sys.setrecursionlimit(100_000)
    try:
        _assert_refused(sa.audit(_under_the_cap("x = 1" + "+1" * 20000 + "\n")))
        assert sys.getrecursionlimit() == 1000
    finally:
        sys.setrecursionlimit(old)


@pytest.mark.parametrize("exc", [
    RecursionError("maximum recursion depth exceeded"),
    ValueError("source code string cannot contain null bytes"),
    MemoryError(),
])
def test_every_parser_failure_mode_is_a_refusal(monkeypatch, exc):
    """Pinned by injection as well as by input, because WHICH exception a
    given source raises moves between Python versions — the handling must not
    depend on the interpreter agreeing with us today."""
    def boom(*a, **kw):
        raise exc

    monkeypatch.setattr(ast, "parse", boom)
    _assert_refused(sa.audit("def decide(ctx):\n    return None\n"))


def test_a_syntax_error_still_names_the_line():
    r = sa.audit("def decide(ctx)\n    return None\n")
    _assert_refused(r)
    assert "line 1" in r["summary"]
    assert r["findings"][0]["line"] == 1


def test_readable_source_is_still_audited_normally():
    # The refusal path must not have eaten the ordinary one.
    clean = sa.audit("def decide(ctx):\n    return None\n")
    assert clean["ok"] is True and clean["parsed"] is True
    assert "not proof it is safe" in clean["summary"]

    dirty = sa.audit(
        "import requests\n"
        "def decide(ctx):\n    requests.post('http://x.example')\n    return None\n")
    assert dirty["ok"] is False and dirty["parsed"] is True
    assert "network" in dirty["categories"]
