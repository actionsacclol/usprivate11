"""quote(): a script's resting bids on both sides of a window, simulated.

The live test of 2026-11-07/08 placed resting YES bids by hand. Three filled, all
three because the price fell through them, and all three lost; the seven windows
that rallied never filled. Making a market on both sides was untestable then (NO
buys were locked) and had no tool: a script could buy one side once per window.

quote() is that tool, in shadow only. These pin what it simulates and the rails a
script cannot widen.
"""
from __future__ import annotations

import asyncio

import pytest

import config
import crypto15m
import db
import script_docs
import script_engine
import script_quotes as sq
import script_sandbox
import trader

ENV = "mainnet"
SID = "q1abc"


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "db_path", lambda: tmp_path / "quotes.db")
    db.init_db()
    monkeypatch.setattr(sq, "_working", {})
    for name in ("_compiled", "_hb", "_hb_last", "_status"):
        monkeypatch.setattr(script_engine, name, {}, raising=False)
    return tmp_path


def _cfg(**over):
    cfg = {"script_max_entry_cents": 97, "script_max_contracts": 20,
           "script_quote_max_inventory": 20, "crypto15m_order_size": 5, "script_poll_sec": 5}
    cfg.update(over)
    return cfg


def _asset(up_ask=0.50, down_ask=0.52, **over):
    a = {"asset": "BTC", "ticker": "btc-window", "closeTime": "2026-11-12T12:15:00Z",
         "upAsk": up_ask, "downAsk": down_ask, "minsLeft": 9.0, "hasMarket": True}
    a.update(over)
    return a


def _tick(want, asset=None, cfg=None, seen=None):
    """One engine tick with a quote() that answers `want`."""
    notes: list[str] = []

    def hook(ctx):
        if seen is not None:
            seen.append(ctx["quotes"])
        return want(ctx) if callable(want) else want
    err = sq.run_asset(SID, asset or _asset(), cfg or _cfg(), ENV, hook, notes.append)
    return err, notes


def _fills():
    with db.get_db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM script_shadow_orders ORDER BY id")]


def _resting(ticker="btc-window"):
    st = sq._working[(SID, ticker)]
    return {s: (st[s]["price"], st[s]["size"]) if st[s] else None for s in sq.SIDES}


# ────────────────────────────────────────────────────────────────────────
# A bid rests until the market comes to it
# ────────────────────────────────────────────────────────────────────────


def test_a_bid_rests_and_fills_only_when_the_offer_comes_down_to_it(fresh):
    want = {"up": 47, "down": 49, "size": 5, "reason": "both sides"}
    _, notes = _tick(want)
    assert _resting() == {"up": (47, 5), "down": (49, 5)}
    assert notes.count("quote_places") == 2 and _fills() == []

    # The offers stay above the bids: nothing proves a fill, so none is counted.
    _tick(want, _asset(up_ask=0.49, down_ask=0.53))
    assert _fills() == []

    # Up is offered at 47: a real bid at 47 would have been matched.
    _, notes = _tick(want, _asset(up_ask=0.47, down_ask=0.55))
    [row] = _fills()
    assert (row["side"], row["contracts"], row["entry_cents"]) == ("up", 5, 47)
    assert row["order_type"] == "POST" and row["source"] == "crypto"
    assert row["reason"] == "both sides" and "simulated maker fill" in row["note"]
    assert "quote_fills" in notes


def test_a_bid_cannot_fill_on_the_tick_it_was_placed(fresh):
    """It has to have been resting when the market came to it."""
    _tick({"up": 47, "size": 5}, _asset(up_ask=0.50))
    _tick(None, _asset(up_ask=0.50))                 # pulled
    _tick({"up": 47, "size": 5}, _asset(up_ask=0.48))  # placed again
    assert _fills() == []
    _tick({"up": 47, "size": 5}, _asset(up_ask=0.45))
    assert len(_fills()) == 1


