"""Agent-authored strategy scripts (2026-10-12 release audit C2 / H1 / H2 /
H10): what an AI agent with the opt-in scripts permission can and cannot do.

  • its code must pass an ALLOWLIST (script_audit.validate_agent), while the
    user's own scripts stay full Python;
  • code it writes or changes lands disabled, in shadow, needs-review, and
    nothing an agent can call turns it on or arms it;
  • a rewrite of an ARMED script (dry_run=0, enabled or not) goes back to
    shadow;
  • its backtest child never sees the real profile or the real database.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone

import pytest

import config
import db
import mcp_server
import mcp_tools
import script_audit
import script_backtest_child
import script_docs
import script_engine
import script_sandbox
import service


def _run(coro):
    return asyncio.run(coro)


# ════════════════════════════════════════════════════════════════════════
# The allowlist
# ════════════════════════════════════════════════════════════════════════

_HOOK = "\ndef decide(ctx):\n    return None\n"

REJECTED = {
    "reads os.environ": "import os\ndef decide(ctx):\n    return os.environ.get('POLY_KEY')\n",
    "imports os from a sub-path": "from os import path\n" + _HOOK,
    "opens a file": "def decide(ctx):\n    return open('C:/Users/x/AppData/Roaming/Krypt PolyBot US/settings.json').read()\n",
    "imports subprocess": "import subprocess\ndef decide(ctx):\n    subprocess.run(['calc'])\n",
    "imports sys": "import sys\ndef decide(ctx):\n    return sys.modules\n",
    "walks __class__": "def decide(ctx):\n    return ().__class__.__base__.__subclasses__()\n",
    "dunder in a string": "def decide(ctx):\n    return '__globals__'\n",
    "uses getattr": "def decide(ctx):\n    return getattr(ctx, 'x')\n",
    "uses getattr with a constant": "def decide(ctx):\n    return getattr(log, 'x', None)\n",
    "uses vars": "def decide(ctx):\n    return vars(ctx)\n",
    "uses eval": "def decide(ctx):\n    return eval('1+1')\n",
    "uses __import__": "def decide(ctx):\n    return __import__('os')\n",
    "uses __builtins__": "def decide(ctx):\n    return __builtins__\n",
    "reaches sys through an injected module": "def decide(ctx):\n    return statistics.sys.modules\n",
    "reaches sys through an allowed import": "import json\ndef decide(ctx):\n    return json.codecs\n",
    "binds a module to a name": "import json\ndef decide(ctx):\n    m = json\n    return m\n",
    "passes a module as a value": "import json\ndef decide(ctx):\n    return str(json)\n",
    "walks a generator's frame": ("def g():\n    yield 1\n"
                                  "def decide(ctx):\n    return g().gi_frame.f_back.f_globals\n"),
    "uses str.format": "def decide(ctx):\n    return '{0.gi_frame}'.format(ctx)\n",
    "a private attribute": "import random\ndef decide(ctx):\n    return random._os\n",
    "matches on an internal attribute": ("def decide(ctx):\n    match ctx:\n"
                                         "        case object(gi_frame=f):\n            return f\n"),
    "typing (get_type_hints evals strings)": "from typing import get_type_hints\n" + _HOOK,
    "star import": "from math import *\n" + _HOOK,
    "relative import": "from . import service\n" + _HOOK,
    "imports the app": "import polymarket_auth\n" + _HOOK,
    "defines __getattr__": "class A:\n    def __getattr__(self, n):\n        return 1\n" + _HOOK,
    "unparseable": "def decide(ctx)\n    return None\n",
}


@pytest.mark.parametrize("why", sorted(REJECTED))
def test_agent_code_outside_the_allowlist_is_rejected(why):
    assert script_audit.validate_agent(REJECTED[why]), why


def test_a_plain_strategy_passes_the_allowlist():
    code = (
        "import json, collections\n"
        "import collections.abc as cabc\n"
        "from datetime import datetime, timezone\n"
        "from functools import lru_cache\n"
        "class Edge:\n"
        "    def __init__(self, x):\n"
        "        self.x = x\n"
        "    def __repr__(self):\n"
        "        return f'Edge({self.x})'\n"
        "def decide(ctx):\n"
        "    seen = collections.defaultdict(int)\n"
        "    if not isinstance(ctx, cabc.Mapping):\n"
        "        return None\n"
        "    m = statistics.mean([1, 2, 3]) + math.sqrt(4)\n"
        "    state['t'] = datetime.now(timezone.utc).isoformat()\n"
        "    log(json.dumps(dict(seen)), Edge(m))\n"
        "    return {'side': 'up', 'price': 'ask', 'size': 1}\n"
    )
    assert script_audit.validate_agent(code) == []


def test_the_shipped_examples_and_presets_pass_the_allowlist():
    # So an agent that starts from the docs is not refused for copying them.
    codes = [getattr(script_docs, n) for n in dir(script_docs) if n.startswith("EXAMPLE_")]
    codes += [p["code"] for p in script_docs.PRESETS if isinstance(p, dict) and p.get("code")]
    assert codes
    for c in codes:
        assert script_sandbox.validate(c) == []
        assert script_audit.validate_agent(c) == [], c[:80]


def test_user_scripts_stay_full_python():
    # The sandbox was removed for USERS on purpose (v3.0.0): their imports,
    # files and os calls still validate and still save + enable.
    code = "import os\nimport subprocess\ndef decide(ctx):\n    return None\n"
    assert script_sandbox.validate(code) == []
    assert script_audit.validate_agent(code)        # an agent could not


# ════════════════════════════════════════════════════════════════════════
# Service: author, needs-review, shadow, arming
# ════════════════════════════════════════════════════════════════════════

GOOD = "def decide(ctx):\n    return None\n"
NEW = "def decide(ctx):\n    return {'side': 'up', 'price': 'ask', 'size': 1}\n"


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "db_path", lambda: tmp_path / "s.db")
    db.init_db()
    mcp_server._sessions.clear()
    forgot: list = []
    monkeypatch.setattr(script_engine, "forget", lambda sid: forgot.append(sid))
    monkeypatch.setattr(mcp_tools, "_push", lambda t: None)
    return forgot


def _row(sid):
    with db.get_db() as conn:
        return db.get_user_script(conn, sid)


def _user_script(sid="s1", *, enabled=1, dry_run=0, code=GOOD):
    with db.get_db() as conn:
        db.upsert_user_script(conn, {"id": sid, "name": "Mine", "code": code, "notes": "n"})
        db.update_user_script(conn, sid, enabled=enabled, dry_run=dry_run)


@pytest.mark.parametrize("enabled", [1, 0])
def test_an_agent_rewrite_of_an_armed_script_forces_shadow_off_and_review(fresh, enabled):
    # enabled=0 / dry_run=0 is a routine state: the daily-loss breaker or a
    # hook error switched off a script the user armed (H1).
    _user_script(enabled=enabled, dry_run=0)
    out = _run(service._h_script_save({"id": "s1", "code": NEW}, agent="claude"))
    r = _row("s1")
    assert (r["enabled"], r["dry_run"], r["needs_review"]) == (0, 1, 1)
    assert r["author"] == "agent:claude"
    assert out["disarmed"] is True
    assert fresh == ["s1"]                       # the engine dropped the old module
    assert out["script"]["needsReview"] is True and out["script"]["author"] == "agent:claude"


def test_an_agent_cannot_enable_or_arm_unreviewed_code(fresh):
    _run(service._h_script_save({"id": "a1", "code": GOOD}, agent="claude"))
    with pytest.raises(ValueError, match="not\\s+reviewed|reviewed it yet"):
        _run(service._h_script_set_enabled({"id": "a1", "enabled": True}, agent="claude"))
    with pytest.raises(ValueError, match="Only the user"):
        _run(service._h_script_set_dry_run({"id": "a1", "dryRun": False}, agent="claude"))
    # nor can the user arm it before reviewing it
    with pytest.raises(ValueError, match="not been\\s+reviewed"):
        _run(service._h_script_set_dry_run({"id": "a1", "dryRun": False}))
    assert _row("a1")["enabled"] == 0 and _row("a1")["dry_run"] == 1


def test_the_user_enabling_it_is_the_review(fresh):
    _run(service._h_script_save({"id": "a1", "code": GOOD}, agent="claude"))
    _run(service._h_script_set_enabled({"id": "a1", "enabled": True}))
    r = _row("a1")
    assert r["enabled"] == 1 and r["needs_review"] == 0 and r["dry_run"] == 1
    # reviewed: now the agent may switch it off and on in shadow
    _run(service._h_script_set_enabled({"id": "a1", "enabled": False}, agent="claude"))
    _run(service._h_script_set_enabled({"id": "a1", "enabled": True}, agent="claude"))
    assert _row("a1")["enabled"] == 1


def test_the_service_refuses_agent_code_outside_the_allowlist(fresh):
    _user_script(code=GOOD)
    with pytest.raises(ValueError, match="plain"):
        _run(service._h_script_save({"id": "s1", "code": REJECTED["reads os.environ"]},
                                    agent="claude"))
    assert _row("s1")["code"] == GOOD and _row("s1")["dry_run"] == 0


def test_a_user_save_keeps_working_and_takes_ownership_when_the_code_changes(fresh):
    _run(service._h_script_save({"id": "a1", "code": GOOD}, agent="claude"))
    # same code (e.g. the editor's autosave before a backtest): still the agent's
    _run(service._h_script_save({"id": "a1", "code": GOOD}))
    assert _row("a1")["needs_review"] == 1
    # the user edits it: theirs now, full Python allowed
    code = "import os\n" + GOOD
    out = _run(service._h_script_save({"id": "a1", "code": code}))
    r = _row("a1")
    assert not out["errors"] and r["needs_review"] == 0 and r["author"] == "user"
    _run(service._h_script_set_enabled({"id": "a1", "enabled": True}))
    assert _row("a1")["enabled"] == 1


def test_agent_scripts_saved_before_this_release_go_back_to_review(fresh):
    note = "Written by an AI agent over MCP."
    with db.get_db() as conn:
        for sid, dry in (("old-shadow", 1), ("old-armed", 0)):
            db.upsert_user_script(conn, {"id": sid, "name": sid, "code": GOOD, "notes": note})
            db.update_user_script(conn, sid, enabled=1, dry_run=dry, author=None)
        db.upsert_user_script(conn, {"id": "mine", "name": "mine", "code": GOOD, "notes": "x"})
        db.update_user_script(conn, "mine", enabled=1)
    db.init_db()                                  # the upgrade
    a, b, c = _row("old-shadow"), _row("old-armed"), _row("mine")
    assert (a["author"], a["needs_review"], a["enabled"]) == ("agent:earlier", 1, 0)
    # the user armed this one: it stays put, held to the agent rules
    assert (b["author"], b["needs_review"], b["enabled"], b["dry_run"]) == ("agent:earlier", 0, 1, 0)
    assert (c["author"], c["needs_review"], c["enabled"]) == (None, 0, 1)
    db.init_db()                                  # and only once
    assert _row("old-shadow")["author"] == "agent:earlier"


def test_the_engine_never_runs_unreviewed_or_rule_breaking_agent_code(fresh, monkeypatch):
    _run(service._h_script_save({"id": "a1", "code": GOOD}, agent="claude"))
    with db.get_db() as conn:                  # a path that forgot to switch it off
        db.update_user_script(conn, "a1", enabled=1)
    compiled = []
    real = script_engine._get_compiled
    monkeypatch.setattr(script_engine, "_get_compiled", lambda s, c: compiled.append(s["id"]))
    _run(script_engine.run_tick(config.merge_with_defaults({}), authed=True))
    assert compiled == []
    monkeypatch.setattr(script_engine, "_get_compiled", real)
    # an agent row whose code breaks the rules (written before a rule existed)
    with db.get_db() as conn:
        db.update_user_script(conn, "a1", needs_review=0, enabled=1,
                              code="import os\n" + GOOD)
    assert script_engine._get_compiled(_row("a1"), {}) is None
    assert _row("a1")["enabled"] == 0


# ════════════════════════════════════════════════════════════════════════
# MCP tools, end to end against the real service
# ════════════════════════════════════════════════════════════════════════

def _agent_cfg(monkeypatch, **kw):
    cfg = {"mcp_enabled": True, "mcp_scripts_enabled": True,
           "mcp_allow_live_switches": False}
    cfg.update(kw)
    monkeypatch.setattr(service.STATE, "cfg", config.merge_with_defaults(cfg))


def _call(name, args):
    s = {"client": "test-agent", "agent": "default", "reads": 0, "writes": 0, "refused": 0}
    return _run(mcp_tools.call(name, args, s))


def _text(res):
    return res["content"][0]["text"]


def test_mcp_rewrite_of_a_disabled_but_armed_script(fresh, monkeypatch):
    _user_script(enabled=0, dry_run=0)
    _agent_cfg(monkeypatch)
    res = _call("save_script", {"id": "s1", "code": NEW})
    assert res["isError"] and "whole-account" in _text(res).lower()
    assert _row("s1")["code"] == GOOD

    _agent_cfg(monkeypatch, mcp_allow_live_switches=True)
    res = _call("save_script", {"id": "s1", "code": NEW})
    assert not res["isError"], _text(res)
    r = _row("s1")
    assert (r["code"], r["enabled"], r["dry_run"], r["needs_review"]) == (NEW, 0, 1, 1)

    res = _call("set_script_state", {"id": "s1", "enabled": True})
    assert res["isError"] and "review" in _text(res).lower()
    assert _row("s1")["enabled"] == 0


def test_an_agent_can_never_arm_live(fresh, monkeypatch):
    _user_script(enabled=1, dry_run=1)
    _agent_cfg(monkeypatch, mcp_allow_live_switches=True)
    res = _call("set_script_state", {"id": "s1", "live": True})
    assert res["isError"] and "only the user" in _text(res).lower()
    assert _row("s1")["dry_run"] == 1
    # back to shadow is always allowed
    _user_script("s2", enabled=1, dry_run=0)
    res = _call("set_script_state", {"id": "s2", "live": False})
    assert not res["isError"], _text(res)
    assert _row("s2")["dry_run"] == 1


@pytest.mark.parametrize("why", ["reads os.environ", "opens a file", "imports subprocess",
                                 "walks __class__", "uses getattr"])
def test_mcp_save_and_backtest_refuse_rule_breaking_code(fresh, monkeypatch, why):
    _agent_cfg(monkeypatch)
    for tool in ("save_script", "backtest_script"):
        res = _call(tool, {"code": REJECTED[why]})
        assert res["isError"], (tool, why)
    with db.get_db() as conn:
        assert db.list_user_scripts(conn) == []


def test_mcp_save_lands_off_in_shadow_needing_review(fresh, monkeypatch):
    _agent_cfg(monkeypatch)
    res = _call("save_script", {"code": GOOD, "name": "Agent's"})
    assert not res["isError"], _text(res)
    with db.get_db() as conn:
        (r,) = db.list_user_scripts(conn)
    assert (r["enabled"], r["dry_run"], r["needs_review"]) == (0, 1, 1)
    assert r["author"] == "agent:default"


# ════════════════════════════════════════════════════════════════════════
# The backtest child: no real paths, no real DB, no child processes
# ════════════════════════════════════════════════════════════════════════

def _seed():
    db.init_db()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    with db.get_db() as conn:
        for c in range(2):
            close = now - timedelta(hours=2) + timedelta(minutes=15 * c)
            tk = f"BTC-{c}"
            conn.execute(
                "INSERT INTO crypto15m_signals (ticker, asset, resolved, up_won, network, close_time, interval) "
                "VALUES (?, 'BTC', 1, 1, 'mainnet', ?, '15m')",
                (tk, close.strftime("%Y-%m-%dT%H:%M:%SZ")))
            for m in range(3, 0, -1):
                conn.execute(
                    "INSERT INTO crypto15m_ticks (ticker, asset, observed_at, mins_left, yes_bid, yes_ask, "
                    "up_prob, up_ask, no_ask, network) VALUES (?, 'BTC', ?, ?, 0.49, 0.5, 0.5, 0.5, 0.5, 'mainnet')",
                    (tk, (close - timedelta(minutes=m)).strftime("%Y-%m-%d %H:%M:%S"), float(m)))
        conn.execute("INSERT OR REPLACE INTO app_kv (k, v) VALUES ('remote:pair_code', 'SECRET-PAIR')")
        db.upsert_user_script(conn, {"id": "u1", "name": "Mine", "code": GOOD})


PROBE = '''
import os, db
def on_start(state):
    log("DB=" + str(db.db_path()))
    for k in ("KRYPT_POLYBOT_US_USERDATA", "KRYPT_POLYBOT_US_VAULT", "APPDATA", "LOCALAPPDATA", "USERPROFILE"):
        log(k + "=" + str(os.environ.get(k)))
    for t in ("app_kv", "user_scripts", "mcp_actions", "crypto15m_positions"):
        try:
            with db.get_db() as c:
                c.execute("SELECT 1 FROM " + t).fetchall()
            log("TABLE " + t + "=yes")
        except Exception:
            log("TABLE " + t + "=no")

def decide(ctx):
    return {"side": "up", "price": "ask", "size": 1}
'''


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    f = tmp_path / "real.db"
    monkeypatch.setattr(db, "db_path", lambda: f)
    _seed()
    return f


def test_the_backtest_child_gets_no_real_paths_and_no_private_tables(seeded, monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path / "RealAppData"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "RealLocal"))
    cfg = config.merge_with_defaults({"crypto15m_interval": "15m"})
    # the user's own code (an agent's could not import os/db at all)
    res = _run(service._script_backtest_isolated(cfg, PROBE, None, "mainnet", 2))
    assert not res.get("scriptError"), res.get("scriptError")
    logs = "\n".join(res["scriptLogs"])
    assert str(seeded) not in logs and "real.db" not in logs, logs
    assert "RealAppData" not in logs and "RealLocal" not in logs, logs
    for k in ("KRYPT_POLYBOT_US_USERDATA", "KRYPT_POLYBOT_US_VAULT", "APPDATA", "LOCALAPPDATA", "USERPROFILE"):
        line = next(ln for ln in res["scriptLogs"] if ln.startswith(k + "="))
        assert "kp-backtest-" in line, line
    for t in ("app_kv", "user_scripts", "mcp_actions", "crypto15m_positions"):
        assert f"TABLE {t}=no" in logs, logs
    assert res["n"] == 2                          # the extract still has the corpus


def test_the_extract_holds_only_research_rows(seeded, tmp_path):
    import sqlite3
    dest = tmp_path / "x" / "extract.db"
    counts = script_backtest_child.build_extract(seeded, dest, 2, "mainnet")
    assert counts["crypto15m_ticks"] == 6 and counts["crypto15m_signals"] == 2
    con = sqlite3.connect(dest)
    names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    con.close()
    assert names == {"crypto15m_ticks", "crypto15m_signals", "whale_trades", "alerts"}


def test_the_extract_folds_in_the_research_archive(seeded, tmp_path):
    import sqlite3
    arch = seeded.with_name(db.RESEARCH_ARCHIVE_NAME)
    con = sqlite3.connect(arch)
    con.execute(f"ATTACH DATABASE '{seeded}' AS live")
    con.execute("CREATE TABLE crypto15m_ticks AS SELECT * FROM live.crypto15m_ticks WHERE 0")
    con.execute("INSERT INTO crypto15m_ticks SELECT * FROM live.crypto15m_ticks LIMIT 1")  # a dup
    con.execute("UPDATE crypto15m_ticks SET id = 99999")                                   # + one new
    con.execute("INSERT INTO crypto15m_ticks SELECT * FROM live.crypto15m_ticks LIMIT 1")
    con.commit()
    con.close()
    counts = script_backtest_child.build_extract(seeded, tmp_path / "e.db", 2, "mainnet", arch)
    assert counts["crypto15m_ticks"] == 7


@pytest.mark.skipif(sys.platform != "win32", reason="Windows job objects")
def test_the_backtest_child_cannot_start_a_process(seeded):
    code = ("import subprocess, sys\n"
            "def on_start(state):\n"
            "    subprocess.run([sys.executable, '-c', 'pass'], check=True)\n"
            "def decide(ctx):\n    return None\n")
    cfg = config.merge_with_defaults({"crypto15m_interval": "15m"})
    res = _run(service._script_backtest_isolated(cfg, code, None, "mainnet", 2))
    err = str(res.get("scriptError") or "").lower()
    assert "quota" in err or "1816" in err, err


def test_the_user_backtesting_an_agent_script_runs_it_isolated(seeded, monkeypatch):
    calls = []

    async def _iso(*a, **k):
        calls.append(a)
        return {"n": 0, "trades": []}
    monkeypatch.setattr(service, "_script_backtest_isolated", _iso)
    with db.get_db() as conn:
        db.update_user_script(conn, "u1", author="agent:claude", needs_review=1)
    _run(service._h_script_backtest({"id": "u1", "sinceDays": 2}))
    assert len(calls) == 1
    with db.get_db() as conn:
        db.update_user_script(conn, "u1", author=None, needs_review=0)
    res = _run(service._h_script_backtest({"id": "u1", "sinceDays": 2}))   # the user's: in-process
    assert len(calls) == 1 and "n" in res
    with pytest.raises(ValueError, match="plain"):
        _run(service._h_script_backtest({"code": REJECTED["opens a file"], "sinceDays": 2},
                                        agent="claude"))
