"""Realised P&L comes from the FILL LEDGER, not from what the wallet still holds.

Shaped from the same real wallet as `test_track_real_cases.py`, and written
against the complaint that followed the last repair: **"it shows 105 losses,
which is not true — it thinks we lost every bet, so my realised profit shows
-$681.13."**

That was not an arithmetic bug. It was the unit of account.

The Positions tab is the WALLET, and on a prediction market a wallet is a
survivorship filter with the sign flipped:

  * a resolved WINNER leaves the wallet the moment it is redeemed, and a
    redemption is an on-chain claim rather than a CLOB trade — so it leaves no
    "sell" row in the fill ledger either;
  * a resolved LOSER stays in the wallet forever as $0 dust, because there is
    nothing to claim and no reason to spend the gas.

So summing realised P&L over "positions we still hold, plus explicit sells"
drops every single win by construction and totals the losses. On the author's
real wallet that read: 584 markets traded, 453 of them redeemed for a payout,
and a Track screen reporting 105 positions, 105 losses, realised -$681.13.

The fix these tests pin: settle EVERY market the ledger has fills for at $1 per
contract on the winning outcome and $0 on the loser, whether or not the tokens
are still in the wallet — a redeemed winner has already been PAID. An
unresolved market realises from its sells only, and a market whose outcome we
cannot read stays UNKNOWN and is counted as such. Never as a loss.

Public market ids only. No wallet address, no key: a condition id names a
market, not a person.
"""
from __future__ import annotations

import asyncio

import pytest

import db
import terminal_trade as tt

# Real, public market ids from the wallet that reported the bug.
BTC_UP = "0xf996adbcb9d5e40162d514d1616325982ae9ef5a48b2622db107018df80e8884"
DOGE_DOWN = "0x322f0d6502dc08f4f61c06d9fbba252711ac250a625330f92984ff4d0cb423e7"
GIANTS = "0x38644f38c63679e539c578c59f4a1be38eb4693d1bdaf0ead50f70b824efb6ba"
ETH_UP = "0x7d13e4a513aeabd0f0ed3c5703b0195d5feb6e23d17ccdac86f8a1ff41e3635a"


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "db_path", lambda: tmp_path / "record.db")
    db.init_db()
    return tmp_path