def test_bids_nobody_watched_for_a_while_did_not_rest_through_it(fresh):
    _tick({"up": 47, "size": 5})
    sq._working[(SID, "btc-window")]["seen"] -= 61      # the engine was paused
    _tick({"up": 47, "size": 5}, _asset(up_ask=0.40))
    assert _fills() == [], "a bid was filled by a move nobody was quoting through"
    _tick({"up": 47, "size": 5}, _asset(up_ask=0.40))
    assert _fills() == [], "and the new bid at 47 is over the 40c offer: not placed"


# ────────────────────────────────────────────────────────────────────────
# The script re-prices; the engine keeps the book matching
# ────────────────────────────────────────────────────────────────────────


def test_a_new_price_replaces_the_bid_and_none_pulls_it(fresh):
    _tick({"up": 47, "down": 49, "size": 5})
    _, notes = _tick({"up": 45, "down": 49, "size": 5})
    assert _resting() == {"up": (45, 5), "down": (49, 5)}
    assert notes.count("quote_moves") == 1 and "quote_places" not in notes
    _, notes = _tick({"up": 45, "down": None, "size": 5})
    assert _resting() == {"up": (45, 5), "down": None} and notes.count("quote_cancels") == 1
    _, notes = _tick(None)
    assert _resting() == {"up": None, "down": None} and notes.count("quote_cancels") == 1


def test_the_script_sees_its_resting_bids_and_what_it_holds(fresh):
    seen: list[dict] = []
    want = {"up": 47, "down": 49, "size": 5}
    _tick(want, seen=seen)
    assert seen[0] == {"up": None, "down": None, "inventory": {"up": 0, "down": 0},
                       "avgCostCents": {"up": None, "down": None}, "maxInventory": 20}
    _tick(want, _asset(up_ask=0.47, down_ask=0.55), seen=seen)
    q = seen[1]
    assert q["up"] is None, "it filled, so it is no longer resting"
    assert q["down"]["priceCents"] == 49 and q["down"]["size"] == 5 and q["down"]["ageSec"] >= 0
    assert q["inventory"] == {"up": 5, "down": 0} and q["avgCostCents"]["up"] == 47.0


# ────────────────────────────────────────────────────────────────────────
# Rails the script cannot widen
# ────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("bid, asks", [
    (50, dict(up_ask=0.50)),          # at the offer: it would take
    (55, dict(up_ask=0.50)),          # through it
    (47, dict(up_ask=None)),          # no offer read: nothing says it would rest
])
def test_a_bid_that_would_take_or_cannot_be_checked_is_not_placed(fresh, bid, asks):
    _, notes = _tick({"up": bid, "size": 5}, _asset(**asks))
    assert _resting()["up"] is None and "quote_declined" in notes


def test_a_side_stops_bidding_at_the_inventory_limit(fresh):
    cfg = _cfg(script_quote_max_inventory=10)
    want = {"up": 47, "down": 49, "size": 5}
    _tick(want, cfg=cfg)
    for _ in range(4):
        _tick(want, _asset(up_ask=0.47, down_ask=0.55), cfg=cfg)   # fills, then re-arms
        _tick(want, _asset(up_ask=0.50, down_ask=0.52), cfg=cfg)
    held = sum(r["contracts"] for r in _fills() if r["side"] == "up")
    assert held == 10, "the script kept asking; the limit is the user's"
    assert _resting() == {"up": None, "down": (49, 5)}


