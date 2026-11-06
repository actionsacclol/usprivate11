"""Remote control, pinned.

These bots let a chat message move real money, so every test here guards a
specific way that goes wrong: a stranger reaching a command, a switch that
turns on more than it should, a stale confirmation still being live tomorrow,
or a refusal that leaves no trace.
"""
from __future__ import annotations

import asyncio

import pytest

import db
import remote


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "db_path", lambda: tmp_path / "remote.db")
    db.init_db()
    remote._hits.clear()
    remote._pending.clear()
    remote._senders.clear()
    return tmp_path


# ────────────────────────────────────────────────────────────────────────
# The allowlist is the security boundary.
# ────────────────────────────────────────────────────────────────────────


def test_an_unpaired_sender_reaches_no_command(fresh, monkeypatch):
    """The token is not the boundary — tokens leak into screenshots and pasted
    logs. The paired-id list is, so an unlisted sender must be stopped before
    the command is even parsed."""
    called = []
    monkeypatch.setattr(remote, "_dispatch",
                        lambda *a, **k: called.append(a) or "should not run")
    for cmd in ("/status", "/flatten", "/buy 0xabc yes 10 50", "/pause"):
        _run(remote.handle("discord", "999", cmd))
    assert called == [], "an unpaired sender reached the dispatcher"


def test_an_unauthorised_attempt_is_logged(fresh):
    _run(remote.handle("telegram", "12345", "/flatten"))
    rows = remote.audit_log(10)
    assert rows and rows[0]["outcome"] == "unauthorised"
    assert rows[0]["sender"] == "12345"


def test_a_stranger_is_answered_once(fresh):
    """The first reply tells a real user how to pair. Every later one only
    tells a stranger the bot is alive and listening."""
    assert "not paired" in _run(remote.handle("discord", "999", "/status")).lower()
    assert _run(remote.handle("discord", "999", "/status")) == ""
    assert _run(remote.handle("discord", "999", "hello")) == ""
    # Pairing still answers: that is how the owner gets in.
    assert "No pairing code" in _run(remote.handle("discord", "999", "/pair 123456"))


def test_a_strangers_messages_are_one_row_with_a_count(fresh):
    for _ in range(3):
        _run(remote.handle("telegram", "12345", "/flatten"))
    rows = [r for r in remote.audit_log(100) if r["sender"] == "12345"]
    assert len(rows) == 1 and rows[0]["count"] == 3


def test_stranger_spam_cannot_push_the_owners_commands_out_of_the_log(
        fresh, monkeypatch):
    """The log is capped. Every stranger message used to be its own row in
    the same log as the owner's commands, so anyone who could DM the bot
    could flush the owner's real history out of it."""
    async def _dispatch(*a, **k):
        return "ok"
    monkeypatch.setattr(remote, "_dispatch", _dispatch)
    remote.add_paired("discord", "111")
    _run(remote.handle("discord", "111", "/flatten"))
    # One stranger hammering: one row, and past the rate limit nothing is
    # written (or answered) at all.
    for _ in range(remote.AUDIT_MAX + 20):
        _run(remote.handle("discord", "999000111", "hello"))
    rows = [r for r in remote.audit_log(remote.AUDIT_MAX) if r["sender"] == "999000111"]
    assert len(rows) == 1 and rows[0]["count"] == remote.RATE_LIMIT_PER_MIN
    # Many strangers, one message each.
    for i in range(remote.AUDIT_MAX + 20):
        _run(remote.handle("discord", f"9{i:05d}", "hello"))
    rows = remote.audit_log(remote.AUDIT_MAX)
    assert any(r["sender"] == "111" and r["command"] == "/flatten" for r in rows), \
        "stranger noise pushed the owner's command out of the log"
    assert sum(r["outcome"] == "unauthorised" for r in rows) == remote.STRANGER_AUDIT_MAX


def test_pairing_is_per_platform(fresh):
    remote.add_paired("discord", "111")
    assert remote.is_allowed("discord", "111")
    assert not remote.is_allowed("telegram", "111"), \
        "a Discord pairing leaked to Telegram"


def test_an_empty_id_is_never_allowed(fresh):
    assert not remote.is_allowed("discord", "")
    assert not remote.is_allowed("discord", "   ")


def test_unpairing_revokes_access(fresh):
    remote.add_paired("discord", "111")
    remote.remove_paired("discord", "111")
    assert not remote.is_allowed("discord", "111")
    assert "not paired" in _run(remote.handle("discord", "111", "/status")).lower()


# ────────────────────────────────────────────────────────────────────────
# Pairing codes.
# ────────────────────────────────────────────────────────────────────────


def test_a_pairing_code_works_once(fresh):
    code = remote.new_pair_code("telegram")["code"]
    ok, _ = remote.consume_pair_code("telegram", code, "555")
    assert ok and remote.is_allowed("telegram", "555")
    # A second use must fail, or a code seen over someone's shoulder stays
    # usable for as long as they like.
    ok2, msg = remote.consume_pair_code("telegram", code, "666")
    assert not ok2 and not remote.is_allowed("telegram", "666")


def test_a_wrong_code_pairs_nobody(fresh):
    remote.new_pair_code("telegram")
    ok, msg = remote.consume_pair_code("telegram", "000000", "777")
    assert not ok and not remote.is_allowed("telegram", "777")
    assert "not right" in msg


