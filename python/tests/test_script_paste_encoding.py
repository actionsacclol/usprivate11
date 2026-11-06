"""Pasted script text with broken encoding must not kill the RPC.

Reported 2026-07-25: pasting a script produced
`error invoking remote method "scripts:validate"` with no useful detail.

Cause: text pasted from a browser/PDF/chat can carry LONE SURROGATES
(\\udc80-\\udcff) — what a smart quote (U+201D, `E2 80 9D` in UTF-8) becomes
when its encoding is mis-detected on the way in. Python and SQLite both accept
them, but serializing the response back to the renderer raises
`UnicodeEncodeError: surrogates not allowed`, so the whole RPC dies and the
user sees a generic IPC failure pointing nowhere near the real cause.
"""
from __future__ import annotations

import asyncio
import json

import pytest

import service


def _mangled(src: str) -> str:
    """What the renderer sends after a paste loses its encoding."""
    return src.encode("utf-8").decode("cp1252", "surrogateescape")


GOOD = 'def decide(ctx):\n    return None  # smart quote ” here\n'
BAD = _mangled(GOOD)


def test_the_fixture_really_contains_a_lone_surrogate():
    assert any(0xDC80 <= ord(c) <= 0xDCFF for c in BAD)
    with pytest.raises(UnicodeEncodeError):
        BAD.encode("utf-8")


def test_sanitizer_makes_it_serializable():
    clean = service._sanitize_script_code(BAD)
    clean.encode("utf-8")                      # must not raise
    json.dumps({"code": clean}).encode("utf-8")
    assert not any(0xDC80 <= ord(c) <= 0xDCFF for c in clean)


def test_sanitizer_leaves_ordinary_code_untouched():
    src = "def decide(ctx):\n    return None\n"
    assert service._sanitize_script_code(src) == src


def test_sanitizer_preserves_legitimate_unicode():
    """An em-dash in a comment is valid UTF-8 and must survive."""
    src = "# strategy — notes\ndef decide(ctx):\n    return None\n"
    assert service._sanitize_script_code(src) == src


@pytest.mark.parametrize("value", [None, "", 0])
def test_sanitizer_handles_empty_input(value):
    assert service._sanitize_script_code(value) == ""


def test_validate_rpc_survives_a_mangled_paste():
    """The actual reported failure: scripts:validate must ANSWER, not die.

    Note the verdict here is `ok=True` — the surrogate sat inside a comment,
    so scrubbing it leaves valid Python. That is the point: the user gets a
    real answer instead of `error invoking remote method`.
    """
    res = asyncio.run(service._h_script_validate({"code": BAD}))
    json.dumps(res).encode("utf-8")            # the step that used to blow up
    assert isinstance(res["ok"], bool)
    assert isinstance(res["errors"], list)


def test_mangled_paste_in_actual_CODE_reports_a_normal_error():
    """When the surrogate lands in code rather than a comment, the user should
    get an ordinary syntax error with a line number — not an IPC failure."""
    src = 'def decide(ctx):\n    return “hello”\n'
    res = asyncio.run(service._h_script_validate({"code": _mangled(src)}))
    json.dumps(res).encode("utf-8")
    assert res["ok"] is False
    assert res["errors"] and "line" in res["errors"][0].lower()


def test_save_rpc_survives_a_mangled_paste(tmp_path, monkeypatch):
    import db

    dbfile = tmp_path / "paste.db"
    monkeypatch.setattr(db, "db_path", lambda: dbfile)
    db.init_db()

    res = asyncio.run(service._h_script_save({"code": BAD}))
    json.dumps(res).encode("utf-8")
    assert res["script"]["id"]

    # And listing it back must not blow up either.
    listing = asyncio.run(service._h_scripts_list({}))
    json.dumps(listing).encode("utf-8")


def test_sqlite_itself_rejects_surrogates(tmp_path, monkeypatch):
    """Why sanitizing at the RPC boundary is the COMPLETE fix.

    SQLite refuses to store a lone surrogate at all, so one can never reach a
    stored row — the crash always happened on the way in (validate, or the
    save's own INSERT). There is no legacy-bad-row case to migrate.
    """
    import db

    dbfile = tmp_path / "legacy.db"
    monkeypatch.setattr(db, "db_path", lambda: dbfile)
    db.init_db()
    with pytest.raises(UnicodeEncodeError):
        with db.get_db() as conn:
            db.upsert_user_script(conn, {
                "id": "legacy", "name": "legacy", "description": "",
                "code": BAD, "notes": "",
            })