def test_size_is_capped_and_meets_the_exchange_minimum_inside_the_caps(fresh):
    # Asked for 50: the per-order cap is 20.
    _tick({"up": 47, "size": 50})
    assert _resting()["up"] == (47, 20)
    # Asked for 1: the exchange takes no fewer than 5.
    _tick({"up": 47, "size": 1}, _asset(ticker="eth-window"))
    assert _resting("eth-window")["up"] == (47, 5)
    # At 8c the $1 minimum needs 13 contracts.
    _tick({"up": 8, "size": 5}, _asset(ticker="sol-window", up_ask=0.10))
    assert _resting("sol-window")["up"] == (8, 13)
    # ...and when the user's cap is under the minimum, refused, never rounded up past it.
    _, notes = _tick({"up": 8, "size": 5}, _asset(ticker="xrp-window", up_ask=0.10),
                     cfg=_cfg(script_max_contracts=10))
    assert _resting("xrp-window")["up"] is None and "quote_declined" in notes
    _, notes = _tick({"up": 98, "size": 5}, _asset(ticker="doge-window", up_ask=0.99))
    assert _resting("doge-window")["up"] is None, "over script_max_entry_cents"


@pytest.mark.parametrize("bad", [
    "up", ["up", 47], {"up": 47.5}, {"up": 0}, {"up": 100}, {"up": True},
    {"up": 47, "size": 0}, {"up": 47, "size": 2.5},
])
def test_a_malformed_answer_is_an_error_not_a_guess(fresh, bad):
    err, _ = _tick(bad)
    assert err and "quote()" in err
    assert sq._working[(SID, "btc-window")]["up"] is None


# ────────────────────────────────────────────────────────────────────────
# Settlement: a maker fill pays no fee
# ────────────────────────────────────────────────────────────────────────


def _settle(up_won):
    with db.get_db() as conn:
        conn.execute("INSERT INTO crypto15m_signals (ticker, asset, resolved, up_won) "
                     "VALUES ('btc-window','BTC',1,?)", (1 if up_won else 0,))
    asyncio.run(script_engine._settle_shadow_orders())
    return {r["side"]: r for r in _fills()}


@pytest.mark.parametrize("up_won", [True, False])
def test_both_sides_filled_under_a_dollar_pay_the_difference_either_way(fresh, up_won):
    want = {"up": 46, "down": 48, "size": 5}
    _tick(want)
    _tick(want, _asset(up_ask=0.46, down_ask=0.56))     # Up fills as the price falls
    _tick(want, _asset(up_ask=0.60, down_ask=0.48))     # then Down as it comes back
    rows = _settle(up_won)
    assert set(rows) == {"up", "down"}
    # 46 + 48 = 94c for a pair that pays 100c: 6c x 5, and no fee on either leg.
    assert rows["up"]["pnl_usd"] + rows["down"]["pnl_usd"] == pytest.approx(0.30)
    assert rows["up"]["outcome_correct"] == (1 if up_won else 0)


def test_one_side_filled_and_lost_loses_its_cost_and_no_fee(fresh):
    """The case the live test hit three times."""
    want = {"up": 46, "size": 5}
    _tick(want)
    _tick(want, _asset(up_ask=0.40, down_ask=0.62))
    assert _settle(up_won=False)["up"]["pnl_usd"] == pytest.approx(-2.30)


# ────────────────────────────────────────────────────────────────────────
# In the engine: shadow only
# ────────────────────────────────────────────────────────────────────────


QUOTER = "def quote(ctx):\n    return {'up': 47, 'down': 49, 'size': 5}\n"


def _engine_tick(monkeypatch, *, dry_run, assets):
    with db.get_db() as conn:
        db.upsert_user_script(conn, {"id": SID, "name": "Quoter", "description": "",
                                     "code": QUOTER, "notes": ""})
        db.update_user_script(conn, SID, enabled=1, dry_run=dry_run)

    async def _snapshot(_cfg):
        return {"assets": assets}

    async def _balance(_cfg, force=False):
        return 10_000, 0
    monkeypatch.setattr(crypto15m, "snapshot", _snapshot)
    monkeypatch.setattr(trader, "refresh_balance", _balance)
    monkeypatch.setattr(config, "engine_on", lambda cfg, key: True)
    cfg = config.merge_with_defaults({"crypto15m_order_size": 5})
    asyncio.run(script_engine.run_tick(cfg, authed=True))