def test_an_expired_code_is_refused(fresh, monkeypatch):
    remote.new_pair_code("telegram")
    monkeypatch.setattr(remote, "_now",
                        lambda: __import__("time").time() + remote.PAIR_CODE_TTL_SEC + 5)
    ok, msg = remote.consume_pair_code("telegram", "123456", "888")
    assert not ok and "expired" in msg


def test_a_pairing_code_dies_after_five_wrong_tries_from_anyone(fresh):
    """The rate limit is per sender, so N accounts got N x the guesses at a
    six-digit code. Five wrong tries in all, and the code is gone."""
    code = remote.new_pair_code("telegram")["code"]
    wrong = "000000" if code != "000000" else "111111"
    for i in range(remote.PAIR_CODE_MAX_FAILS):
        ok, msg = remote.consume_pair_code("telegram", wrong, f"77{i}")
        assert not ok
    assert "canceled" in msg.lower()
    ok, msg = remote.consume_pair_code("telegram", code, "555")
    assert not ok and not remote.is_allowed("telegram", "555")
    assert "No pairing code" in msg


def test_pairing_with_no_code_active_is_refused(fresh):
    ok, msg = remote.consume_pair_code("telegram", "123456", "999")
    assert not ok and "No pairing code" in msg


def test_an_unpaired_sender_may_only_run_pair(fresh):
    code = remote.new_pair_code("telegram")["code"]
    reply = _run(remote.handle("telegram", "555", f"/pair {code}"))
    assert "Paired" in reply
    assert remote.is_allowed("telegram", "555")


# ────────────────────────────────────────────────────────────────────────
# The trading switch.
# ────────────────────────────────────────────────────────────────────────


def test_remote_trading_needs_both_switches():
    """Two switches on purpose: placing orders from a phone has a far bigger
    blast radius than reading from one, and must never ride along."""
    assert not remote.trading_allowed({})
    assert not remote.trading_allowed({"remote_enabled": True})
    assert not remote.trading_allowed({"remote_trading_enabled": True})
    assert remote.trading_allowed(
        {"remote_enabled": True, "remote_trading_enabled": True})


def test_buy_is_refused_when_remote_trading_is_off(fresh, monkeypatch):
    remote.add_paired("discord", "111")

    async def _cfg():
        return {"remote_enabled": True, "remote_trading_enabled": False}
    monkeypatch.setattr(remote, "_cfg", _cfg)

    reply = _run(remote.handle("discord", "111", "/buy 0xabc yes 10 50"))
    assert "turned off" in reply.lower()


def test_help_hides_trading_commands_when_it_is_off(fresh, monkeypatch):
    remote.add_paired("discord", "111")

    async def _cfg():
        return {"remote_enabled": True, "remote_trading_enabled": False}
    monkeypatch.setattr(remote, "_cfg", _cfg)
    assert "/buy" not in _run(remote.handle("discord", "111", "/help"))

    async def _cfg2():
        return {"remote_enabled": True, "remote_trading_enabled": True}
    monkeypatch.setattr(remote, "_cfg", _cfg2)
    assert "/buy" in _run(remote.handle("discord", "111", "/help"))


# ────────────────────────────────────────────────────────────────────────
# Order staging: preview, cap, confirm, expiry.
# ────────────────────────────────────────────────────────────────────────


def _trading_cfg(monkeypatch, cap=100.0, paper=False, live=True):
    async def _cfg():
        return {"remote_enabled": True, "remote_trading_enabled": True,
                "remote_max_order_usd": cap, "terminal_paper_mode": paper,
                "enable_trading": live}
    monkeypatch.setattr(remote, "_cfg", _cfg)


def _fake_preview(monkeypatch, cost=5.0, blockers=()):
    import terminal_trade

    async def _preview(order):
        return {"blockers": list(blockers), "warnings": [],
                "costUsd": cost, "feeUsd": 0.1, "size": order["size"],
                "priceCents": order["priceCents"], "fillableSize": order["size"],
                "estimatedAvgCents": order["priceCents"]}
    monkeypatch.setattr(terminal_trade, "preview", _preview)


def test_a_buy_stages_and_does_not_place(fresh, monkeypatch):
    remote.add_paired("discord", "111")
    _trading_cfg(monkeypatch)
    _fake_preview(monkeypatch)
    placed = []
    import terminal_trade

    async def _place(order, **kw):
        placed.append(order)
        return {"ok": True, "message": "sent"}
    monkeypatch.setattr(terminal_trade, "place", _place)

    reply = _run(remote.handle("discord", "111", "/buy 0xabc yes 10 50"))
    assert "/confirm" in reply
    assert placed == [], "an order was placed without a confirmation"


def test_confirm_places_the_staged_order(fresh, monkeypatch):
    remote.add_paired("discord", "111")
    _trading_cfg(monkeypatch)
    _fake_preview(monkeypatch)
    placed = []
    import terminal_trade

    async def _place(order, **kw):
        placed.append(order)
        return {"ok": True, "message": "sent"}
    monkeypatch.setattr(terminal_trade, "place", _place)

    _run(remote.handle("discord", "111", "/buy 0xabc yes 10 50"))
    _run(remote.handle("discord", "111", "/confirm"))
    assert len(placed) == 1
    assert placed[0]["conditionId"] == "0xabc"