def _fills(rows):
    """Seed the fill ledger. `rows`: dicts of cid/side/action/size/price/fee."""
    with db.get_db() as conn:
        conn.execute("DELETE FROM terminal_fills")
        for i, r in enumerate(rows):
            conn.execute(
                """INSERT INTO terminal_fills
                   (trade_id, order_id, condition_id, title, side, action,
                    size, price_cents, fees_usd, cost_usd, origin, network,
                    filled_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (f"t{i}", None, r["cid"], r.get("title", "T"),
                 r.get("side", "yes"), r["action"], r["size"], r["price"],
                 r.get("fee", 0.0), r["size"] * r["price"] / 100.0,
                 r.get("origin", "manual"), "mainnet",
                 f"2026-01-01T00:00:{i:02d}Z"))


def _portfolio(monkeypatch, *, wallet=(), resolved=None, calls=None):
    """Run `portfolio()` against a stubbed wallet and stubbed Gamma.

    `wallet` is what /positions still returns — deliberately allowed to be
    EMPTY while the ledger is full, which is the redeemed-winner case.
    `resolved` maps conditionId -> 'yes' | 'no' | None; a market absent from
    the map is one Gamma would not answer for at all.
    """
    resolved = dict(resolved or {})

    async def _positions(*a, **kw):
        return [
            {"ticker": w["cid"], "title": w.get("title", "T"),
             "position_fp": w["size"] if w.get("side", "yes") == "yes" else -w["size"],
             "cur_price": w.get("price", 0.0),
             "redeemable": bool(w.get("redeemable")),
             "_raw": w.get("raw")}
            for w in wallet
        ]

    async def _summaries(ids):
        if calls is not None:
            calls.append(list(ids))
        out = []
        for cid in ids:
            if cid not in resolved:
                continue          # Gamma has nothing for this market.
            out.append({
                "conditionId": cid, "title": "T", "slug": None,
                "status": "settled" if resolved[cid] else "open",
                "resolvedOutcome": resolved[cid],
                "outcomes": ["Up", "Down"],
                "closeTime": "2026-01-01T00:00:00Z", "probability": 50.0,
            })
        return out

    async def _balance():
        raise RuntimeError("no wallet")

    monkeypatch.setattr(tt.api, "get_positions", _positions)
    monkeypatch.setattr(tt.term, "_summaries_for", _summaries)
    monkeypatch.setattr(tt.api, "get_balance", _balance)
    return _run(tt.portfolio())


# ────────────────────────────────────────────────────────────────────────
# The bug itself
# ────────────────────────────────────────────────────────────────────────


def test_a_redeemed_winner_absent_from_the_wallet_still_counts(fresh_db, monkeypatch):
    """THE regression. 5 contracts bought at 40c on a market that resolved our
    way, redeemed, and therefore gone from /positions. There is no sell row —
    a redemption is an on-chain claim — so the old code saw nothing at all and
    realised $0.00 on a bet that paid $5.00 for $2.00."""
    _fills([{"cid": BTC_UP, "side": "yes", "action": "buy",
             "size": 5.0, "price": 40.0}])
    r = _portfolio(monkeypatch, wallet=[], resolved={BTC_UP: "yes"})

    assert r["positions"] == [], "the winner really is out of the wallet"
    # 5 contracts settling at $1 against $2.00 of cost.
    assert r["realizedPnlUsd"] == pytest.approx(3.0)
    assert r["settledPnlUsd"] == pytest.approx(3.0)
    assert r["wonMarkets"] == 1
    assert r["lostMarkets"] == 0
    assert r["ledgerMarkets"] == 1


def test_a_losing_dust_position_counts_as_a_loss(fresh_db, monkeypatch):
    """The other half: the loser is still in the wallet, worth exactly $0, and
    it is a real loss. Nothing here is meant to hide it — the complaint was
    never that the losses were wrong."""
    _fills([{"cid": DOGE_DOWN, "side": "no", "action": "buy",
             "size": 5.0, "price": 40.0}])
    r = _portfolio(
        monkeypatch,
        wallet=[{"cid": DOGE_DOWN, "side": "no", "size": 5.0, "price": 0.0,
                 "redeemable": True}],
        resolved={DOGE_DOWN: "yes"})

    assert r["realizedPnlUsd"] == pytest.approx(-2.0)
    assert r["lostMarkets"] == 1
    assert r["wonMarkets"] == 0
    assert r["settledWorthlessCount"] == 1
    # Flagged redeemable by the venue, but worth nothing — no badge.
    assert r["redeemableCount"] == 0


def test_the_wallet_alone_reports_every_bet_as_a_loss(fresh_db, monkeypatch):
    """The shape of the real complaint, in miniature: three winners redeemed
    and gone, one loser left sitting in the wallet. Counted over the wallet
    this is "1 position, 1 loss, realised -$2.00" — which is what the user
    saw. Counted over the ledger it is 3 won, 1 lost, and a profit."""
    _fills([
        {"cid": BTC_UP, "side": "yes", "action": "buy", "size": 5.0, "price": 40.0},
        {"cid": ETH_UP, "side": "yes", "action": "buy", "size": 5.0, "price": 45.0},
        {"cid": GIANTS, "side": "no", "action": "buy", "size": 5.0, "price": 50.0},
        {"cid": DOGE_DOWN, "side": "no", "action": "buy", "size": 5.0, "price": 40.0},
    ])
    r = _portfolio(
        monkeypatch,
        wallet=[{"cid": DOGE_DOWN, "side": "no", "size": 5.0, "price": 0.0}],
        resolved={BTC_UP: "yes", ETH_UP: "yes", GIANTS: "no", DOGE_DOWN: "yes"})

    assert r["wonMarkets"] == 3
    assert r["lostMarkets"] == 1
    assert r["ledgerMarkets"] == 4
    # +3.00 +2.75 +2.50 -2.00
    assert r["realizedPnlUsd"] == pytest.approx(6.25)
    assert r["realizedPnlUsd"] > 0, "a profitable ledger reported as a loss"
    # And the wallet-scope figure — the one the venue's cashPnl is compared
    # against — is still the wallet's own -$2.00. Two different questions,
    # two different numbers, neither pretending to be the other.
    assert r["walletPnlUsd"] == pytest.approx(-2.0)


# ────────────────────────────────────────────────────────────────────────
# What the outcome did NOT decide
# ────────────────────────────────────────────────────────────────────────


def test_a_sold_out_unresolved_market_realises_its_sells_only(fresh_db, monkeypatch):
    """Bought 10 at 50c, sold all 10 at 60c, market still open. There is
    nothing left to settle, so the outcome decides nothing: realised is the
    $1.00 the sells banked, and the market scores neither a win nor a loss."""
    _fills([
        {"cid": BTC_UP, "side": "yes", "action": "buy", "size": 10.0, "price": 50.0},
        {"cid": BTC_UP, "side": "yes", "action": "sell", "size": 10.0, "price": 60.0},
    ])
    r = _portfolio(monkeypatch, wallet=[], resolved={BTC_UP: None})

    assert r["realizedPnlUsd"] == pytest.approx(1.0)
    assert r["settledPnlUsd"] is None, "an open market settled something"
    assert r["wonMarkets"] == 0
    assert r["lostMarkets"] == 0
    assert r["closedOutMarkets"] == 1
    assert r["unknownResolutionMarkets"] == 0


def test_a_sub_contract_remainder_does_not_score(fresh_db, monkeypatch):
    """Straight from the real ledger: selling 5 contracts back left 0.0794
    behind, because the venue nets in shares rather than lots. Those hundredths
    were never redeemed — they are not worth the gas — and calling that market
    "won" inflated the record past the venue's own redemption count. The P&L on
    them still counts; it is real money."""
    _fills([
        {"cid": BTC_UP, "side": "yes", "action": "buy",
         "size": 5.0794, "price": 64.64},
        {"cid": BTC_UP, "side": "yes", "action": "sell",
         "size": 5.0, "price": 60.35},
    ])
    r = _portfolio(monkeypatch, wallet=[], resolved={BTC_UP: "yes"})

    assert r["wonMarkets"] == 0, "a 0.08-contract remainder scored as a win"
    assert r["lostMarkets"] == 0
    assert r["closedOutMarkets"] == 1
    # But the remainder's 8c settlement is still in the money figure.
    assert r["settledPnlUsd"] == pytest.approx(0.03, abs=0.01)


def test_both_sides_held_to_settlement_land_in_both_columns(fresh_db, monkeypatch):
    """A paired crypto entry buys Up AND Down and holds both to settlement, so
    one market is genuinely a win and a loss at once. The counts say so instead
    of quietly picking one, and the note tells the user they do not add up."""
    _fills([
        {"cid": BTC_UP, "side": "yes", "action": "buy", "size": 5.0, "price": 55.0},
        {"cid": BTC_UP, "side": "no", "action": "buy", "size": 5.0, "price": 48.0},
    ])
    r = _portfolio(monkeypatch, wallet=[], resolved={BTC_UP: "yes"})

    assert r["wonMarkets"] == 1
    assert r["lostMarkets"] == 1
    assert r["bothSidesMarkets"] == 1
    assert r["ledgerMarkets"] == 1
    # +$5.00 payout against $5.15 of cost across the pair.
    assert r["realizedPnlUsd"] == pytest.approx(-0.15)
    assert "BOTH columns" in (r["recordNote"] or "")


# ────────────────────────────────────────────────────────────────────────
# Unknown stays unknown
# ────────────────────────────────────────────────────────────────────────


def test_an_unreadable_outcome_is_counted_never_assumed_lost(fresh_db, monkeypatch):
    """Gamma has nothing for this market. The contracts are outstanding, so we
    cannot say what they are worth — and the one thing we must not do is settle
    them at zero, which would turn every lookup failure into a loss."""
    _fills([{"cid": GIANTS, "side": "yes", "action": "buy",
             "size": 5.0, "price": 40.0}])
    r = _portfolio(monkeypatch, wallet=[], resolved={})   # Gamma answers nothing

    assert r["unknownResolutionMarkets"] == 1
    assert r["wonMarkets"] == 0
    assert r["lostMarkets"] == 0, "an unreadable market was scored as a loss"
    assert r["settledPnlUsd"] is None
    assert r["realizedPnlUsd"] is None, "an unknowable total was reported as $0"
    assert "could not read" in (r["recordNote"] or "")


def test_one_unknown_market_does_not_void_the_rest(fresh_db, monkeypatch):
    """The unknown is carved out, not contagious: the markets we CAN read are
    still settled, and the page is told how many were left out."""
    _fills([
        {"cid": BTC_UP, "side": "yes", "action": "buy", "size": 5.0, "price": 40.0},
        {"cid": GIANTS, "side": "yes", "action": "buy", "size": 5.0, "price": 40.0},
    ])
    r = _portfolio(monkeypatch, wallet=[], resolved={BTC_UP: "yes"})

    assert r["realizedPnlUsd"] == pytest.approx(3.0)
    assert r["wonMarkets"] == 1
    assert r["unknownResolutionMarkets"] == 1
    assert r["ledgerMarkets"] == 2


# ────────────────────────────────────────────────────────────────────────
# Fees, and the cost of asking
# ────────────────────────────────────────────────────────────────────────


def test_fees_are_reported_and_netted_rather_than_buried(fresh_db, monkeypatch):
    """On the real ledger the gross figure was +$11.87 and $76.14 of fees had
    been paid to earn it — the account was DOWN $64.27. Gross stays available
    because it is the convention the venue's own P&L uses; the net is the one
    the screen leads with."""
    _fills([
        {"cid": BTC_UP, "side": "yes", "action": "buy",
         "size": 5.0, "price": 40.0, "fee": 0.08},
        {"cid": DOGE_DOWN, "side": "yes", "action": "buy",
         "size": 5.0, "price": 40.0, "fee": 0.08},
    ])
    r = _portfolio(monkeypatch, wallet=[],
                   resolved={BTC_UP: "yes", DOGE_DOWN: "no"})

    assert r["realizedPnlUsd"] == pytest.approx(1.0)     # +3.00 -2.00
    assert r["realizedFeesUsd"] == pytest.approx(0.16)
    assert r["realizedPnlNetUsd"] == pytest.approx(0.84)


def test_a_resolved_outcome_is_asked_for_once_and_then_remembered(fresh_db, monkeypatch):
    """A resolved market never un-resolves, and the ledger reaches back over
    580 of them on the real wallet. Re-asking Gamma for every one on every
    12-second Track poll would be ~30 batched HTTP calls a refresh for an
    answer that cannot change, so a known outcome is written down once."""
    _fills([{"cid": BTC_UP, "side": "yes", "action": "buy",
             "size": 5.0, "price": 40.0}])
    calls: list = []
    first = _portfolio(monkeypatch, wallet=[], resolved={BTC_UP: "yes"},
                       calls=calls)
    asked_first = sum(len(c) for c in calls)
    calls.clear()
    second = _portfolio(monkeypatch, wallet=[], resolved={BTC_UP: "yes"},
                        calls=calls)

    assert asked_first >= 1, "the first pass never looked the market up"
    assert sum(len(c) for c in calls) == 0, "a settled outcome was re-fetched"
    assert second["realizedPnlUsd"] == first["realizedPnlUsd"]
    assert second["wonMarkets"] == 1


def test_the_cache_does_not_survive_the_database_moving(fresh_db, monkeypatch,
                                                        tmp_path):
    """A factory reset, a different wallet or a test's temp DB moves the file
    under us. An in-process cache that outlived that move would answer
    questions about somebody else's markets."""
    _fills([{"cid": BTC_UP, "side": "yes", "action": "buy",
             "size": 5.0, "price": 40.0}])
    _portfolio(monkeypatch, wallet=[], resolved={BTC_UP: "yes"})
    assert tt._resolutions_cached().get(BTC_UP) == "yes"

    monkeypatch.setattr(db, "db_path", lambda: tmp_path / "other.db")
    db.init_db()
    assert tt._resolutions_cached().get(BTC_UP) is None, "cache crossed databases"