def test_a_shadow_script_quotes_through_the_engine(fresh, monkeypatch):
    assert script_sandbox.validate(QUOTER) == [], "quote() alone is a whole script"
    _engine_tick(monkeypatch, dry_run=1, assets=[_asset()])
    assert _resting() == {"up": (47, 5), "down": (49, 5)}
    _engine_tick(monkeypatch, dry_run=1, assets=[_asset(up_ask=0.47, down_ask=0.55)])
    assert [(r["side"], r["order_type"]) for r in _fills()] == [("up", "POST")]
    # The window ends: its bids go with it.
    _engine_tick(monkeypatch, dry_run=1, assets=[_asset(ticker="next-window")])
    assert (SID, "btc-window") not in sq._working


def test_an_armed_script_does_not_quote(fresh, monkeypatch):
    """Its bids would have to be real, and nothing sends those."""
    sent = []

    async def _place(**kw):
        sent.append(kw)
    monkeypatch.setattr(script_engine.polymarket_api, "place_limit_order", _place)
    _engine_tick(monkeypatch, dry_run=0, assets=[_asset()])
    assert sq._working == {} and sent == [] and _fills() == []
    assert script_engine._hb.get(SID, {}).get("quote_skipped_armed") or \
        "quote skipped" in script_engine._heartbeat_line(
            {"quote_skipped_armed": 1}, shadow=False, open_count=0, day_pnl=0.0)


def test_a_quote_that_raises_disables_the_script(fresh, monkeypatch):
    global QUOTER
    monkeypatch.setitem(globals(), "QUOTER", "def quote(ctx):\n    return ctx['nope']\n")
    _engine_tick(monkeypatch, dry_run=1, assets=[_asset()])
    with db.get_db() as conn:
        row = db.get_user_script(conn, SID)
    assert row["enabled"] == 0 and "quote" in (row["last_error"] or "")


def test_the_heartbeat_counts_what_the_quotes_did():
    line = script_engine._heartbeat_line(
        {"ticks": 3, "quote": 12, "quote_places": 2, "quote_moves": 5, "quote_cancels": 1,
         "quote_fills": 1}, shadow=True, open_count=1, day_pnl=0.0)
    assert "quote ×12 → 2 placed, 5 moved, 1 pulled, 1 fill" in line


# ────────────────────────────────────────────────────────────────────────
# The shipped template
# ────────────────────────────────────────────────────────────────────────


def _template():
    return script_sandbox.CompiledScript("tmpl", script_docs.EXAMPLE_QUOTER, state={})


def _ctx(up=0, down=0, **over):
    ctx = _asset(**over)
    ctx["quotes"] = {"up": None, "down": None, "inventory": {"up": up, "down": down},
                     "avgCostCents": {"up": None, "down": None}, "maxInventory": 20}
    return ctx


def test_the_template_bids_under_the_middle_on_both_sides():
    out, err = sq.sanitize(_template().call("quote", _ctx(up_ask=0.50, down_ask=0.52)))
    assert err is None
    # Up trades 48 bid / 50 offered: middle 49. Down 48 / 52... middle 51.
    assert (out["up"], out["down"], out["size"]) == (46, 48, 5)
    assert out["up"] + out["down"] < 100, "a pair that costs a dollar earns nothing"


def test_the_template_stops_adding_to_the_heavier_side_and_stays_out_late():
    mod = _template()
    heavy, _ = sq.sanitize(mod.call("quote", _ctx(up=5, down=0)))
    assert heavy["up"] is None and heavy["down"] is not None
    assert mod.call("quote", _ctx(minsLeft=3.0)) is None
    assert mod.call("quote", _ctx(up_ask=0.50, down_ask=0.60)) is None, "a 10c spread"
    assert mod.call("quote", _ctx(up_ask=None)) is None