def test_a_confirmation_expires(fresh, monkeypatch):
    """A '/buy 50' left unconfirmed overnight must not be executable by
    whoever picks the phone up tomorrow."""
    remote.add_paired("discord", "111")
    _trading_cfg(monkeypatch)
    _fake_preview(monkeypatch)
    _run(remote.handle("discord", "111", "/buy 0xabc yes 10 50"))
    monkeypatch.setattr(remote, "_now",
                        lambda: __import__("time").time() + remote.CONFIRM_TTL_SEC + 5)
    reply = _run(remote.handle("discord", "111", "/confirm"))
    assert "expired" in reply or "Nothing pending" in reply


def test_a_confirmation_is_not_reusable(fresh, monkeypatch):
    remote.add_paired("discord", "111")
    _trading_cfg(monkeypatch)
    _fake_preview(monkeypatch)
    import terminal_trade

    async def _place(order, **kw):
        return {"ok": True, "message": "sent"}
    monkeypatch.setattr(terminal_trade, "place", _place)
    _run(remote.handle("discord", "111", "/buy 0xabc yes 10 50"))
    _run(remote.handle("discord", "111", "/confirm"))
    assert "Nothing pending" in _run(remote.handle("discord", "111", "/confirm"))


def test_the_order_cap_is_enforced_in_the_backend(fresh, monkeypatch):
    remote.add_paired("discord", "111")
    _trading_cfg(monkeypatch, cap=10.0)
    _fake_preview(monkeypatch, cost=50.0)
    reply = _run(remote.handle("discord", "111", "/buy 0xabc yes 100 50"))
    assert "cap" in reply.lower()
    assert "/confirm" not in reply


def test_a_blocked_order_never_stages(fresh, monkeypatch):
    remote.add_paired("discord", "111")
    _trading_cfg(monkeypatch)
    _fake_preview(monkeypatch, blockers=["Market is closed."])
    reply = _run(remote.handle("discord", "111", "/buy 0xabc yes 10 50"))
    assert "Refused" in reply
    assert "Nothing pending" in _run(remote.handle("discord", "111", "/confirm"))


def test_confirm_is_independent_of_the_automation_switch(fresh, monkeypatch):
    """A chat order is a MANUAL order. `enable_trading` runs the engines; it
    must not gate the terminal (2026-09-01, owner's decision). Paper mode, the
    remote's own trading switch, the per-order cap and place()'s gates still
    apply — those are exercised by the tests around this one."""
    remote.add_paired("discord", "111")
    _trading_cfg(monkeypatch, live=False)
    _fake_preview(monkeypatch)
    placed = []
    import terminal_trade

    async def _place(order, **kw):
        placed.append(order)
        return {"ok": True, "message": "sent"}
    monkeypatch.setattr(terminal_trade, "place", _place)

    _run(remote.handle("discord", "111", "/buy 0xabc yes 10 50"))
    reply = _run(remote.handle("discord", "111", "/confirm"))
    assert len(placed) == 1, "the automation switch blocked a manual chat order"
    assert "Live trading is off" not in reply


def test_paper_mode_routes_a_remote_order_to_the_simulator(fresh, monkeypatch):
    remote.add_paired("discord", "111")
    _trading_cfg(monkeypatch, paper=True)
    _fake_preview(monkeypatch)
    import terminal_paper
    import terminal_trade
    real, paper = [], []

    async def _place(order, **kw):
        real.append(order)
        return {"ok": True, "message": "sent"}

    async def _paper(order, cfg=None):
        paper.append(order)
        return {"ok": True, "message": "paper filled"}
    monkeypatch.setattr(terminal_trade, "place", _place)
    monkeypatch.setattr(terminal_paper, "place", _paper)

    reply = _run(remote.handle("discord", "111", "/buy 0xabc yes 10 50"))
    assert "PAPER" in reply
    _run(remote.handle("discord", "111", "/confirm"))
    assert real == [] and len(paper) == 1


def test_a_malformed_order_is_rejected(fresh, monkeypatch):
    remote.add_paired("discord", "111")
    _trading_cfg(monkeypatch)
    for bad in ("/buy", "/buy 0xabc", "/buy 0xabc maybe 10 50", "/buy 0xabc yes ten 50"):
        reply = _run(remote.handle("discord", "111", bad))
        assert "Usage" in reply or "must be" in reply, f"accepted {bad!r}"


# ────────────────────────────────────────────────────────────────────────
# Rate limiting + parsing.
# ────────────────────────────────────────────────────────────────────────


def test_rate_limiting_kicks_in(fresh, monkeypatch):
    remote.add_paired("discord", "111")

    async def _cfg():
        return {"remote_enabled": True}
    monkeypatch.setattr(remote, "_cfg", _cfg)
    replies = [_run(remote.handle("discord", "111", "/whoami"))
               for _ in range(remote.RATE_LIMIT_PER_MIN + 3)]
    assert any("Slow down" in r for r in replies)


def test_rate_limits_are_per_sender(fresh, monkeypatch):
    remote.add_paired("discord", "111")
    remote.add_paired("discord", "222")

    async def _cfg():
        return {"remote_enabled": True}
    monkeypatch.setattr(remote, "_cfg", _cfg)
    for _ in range(remote.RATE_LIMIT_PER_MIN + 2):
        _run(remote.handle("discord", "111", "/whoami"))
    # The second account must be unaffected.
    assert "Slow down" not in _run(remote.handle("discord", "222", "/whoami"))


def test_telegram_style_command_suffix_is_stripped(fresh, monkeypatch):
    """Telegram delivers '/status@YourBot' in groups; without stripping the
    suffix every command in a group would read as unknown."""
    remote.add_paired("telegram", "111")

    async def _cfg():
        return {"remote_enabled": True}
    monkeypatch.setattr(remote, "_cfg", _cfg)
    reply = _run(remote.handle("telegram", "111", "/whoami@KryptPolyBot"))
    assert "Unknown command" not in reply


def test_an_empty_message_is_ignored(fresh):
    assert _run(remote.handle("discord", "111", "   ")) == ""


# ────────────────────────────────────────────────────────────────────────
# Token storage.
# ────────────────────────────────────────────────────────────────────────


def test_bot_secrets_reject_an_arbitrary_platform():
    """`platform` names a file. Only the two known values may reach the path."""
    import bot_secrets
    for bad in ("../../evil", "wallet", "", "discord/../x"):
        with pytest.raises(ValueError):
            bot_secrets._path(bad)


def test_a_stored_token_round_trips_and_clears(tmp_path, monkeypatch):
    import bot_secrets
    import polymarket_auth as auth
    monkeypatch.setattr(auth, "_credentials_dir", lambda: tmp_path)
    bot_secrets.save_token("telegram", "123:ABCdefGHI")
    assert bot_secrets.load_token("telegram") == "123:ABCdefGHI"
    assert bot_secrets.has_token("telegram")
    # The masked form must not be the token.
    m = bot_secrets.masked("telegram")
    assert m and m != "123:ABCdefGHI"
    bot_secrets.clear_token("telegram")
    assert not bot_secrets.has_token("telegram")


def test_a_token_is_not_written_in_plaintext_json(tmp_path, monkeypatch):
    """It must not land in a file the user pastes into a support thread."""
    import bot_secrets
    import polymarket_auth as auth
    monkeypatch.setattr(auth, "_credentials_dir", lambda: tmp_path)
    bot_secrets.save_token("discord", "SECRET-TOKEN-VALUE")
    for f in tmp_path.rglob("*"):
        if f.is_file() and f.suffix in (".json", ".txt"):
            assert b"SECRET-TOKEN-VALUE" not in f.read_bytes()


# ────────────────────────────────────────────────────────────────────────
# Stale commands must not execute on startup.
# ────────────────────────────────────────────────────────────────────────


def _tg(monkeypatch):
    """bot_telegram with its outbound + dispatch stubbed."""
    import bot_telegram as tg
    sent, handled = [], []

    async def _send(chat_id, text):
        sent.append((chat_id, text))

    async def _handle(platform, sender, text, label=""):
        handled.append((sender, text))
        return "ok"

    monkeypatch.setattr(tg, "send", _send)
    monkeypatch.setattr(tg.remote, "handle", _handle)
    monkeypatch.setattr(tg.remote, "audit", lambda *a, **k: None)
    return tg, sent, handled


def _update(uid, text, age_sec, user_id="7", chat_id="42"):
    import time as _t
    return {"update_id": uid, "message": {
        "text": text, "chat": {"id": chat_id},
        "from": {"id": user_id, "username": "me"},
        "date": int(_t.time() - age_sec)}}


def test_a_stale_command_is_acknowledged_but_never_executed(fresh, monkeypatch):
    """Telegram queues undelivered updates for ~24h and a fresh process starts
    from offset 0. Without a guard, a "/flatten" sent at 2am while the PC was
    asleep EXECUTES when the machine wakes at 9am — closing positions on a
    seven-hour-old instruction the user long since stopped meaning."""
    tg, sent, handled = _tg(monkeypatch)
    _run(tg.process_updates([_update(1, "/flatten", 7 * 3600)]))
    assert handled == [], "a seven-hour-old /flatten was executed"
    assert sent and "Ignored a command" in sent[0][1]


def test_a_stale_command_still_advances_the_offset(fresh, monkeypatch):
    """Acknowledged, so it does not come back on the next poll forever."""
    tg, _s, _h = _tg(monkeypatch)
    tg._offset = 0
    _run(tg.process_updates([_update(77, "/flatten", 9999)]))
    assert tg._offset == 78


def test_a_fresh_command_still_runs(fresh, monkeypatch):
    tg, sent, handled = _tg(monkeypatch)
    _run(tg.process_updates([_update(2, "/status", 3)]))
    assert handled == [("7", "/status")]


def test_a_batch_of_stale_messages_warns_once(fresh, monkeypatch):
    tg, sent, handled = _tg(monkeypatch)
    _run(tg.process_updates([_update(i, "/status", 9999) for i in range(5)]))
    assert handled == []
    warnings = [t for _c, t in sent if "Ignored a command" in t]
    assert len(warnings) == 1, f"warned {len(warnings)} times for one batch"


def test_a_message_with_no_date_is_not_assumed_stale(fresh, monkeypatch):
    # Absent timestamp = unknown age. Refusing on unknown would make the bot
    # unusable against any payload shape that omits it.
    tg, sent, handled = _tg(monkeypatch)
    _run(tg.process_updates([{
        "update_id": 9, "message": {
            "text": "/status", "chat": {"id": "42"}, "from": {"id": "7"}}}]))
    assert handled == [("7", "/status")]


def test_the_staleness_threshold_is_the_boundary(monkeypatch):
    import bot_telegram as tg
    import time as _t
    fresh_msg = {"date": int(_t.time() - (tg._MAX_COMMAND_AGE_SEC - 10))}
    old_msg = {"date": int(_t.time() - (tg._MAX_COMMAND_AGE_SEC + 10))}
    assert tg.is_stale(fresh_msg)[0] is False
    assert tg.is_stale(old_msg)[0] is True
    assert tg.is_stale({})[0] is False


def test_a_message_with_no_text_is_skipped(fresh, monkeypatch):
    tg, sent, handled = _tg(monkeypatch)
    _run(tg.process_updates([{"update_id": 3, "message": {"chat": {"id": "42"}}}]))
    assert handled == [] and sent == []


# ────────────────────────────────────────────────────────────────────────
# Telegram: the SENDER is the identity, never the chat.
# ────────────────────────────────────────────────────────────────────────


def test_the_telegram_identity_is_the_sender_not_the_chat(fresh, monkeypatch):
    """A group chat id is shared by everyone in it — using it as the identity
    would hand the remote control to every current and future member."""
    tg, sent, handled = _tg(monkeypatch)
    _run(tg.process_updates(
        [_update(1, "/flatten", 3, user_id="7", chat_id="-1009")]))
    assert handled == [("7", "/flatten")], "the group chat id was the identity"
    # The reply still goes back to the group the command came from.
    assert sent and sent[0][0] == "-1009"


def test_pairing_in_a_group_pairs_the_member_not_the_group(fresh, monkeypatch):
    """Real handle(), so the allowlist itself is exercised."""
    import bot_telegram as tg
    sent = []

    async def _send(chat_id, text):
        sent.append((chat_id, text))
    monkeypatch.setattr(tg, "send", _send)

    async def _cfg():
        return {"remote_enabled": True}
    monkeypatch.setattr(remote, "_cfg", _cfg)

    code = remote.new_pair_code("telegram")["code"]
    _run(tg.process_updates(
        [_update(1, f"/pair {code}", 3, user_id="7", chat_id="-1009")]))
    assert remote.is_allowed("telegram", "7")
    assert not remote.is_allowed("telegram", "-1009"), "the whole group paired"

    # Another member of the same group is still a stranger.
    _run(tg.process_updates(
        [_update(2, "/flatten", 3, user_id="8", chat_id="-1009")]))
    assert any("not paired" in t.lower() for _c, t in sent), \
        "a group member rode in on someone else's pairing"


def test_a_message_with_no_sender_id_is_dropped(fresh, monkeypatch):
    """No identity means no authorisation — a channel post or an anonymous
    group admin has no `from.id` to check against the allowlist."""
    tg, sent, handled = _tg(monkeypatch)
    _run(tg.process_updates([{"update_id": 4, "message": {
        "text": "/flatten", "chat": {"id": "42"}}}]))
    assert handled == [] and sent == []


# ────────────────────────────────────────────────────────────────────────
# /pause has to land in the RUNNING config, not just in Electron's copy.
# ────────────────────────────────────────────────────────────────────────


def _fake_service(monkeypatch, cfg=None):
    """A stand-in for the service module. remote imports it lazily inside the
    functions, so a sys.modules entry is all it takes."""
    import sys
    import types
    mod = types.ModuleType("service")
    events = []
    mod.STATE = types.SimpleNamespace(cfg=dict(cfg or {}), auth_ok=True)

    async def _emit(name, payload=None):
        events.append((name, payload))
    mod.emit_event = _emit
    monkeypatch.setitem(sys.modules, "service", mod)
    return mod, events


def test_pause_lands_in_the_backend_config_as_snake_case(fresh, monkeypatch):
    """The patch is camelCase and STATE.cfg is snake_case. Merging it raw only
    ADDED a dead `enableTrading` key, so the trading loop — which reads
    `enable_trading` — carried on straight through a /pause. The only thing
    that ever stopped it was an Electron round-trip whose failure is
    swallowed."""
    remote.add_paired("discord", "111")
    svc, events = _fake_service(monkeypatch, {
        "remote_enabled": True, "enable_trading": True,
        "crypto15m_enabled": True,
        "scripts_live_enabled": False})

    _run(remote.handle("discord", "111", "/pause"))

    assert svc.STATE.cfg["enable_trading"] is False
    assert svc.STATE.cfg["crypto15m_enabled"] is False
    assert "enableTrading" not in svc.STATE.cfg, "the camelCase key survived"
    # Electron still gets its copy, so the pause outlives a restart.
    assert any(n == "remote:configPatch" for n, _p in events)


def test_resume_puts_the_snake_case_flags_back(fresh, monkeypatch):
    remote.add_paired("discord", "111")
    svc, _e = _fake_service(monkeypatch, {
        "remote_enabled": True, "enable_trading": True,
        "crypto15m_enabled": True})

    _run(remote.handle("discord", "111", "/pause"))
    assert svc.STATE.cfg["enable_trading"] is False
    _run(remote.handle("discord", "111", "/resume"))
    assert svc.STATE.cfg["enable_trading"] is True
    assert svc.STATE.cfg["crypto15m_enabled"] is True


# ────────────────────────────────────────────────────────────────────────
# /flatten closes EVERYTHING, so it confirms first; /cancelall reaches both
# ledgers. Both used to reach less than /positions and /orders showed.
# ────────────────────────────────────────────────────────────────────────


def _flatten_service(monkeypatch, closed=0):
    svc, _e = _fake_service(monkeypatch, {"remote_enabled": True})

    async def _h_flatten(_p):
        return {"closed": closed, "canceled": 0, "sold": closed}

    async def _h_cancel(_p):
        return {"canceled": closed}
    svc._h_flatten = _h_flatten
    svc._h_cancelAllOpen = _h_cancel
    return svc


def _wallet(monkeypatch, positions=0, orders=0, left=None, orders_left=None,
            flatten=None, preview=None, canceled=None):
    """Stub the whole wallet side of the terminal.

    `positions` / `orders` are what the PREVIEW sees; `left` and `orders_left`
    are what the re-read afterwards sees, which is the case that catches a
    reply claiming success it cannot see.
    """
    import terminal_trade
    seen = {"portfolio": 0, "resting": 0, "canceled": []}

    async def _portfolio():
        seen["portfolio"] += 1
        n = positions if left is None else left
        return {"positions": [{"conditionId": f"0x{i}"} for i in range(n)]}

    async def _resting():
        seen["resting"] += 1
        n = (orders_left if (orders_left is not None and seen["resting"] > 1)
             else orders)
        return {"orders": [{"conditionId": f"0x{i}", "orderId": f"oid{i}"}
                           for i in range(n)], "note": None}

    async def _cancel(order_id):
        seen["canceled"].append(order_id)
        return {"ok": True, "message": "canceled"}

    async def _preview():
        return preview if preview is not None else {
            "ok": True,
            "positions": [{"conditionId": f"0x{i}", "side": "yes", "size": 10,
                           "bidCents": 50.0, "fillableSize": 10,
                           "proceedsUsd": 5.0, "blocked": None,
                           "partial": None, "engineTracked": False}
                          for i in range(positions)],
            "count": positions, "skipped": 0,
            "proceedsUsd": round(5.0 * positions, 2), "engineTracked": 0,
            "paper": False, "message": "ok",
        }

    async def _flatten_wallet(*, allow_trading, origin="manual", reason="flatten"):
        seen["flattenArgs"] = {"allow_trading": allow_trading, "reason": reason}
        return flatten if flatten is not None else {
            "ok": True, "attempted": positions, "sold": positions,
            "refused": 0, "skipped": 0, "proceedsUsd": 5.0 * positions,
            "engineTrackedSold": 0, "reason": reason, "results": [],
            "message": f"Sold {positions} of {positions} open position(s).",
        }

    monkeypatch.setattr(terminal_trade, "portfolio", _portfolio)
    monkeypatch.setattr(terminal_trade, "resting_orders", _resting)
    monkeypatch.setattr(terminal_trade, "cancel", _cancel)
    monkeypatch.setattr(terminal_trade, "flatten_preview", _preview)
    monkeypatch.setattr(terminal_trade, "flatten_wallet", _flatten_wallet)
    return seen


def test_flatten_previews_and_sells_nothing_until_confirm(fresh, monkeypatch):
    """It closes every position you own, so one fat-fingered tap must not be
    able to do it — same two-step contract as a chat order."""
    remote.add_paired("discord", "111")
    _flatten_service(monkeypatch, closed=1)
    seen = _wallet(monkeypatch, positions=3)

    reply = _run(remote.handle("discord", "111", "/flatten"))
    assert "/confirm" in reply
    assert "3 position(s) sold at the best bid" in reply
    assert "$15.00" in reply, "the estimated proceeds were not shown"
    assert "MARKET" in reply, "it did not say the sells are market orders"
    assert "flattenArgs" not in seen, "/flatten sold before /confirm"


def test_confirm_runs_both_the_engine_and_the_wallet(fresh, monkeypatch):
    """A user who reads "Closed 1 position" and puts the phone down must not
    still be holding three. The confirm closes BOTH ledgers."""
    remote.add_paired("discord", "111")
    _flatten_service(monkeypatch, closed=1)
    seen = _wallet(monkeypatch, positions=3, left=0)

    _run(remote.handle("discord", "111", "/flatten"))
    reply = _run(remote.handle("discord", "111", "/confirm"))
    assert "Closed 1 bot position(s)." in reply
    assert "Sold 3 of 3 open position(s)." in reply
    assert "Nothing else open" in reply
    # Flatten is a MANUAL de-risk; the automation master switch (off in this
    # fixture) does not gate it (2026-09-01, owner's decision).
    assert seen["flattenArgs"]["allow_trading"] is True, \
        "the wallet flatten was held to the automation switch"


def test_confirm_flattens_with_the_automation_switch_on_too(fresh, monkeypatch):
    remote.add_paired("discord", "111")
    svc = _flatten_service(monkeypatch, closed=0)
    svc.STATE.cfg["enable_trading"] = True
    seen = _wallet(monkeypatch, positions=1, left=0)

    _run(remote.handle("discord", "111", "/flatten"))
    _run(remote.handle("discord", "111", "/confirm"))
    assert seen["flattenArgs"]["allow_trading"] is True


def test_a_flatten_confirmation_expires(fresh, monkeypatch):
    remote.add_paired("discord", "111")
    _flatten_service(monkeypatch, closed=1)
    seen = _wallet(monkeypatch, positions=2)

    _run(remote.handle("discord", "111", "/flatten"))
    monkeypatch.setattr(remote, "_now", lambda: __import__("time").time() + 999)
    reply = _run(remote.handle("discord", "111", "/confirm"))
    assert "Nothing pending" in reply
    assert "flattenArgs" not in seen, "an expired flatten still sold"


def test_a_pending_flatten_is_discarded_by_cancel(fresh, monkeypatch):
    remote.add_paired("discord", "111")
    _flatten_service(monkeypatch, closed=1)
    seen = _wallet(monkeypatch, positions=2)

    _run(remote.handle("discord", "111", "/flatten"))
    assert "nothing was sold" in _run(remote.handle("discord", "111", "/cancel"))
    assert "Nothing pending" in _run(remote.handle("discord", "111", "/confirm"))
    assert "flattenArgs" not in seen


def test_a_flatten_confirms_without_the_remote_trading_switch(fresh, monkeypatch):
    """De-risking is available to any paired account — the second switch gates
    ENTRIES. Getting out must never need more permission than getting in."""
    remote.add_paired("discord", "111")
    _flatten_service(monkeypatch, closed=0)
    seen = _wallet(monkeypatch, positions=1, left=0)

    _run(remote.handle("discord", "111", "/flatten"))
    reply = _run(remote.handle("discord", "111", "/confirm"))
    assert "Remote trading is off" not in reply
    assert "flattenArgs" in seen


def test_flatten_never_claims_nothing_is_open_while_the_wallet_holds(
        fresh, monkeypatch):
    remote.add_paired("discord", "111")
    _flatten_service(monkeypatch, closed=0)
    _wallet(monkeypatch, positions=2)

    reply = _run(remote.handle("discord", "111", "/flatten"))
    assert "Nothing open to close" not in reply
    assert "2 position(s)" in reply


def test_flatten_says_so_when_there_really_is_nothing_open(fresh, monkeypatch):
    remote.add_paired("discord", "111")
    _flatten_service(monkeypatch, closed=0)
    _wallet(monkeypatch, positions=0)

    reply = _run(remote.handle("discord", "111", "/flatten"))
    assert "Nothing open to close." in reply
    assert "/confirm" not in reply, "it staged a flatten with nothing to flatten"


def test_flatten_reports_what_the_wallet_still_holds_after_the_confirm(
        fresh, monkeypatch):
    remote.add_paired("discord", "111")
    _flatten_service(monkeypatch, closed=1)
    _wallet(monkeypatch, positions=3, left=1, flatten={
        "ok": False, "attempted": 3, "sold": 2, "refused": 1, "skipped": 0,
        "proceedsUsd": 10.0, "engineTrackedSold": 1, "reason": "r",
        "results": [{"conditionId": "0x2", "side": "yes", "size": 4,
                     "ok": False, "message": "below the market minimum"}],
        "message": "Sold 2 of 3 open position(s).",
    })

    _run(remote.handle("discord", "111", "/flatten"))
    reply = _run(remote.handle("discord", "111", "/confirm"))
    assert "Sold 2 of 3" in reply
    assert "below the market minimum" in reply, "a refusal was swallowed"
    assert "1 position(s) are still open" in reply


def test_flatten_keeps_an_unreadable_wallet_unknown(fresh, monkeypatch):
    """An unknown wallet is not an empty one — and must not stage a flatten
    whose scope nobody can see."""
    remote.add_paired("discord", "111")
    _flatten_service(monkeypatch, closed=1)
    seen = _wallet(monkeypatch, positions=1, preview={
        "ok": False, "positions": [], "count": 0, "skipped": 0,
        "proceedsUsd": None, "engineTracked": None, "paper": False,
        "message": "Could not read your positions (RuntimeError: gamma is down).",
    })

    reply = _run(remote.handle("discord", "111", "/flatten"))
    assert "Could not read your positions" in reply
    assert "/confirm" not in reply
    assert "flattenArgs" not in seen


def test_flatten_says_paper_mode_is_on_before_you_confirm(fresh, monkeypatch):
    """Better said at the preview than after a /confirm that sells nothing."""
    remote.add_paired("discord", "111")
    _flatten_service(monkeypatch, closed=0)
    _wallet(monkeypatch, positions=1, preview={
        "ok": True, "positions": [], "count": 1, "skipped": 0,
        "proceedsUsd": 5.0, "engineTracked": 0, "paper": True, "message": "ok",
    })

    reply = _run(remote.handle("discord", "111", "/flatten"))
    assert "PAPER MODE IS ON" in reply


def test_cancelall_cancels_both_ledgers(fresh, monkeypatch):
    """The engine cancels what it placed; the terminal's own resting orders —
    a stop that rested, a GTC left on the book — need their own cancel."""
    remote.add_paired("discord", "111")
    _flatten_service(monkeypatch, closed=1)
    seen = _wallet(monkeypatch, orders=4, orders_left=0)

    reply = _run(remote.handle("discord", "111", "/cancelall"))
    assert "Canceled 1 bot resting order(s)." in reply
    assert "Canceled 4 of 4 remaining resting order(s)" in reply
    assert seen["canceled"] == ["oid0", "oid1", "oid2", "oid3"]
    assert "No resting orders left." in reply


def test_cancelall_never_claims_no_orders_while_some_rest(fresh, monkeypatch):
    remote.add_paired("discord", "111")
    _flatten_service(monkeypatch, closed=0)
    _wallet(monkeypatch, orders=2)

    reply = _run(remote.handle("discord", "111", "/cancelall"))
    assert reply != "No resting orders."
    assert "2 resting order(s) are still on the book" in reply


# ────────────────────────────────────────────────────────────────────────
# The Discord zombie: a socket that stops answering.
# ────────────────────────────────────────────────────────────────────────


class _FakeGatewayWS:
    """Says HELLO, says READY, then goes silent forever — the shape of a
    half-open TCP connection: sends still "succeed", recv() never returns."""

    def __init__(self, interval_ms=40):
        import json as _j
        self.sent = []
        self.closed = asyncio.Event()
        self._script = [
            _j.dumps({"op": 10, "d": {"heartbeat_interval": interval_ms}}),
            _j.dumps({"op": 0, "t": "READY", "s": 1,
                      "d": {"user": {"id": "9", "username": "bot"},
                            "session_id": "s1"}}),
        ]

    async def recv(self):
        if self._script:
            return self._script.pop(0)
        await self.closed.wait()
        raise ConnectionError("connection closed")

    async def send(self, raw):
        import json as _j
        self.sent.append(_j.loads(raw))

    async def close(self):
        self.closed.set()


class _FakeConnect:
    def __init__(self, ws):
        self._ws = ws

    async def __aenter__(self):
        return self._ws

    async def __aexit__(self, *exc):
        return False


def test_a_gateway_that_stops_acking_is_torn_down(fresh, monkeypatch):
    """_OP_HEARTBEAT_ACK used to be defined and never read, and the websockets
    library's own liveness check was disabled — so a half-open socket left the
    bot reporting "connected" forever while every command was lost."""
    import sys
    import types

    import bot_discord as bd

    monkeypatch.setattr(bd.bot_secrets, "load_token", lambda p: "tok")
    # 200 ms, not less: the watchdog trips at 1.5 intervals, and on Windows
    # Python 3.11 a sleep wakes on a 15.6 ms clock tick, so a 40 ms interval's
    # first sleep could overshoot the 60 ms limit before any heartbeat went out.
    ws = _FakeGatewayWS(interval_ms=200)
    mod = types.ModuleType("websockets")
    mod.connect = lambda url, **kw: _FakeConnect(ws)
    monkeypatch.setitem(sys.modules, "websockets", mod)

    async def _drive():
        bd._stop = False
        bd._connected = False
        bd._last_error = None
        bd._task = asyncio.ensure_future(bd._run())
        for _ in range(300):
            if bd._connected:
                break
            await asyncio.sleep(0.01)
        assert bd.is_running(), "READY did not register as a live session"
        # Nothing ever ACKs. The watchdog has to close the socket itself.
        await asyncio.wait_for(ws.closed.wait(), timeout=5.0)
        bd._stop = True
        await asyncio.wait_for(bd._task, timeout=5.0)
        return bd.is_running(), bd.last_error(), list(ws.sent)

    try:
        running, err, sent = _run(_drive())
    finally:
        bd._stop = True
        bd._task = None
        bd._connected = False

    assert any(m.get("op") == 1 for m in sent), "no heartbeat was ever sent"
    assert running is False, "a dead socket still reported as running"
    assert "ACK" in (err or ""), f"last_error did not name the cause: {err!r}"


# ────────────────────────────────────────────────────────────────────────
# AI agents' queued live orders, answered by phone
# ────────────────────────────────────────────────────────────────────────


def _queue_agent_order(committed=5.0):
    with db.get_db() as conn:
        cur = conn.execute(
            "INSERT INTO mcp_orders (created_at, network, mode, agent_id, "
            "condition_id, title, side, action, size, price_cents, committed_usd, "
            "ok, status) VALUES (datetime('now'),'mainnet','live','default',"
            "'0xabc','Will it rain?','yes','buy',10,50,?,0,'pending')", (committed,))
        return int(cur.lastrowid)


def test_agents_lists_what_is_waiting(fresh, monkeypatch):
    remote.add_paired("discord", "111")
    _fake_service(monkeypatch, {"remote_enabled": True})
    rid = _queue_agent_order()
    reply = _run(remote.handle("discord", "111", "/agents"))
    assert f"#{rid}" in reply and "Will it rain?" in reply and "$5.00" in reply


def test_reject_needs_no_trading_switch_but_approve_does(fresh, monkeypatch):
    remote.add_paired("discord", "111")
    _fake_service(monkeypatch, {"remote_enabled": True, "remote_trading_enabled": False})
    a, b = _queue_agent_order(), _queue_agent_order()
    reply = _run(remote.handle("discord", "111", f"/approve {a}"))
    assert "needs remote trading on" in reply
    with db.get_db() as conn:
        assert conn.execute("SELECT status FROM mcp_orders WHERE id=?", (a,)).fetchone()[0] == "pending"
    reply = _run(remote.handle("discord", "111", f"/reject {b}"))
    assert "Rejected" in reply
    with db.get_db() as conn:
        assert conn.execute("SELECT status FROM mcp_orders WHERE id=?", (b,)).fetchone()[0] == "rejected"


def test_approve_by_phone_re_vets_and_never_sends_on_a_paper_app(fresh, monkeypatch):
    remote.add_paired("discord", "111")
    # Agents on paper now: the queued live order must not go out.
    _fake_service(monkeypatch, {"remote_enabled": True, "remote_trading_enabled": True,
                                "mcp_trade_mode": "paper"})
    rid = _queue_agent_order()
    reply = _run(remote.handle("discord", "111", f"/approve {rid}"))
    assert "no longer passes" in reply
    with db.get_db() as conn:
        assert conn.execute("SELECT status FROM mcp_orders WHERE id=?", (rid,)).fetchone()[0] == "failed"
