"""The one-click position exit, pinned.

This exists because of one sentence from the first live test: "selling is a
little wack… it technically works but I tried to sell at the same price I
bought at". A limit at your entry price does not cross a bid, so it RESTS —
an offer, not an exit. The panel this file guards prices a marketable
fill-and-kill instead, and every test here pins one way that could quietly
stop being true:

* the limit escaping the slippage cap the user picked,
* an order being sized past what the book can absorb, leaving a resting
  remainder the user thinks is an exit,
* a settled market being "sold" into a book that does not exist,
* an unreadable book or an unreadable account being treated as "nothing",
* a paper sell touching the real exchange,
* the confirmed dollar figure not being the one that leaves.
"""
from __future__ import annotations

import asyncio

import pytest

import db  # noqa: F401 — the paper_db fixture needs the module patched
import terminal_paper as tp
import terminal_trade as tt

# The book/market wiring and the DB fixture are the paper suite's; the fill
# seeding is the honesty suite's. Reused rather than re-forked so a change to
# either shape breaks in one place.
from test_terminal_paper import _mock_market, _order, paper_db  # noqa: E402,F401
from test_terminal_honesty import _seed_fills  # noqa: E402

# `_seed_fills` files everything under this condition id.
CID = "0xdead"


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture(autouse=True)
def _no_background_reconcile(monkeypatch):
    """`place()` fires a reconcile 3 s later. In a test that task outlives the
    loop and does nothing useful, so it never starts."""
    monkeypatch.setattr(tt, "_spawn", lambda coro: (coro.close(), None)[1])


def _wallet(monkeypatch, *, size=100.0, side="yes", cur_price=0.60,
            avg_price=None, outcome="Up"):
    """The live wallet stub — same shape as `test_terminal_honesty`'s
    `_fake_portfolio`, with the size and the venue's own average exposed so
    the basis tests can turn them on and off."""
    raw: dict = {"outcome": outcome}
    if avg_price is not None:
        raw["avgPrice"] = avg_price
        raw["initialValue"] = size * avg_price

    async def _positions(*_a, **_kw):
        if size <= 0:
            return []
        return [{
            "ticker": CID, "title": "Did it happen?",
            "position_fp": size if side == "yes" else -size,
            "cur_price": cur_price, "redeemable": False, "_raw": raw,
        }]

    monkeypatch.setattr(tt.api, "get_positions", _positions)


def _tradable(monkeypatch):
    """Everything `place()` needs that is not the book: a funded wallet and a
    venue that accepts, on a Live account (the backend's raw default config
    reads Paper). The order itself is captured, never sent."""
    sent: list[dict] = []
    monkeypatch.setattr(tt, "_paper_mode_on", lambda: False)

    monkeypatch.setattr(tt.auth, "credentials_present", lambda env=None: True)

    async def _place(**kw):
        sent.append(kw)
        return {"order": {"order_id": "oid-1", "status": "matched"}}

    monkeypatch.setattr(tt.api, "place_limit_order", _place)
    return sent


def _sell(**kw) -> dict:
    base = {"conditionId": CID, "side": "yes", "paper": False}
    base.update(kw)
    return base


# ────────────────────────────────────────────────────────────────────────
# Walking the bids inside the slippage cap
# ────────────────────────────────────────────────────────────────────────


def test_a_whole_position_that_fits_the_cap_sells_at_the_walked_average(
        paper_db, monkeypatch):
    _mock_market(monkeypatch, bids=[(60.0, 10), (58.0, 30)])
    _wallet(monkeypatch, size=40.0)
    pv = _run(tt.sell_position_preview(_sell(maxSlippageCents=5)))
    assert pv["blockers"] == []
    assert pv["size"] == 40.0
    assert pv["fillableSize"] == 40.0
    assert pv["bestBidCents"] == 60.0
    # (10 × 60 + 30 × 58) / 40 = 58.5 — the walked average, not the best bid.
    assert abs(pv["estimatedAvgCents"] - 58.5) < 1e-6
    # The limit is the DEEPEST level touched. A limit at the touch would only
    # cross the touch, which is the resting-order bug all over again.
    assert pv["limitCents"] == 58.0
    assert pv["worstFillCents"] == 58.0
    assert abs(pv["proceedsUsd"] - 23.40) < 1e-9


def test_the_limit_never_goes_below_the_slippage_cap(paper_db, monkeypatch):
    # A deep, cheap level sits below the cap. It must not be touched.
    _mock_market(monkeypatch, bids=[(60.0, 10), (40.0, 1000)])
    _wallet(monkeypatch, size=100.0)
    pv = _run(tt.sell_position_preview(_sell(maxSlippageCents=5)))
    assert pv["limitCents"] >= 60.0 - 5.0
    assert pv["fillableSize"] == 10.0, "the walk ate through the slippage cap"


def test_a_size_that_does_not_fit_the_cap_is_sized_down_and_says_so(
        paper_db, monkeypatch):
    _mock_market(monkeypatch, bids=[(60.0, 10), (50.0, 100)])
    _wallet(monkeypatch, size=40.0)
    pv = _run(tt.sell_position_preview(_sell(maxSlippageCents=5)))
    assert pv["blockers"] == []
    assert pv["size"] == 40.0
    assert pv["fillableSize"] == 10.0
    assert abs(pv["proceedsUsd"] - 6.0) < 1e-9
    # The user has to be told, in contracts, exactly what stays behind —
    # otherwise a partial exit reads as a full one.
    assert any("10 of 40" in w for w in pv["warnings"]), pv["warnings"]
    assert any("stay in your position" in w for w in pv["warnings"])


def test_a_zero_cent_cap_sells_only_at_the_best_bid(paper_db, monkeypatch):
    _mock_market(monkeypatch, bids=[(60.0, 10), (59.0, 1000)])
    _wallet(monkeypatch, size=100.0)
    pv = _run(tt.sell_position_preview(_sell(maxSlippageCents=0)))
    assert pv["fillableSize"] == 10.0
    assert pv["limitCents"] == 60.0
    assert pv["worstFillCents"] == 60.0


def test_the_order_is_sized_to_what_fits_so_nothing_rests(paper_db, monkeypatch):
    """The whole point. An FAK sized past the book kills the remainder, which
    is fine — but an order sized past the CAP would fill below it, and a GTC
    sized past the book would rest. Neither is an exit."""
    _mock_market(monkeypatch, bids=[(60.0, 10), (50.0, 100)])
    _wallet(monkeypatch, size=40.0)
    sent = _tradable(monkeypatch)
    pv = _run(tt.sell_position_preview(_sell(maxSlippageCents=5)))
    r = _run(tt.sell_position(_sell(maxSlippageCents=5,
                                    confirmUsd=pv["proceedsUsd"])))
    assert r["ok"] is True, r["message"]
    assert len(sent) == 1
    assert sent[0]["count"] == 10, "sent more size than the cap could absorb"
    assert sent[0]["order_type"] == "FAK"
    assert sent[0]["action"] == "sell"
    assert sent[0]["price_cents"] == 60
    assert r["sentSize"] == 10.0


# ────────────────────────────────────────────────────────────────────────
# Refusals — every one of these used to be a confident wrong number
# ────────────────────────────────────────────────────────────────────────


def test_an_unreadable_book_refuses_rather_than_guessing(paper_db, monkeypatch):
    _mock_market(monkeypatch, bids=[(60.0, 100)], available=False)
    _wallet(monkeypatch, size=40.0)
    pv = _run(tt.sell_position_preview(_sell()))
    assert pv["blockers"], "priced a sell against a book it could not read"
    assert "order book" in pv["blockers"][0].lower()
    assert pv["proceedsUsd"] is None


def test_no_bid_at_all_refuses(paper_db, monkeypatch):
    _mock_market(monkeypatch, bids=[])
    _wallet(monkeypatch, size=40.0)
    pv = _run(tt.sell_position_preview(_sell()))
    assert pv["blockers"] and "bidding" in pv["blockers"][0]


def test_no_position_refuses_instead_of_shorting(paper_db, monkeypatch):
    _mock_market(monkeypatch, bids=[(60.0, 100)])
    _wallet(monkeypatch, size=0.0)
    pv = _run(tt.sell_position_preview(_sell()))
    assert pv["blockers"] and "nothing to sell" in pv["blockers"][0]
    assert pv["positionSize"] is None


def test_an_unreadable_wallet_is_not_an_empty_one(paper_db, monkeypatch):
    async def _boom(*_a, **_kw):
        raise RuntimeError("rpc down")

    _mock_market(monkeypatch, bids=[(60.0, 100)])
    monkeypatch.setattr(tt.api, "get_positions", _boom)
    pv = _run(tt.sell_position_preview(_sell()))
    assert pv["blockers"]
    # "We could not ask" must never render as "you hold nothing".
    assert "could not read" in pv["blockers"][0].lower()


def test_a_won_settled_market_is_paid_by_the_exchange_not_sold(paper_db, monkeypatch):
    _mock_market(monkeypatch, bids=[(60.0, 100)], resolved="yes")
    _wallet(monkeypatch, size=40.0, side="yes")
    pv = _run(tt.sell_position_preview(_sell()))
    assert pv["settled"] is True
    assert pv["blockers"]
    assert "pays a won position automatically" in pv["blockers"][0]


def test_a_lost_settled_market_says_there_is_nothing_to_sell(
        paper_db, monkeypatch):
    _mock_market(monkeypatch, bids=[(60.0, 100)], resolved="no")
    _wallet(monkeypatch, size=40.0, side="yes")
    pv = _run(tt.sell_position_preview(_sell()))
    assert pv["blockers"]
    assert "nothing to sell" in pv["blockers"][0]
    assert "$0.00" in pv["blockers"][0]


def test_a_sub_minimum_holding_refuses_with_the_reason(paper_db, monkeypatch):
    """`preview()` only WARNS on a sub-minimum sell, because a hand-typed one
    may be worth attempting. A one-click exit is a promise that it will go
    through, so the refusal has to arrive here with the reason attached."""
    async def _market(cid, include_raw=False):
        m = {
            "conditionId": cid, "title": "T", "slug": "t", "category": "crypto",
            "status": "open", "resolvedOutcome": None,
            "closeTime": "2030-01-01T00:00:00Z", "probability": 50.0,
            "spread": 2.0, "liquidityUsd": 1e5, "hoursToClose": 100.0,
            "bid": 60.0, "ask": 62.0,
            "_raw": {"orderPriceMinTickSize": 0.01, "minimumTradeQty": 20,
                     "status": "MARKET_STATUS_OPEN", "active": True, "closed": False,
                     "feeCoefficient": 0},
        }
        return m if include_raw else {k: v for k, v in m.items() if k != "_raw"}

    _mock_market(monkeypatch, bids=[(60.0, 100)])
    monkeypatch.setattr(tt.term, "market", _market)
    _wallet(monkeypatch, size=5.0)
    pv = _run(tt.sell_position_preview(_sell()))
    assert pv["blockers"] and "minimum order" in pv["blockers"][0]


def test_the_confirmed_figure_is_the_one_that_leaves(paper_db, monkeypatch):
    _mock_market(monkeypatch, bids=[(60.0, 100)])
    _wallet(monkeypatch, size=40.0)
    sent = _tradable(monkeypatch)
    r = _run(tt.sell_position(_sell(maxSlippageCents=5, confirmUsd=5.0)))
    assert r["ok"] is False
    assert "book moved" in r["message"]
    assert "$24.00" in r["message"], r["message"]
    assert sent == [], "an order left on a figure the user never read"


def test_a_missing_confirmation_refuses(paper_db, monkeypatch):
    _mock_market(monkeypatch, bids=[(60.0, 100)])
    _wallet(monkeypatch, size=40.0)
    sent = _tradable(monkeypatch)
    r = _run(tt.sell_position(_sell()))
    assert r["ok"] is False and sent == []


# ────────────────────────────────────────────────────────────────────────
# Fees, net proceeds and P&L — unknown is never zero
# ────────────────────────────────────────────────────────────────────────


def _fees_on(monkeypatch, rate=0.07, exponent=1.0):
    monkeypatch.setattr(
        tt.api, "_fee_schedule_from_gamma",
        lambda _raw: {"enabled": True, "rate": rate, "exponent": exponent})


def test_fee_net_and_pnl_against_a_known_ledger_basis(paper_db, monkeypatch):
    _mock_market(monkeypatch, bids=[(60.0, 100)])
    _fees_on(monkeypatch)
    _seed_fills([{"action": "buy", "size": 100, "price": 40.0}])
    _wallet(monkeypatch, size=100.0)
    pv = _run(tt.sell_position_preview(_sell(maxSlippageCents=5)))
    assert pv["avgCostCents"] == 40.0
    assert pv["basisSource"] == "ledger"
    assert abs(pv["proceedsUsd"] - 60.0) < 1e-9
    # 0.07 × 0.6 × 0.4 = 0.0168 per share × 100.
    assert abs(pv["feeUsd"] - 1.68) < 1e-6
    assert abs(pv["netProceedsUsd"] - 58.32) < 1e-6
    # Net of the fee, against what these contracts actually cost: 58.32 − 40.
    assert abs(pv["estimatedPnlUsd"] - 18.32) < 1e-6


def test_an_unknown_basis_leaves_the_pnl_unknown_not_zero(paper_db, monkeypatch):
    _mock_market(monkeypatch, bids=[(60.0, 100)])
    _fees_on(monkeypatch)
    # No ledger fills, and the venue offers no average of its own.
    _wallet(monkeypatch, size=100.0, avg_price=None)
    pv = _run(tt.sell_position_preview(_sell()))
    assert pv["avgCostCents"] is None
    assert pv["estimatedPnlUsd"] is None, "guessed a P&L with no cost basis"
    assert pv["proceedsUsd"] is not None, "the proceeds are still knowable"
    assert any("not zero" in w for w in pv["warnings"]), pv["warnings"]


def test_an_unknown_fee_rate_is_stated_rather_than_treated_as_free(
        paper_db, monkeypatch):
    _mock_market(monkeypatch, bids=[(60.0, 100)])
    monkeypatch.setattr(tt.api, "_fee_schedule_from_gamma", lambda _raw: None)
    # `category` on the mock market is "crypto"; drop it so nothing can answer.
    _seed_fills([{"action": "buy", "size": 100, "price": 40.0}])
    _wallet(monkeypatch, size=100.0)
    monkeypatch.setattr(tt, "_fee_per_share", lambda *a, **k: None)
    pv = _run(tt.sell_position_preview(_sell()))
    assert pv["feeUsd"] is None
    assert pv["netProceedsUsd"] is None
    assert any("EXCLUDE fees" in w for w in pv["warnings"]), pv["warnings"]
    # Still a P&L, because the basis IS known — gross, and warned about.
    assert pv["estimatedPnlUsd"] is not None


def test_the_venues_own_average_is_used_and_stamped(paper_db, monkeypatch):
    _mock_market(monkeypatch, bids=[(60.0, 100)])
    _wallet(monkeypatch, size=100.0, avg_price=0.45)
    pv = _run(tt.sell_position_preview(_sell()))
    assert pv["avgCostCents"] == 45.0
    assert pv["basisSource"] == "data-api"


# ────────────────────────────────────────────────────────────────────────
# Paper — its own ledger, and it never reaches the exchange
# ────────────────────────────────────────────────────────────────────────


def test_a_paper_sell_reduces_the_paper_position_and_sends_nothing_live(
        paper_db, monkeypatch):
    _mock_market(monkeypatch, asks=[(50.0, 100)], bids=[(60.0, 100)])

    async def _never(**_kw):
        raise AssertionError("a paper sell reached the real exchange")

    monkeypatch.setattr(tt.api, "place_limit_order", _never)

    buy = _run(tp.place(_order(conditionId=CID, priceCents=55, size=20,
                               orderType="FAK")))
    assert buy["ok"] and buy["filledSize"] == 20.0

    pv = _run(tt.sell_position_preview(
        _sell(paper=True, size=8, maxSlippageCents=5)))
    assert pv["paper"] is True
    assert pv["positionSize"] == 20.0
    assert pv["basisSource"] == "paper"
    assert pv["fillableSize"] == 8.0

    r = _run(tt.sell_position(
        _sell(paper=True, size=8, maxSlippageCents=5,
              confirmUsd=pv["proceedsUsd"])))
    assert r["ok"] is True, r["message"]
    assert r["filledSize"] == 8.0
    assert abs(r["avgPriceCents"] - 60.0) < 1e-9
    assert abs(r["proceedsUsd"] - 4.8) < 1e-9
    assert r["killedSize"] == 0.0

    after = _run(tp.portfolio())["positions"]
    assert after[0]["size"] == 12.0, "the paper position did not shrink"


def test_a_paper_sell_reads_the_paper_book_not_the_wallet(paper_db, monkeypatch):
    """Paper and live are never mixed. With a real wallet position and an
    empty paper ledger, a paper sell must find nothing."""
    _mock_market(monkeypatch, bids=[(60.0, 100)])
    _wallet(monkeypatch, size=100.0)
    pv = _run(tt.sell_position_preview(_sell(paper=True)))
    assert pv["blockers"] and "paper" in pv["blockers"][0]


def test_paper_mode_still_refuses_a_live_sell(paper_db, monkeypatch):
    """`place()` reads the PAPER switch from the live config, so a live sell
    while paper mode is on is refused inside the module — not only at the RPC
    handler that the chat bots and the standing tick bypass."""
    _mock_market(monkeypatch, bids=[(60.0, 100)])
    _wallet(monkeypatch, size=40.0)
    sent = _tradable(monkeypatch)
    monkeypatch.setattr(tt, "_paper_mode_on", lambda: True)
    pv = _run(tt.sell_position_preview(_sell()))
    r = _run(tt.sell_position(_sell(confirmUsd=pv["proceedsUsd"])))
    assert r["ok"] is False
    assert "paper mode" in r["message"].lower()
    assert sent == []


# ────────────────────────────────────────────────────────────────────────
# Sizing edges
# ────────────────────────────────────────────────────────────────────────


def test_asking_for_more_than_you_hold_is_capped_not_shorted(
        paper_db, monkeypatch):
    _mock_market(monkeypatch, bids=[(60.0, 1000)])
    _wallet(monkeypatch, size=40.0)
    pv = _run(tt.sell_position_preview(_sell(size=500)))
    assert pv["size"] == 40.0
    assert pv["fillableSize"] == 40.0
    assert any("no naked short" in w for w in pv["warnings"]), pv["warnings"]


def test_a_fractional_position_is_floored_to_the_markets_step(
        paper_db, monkeypatch):
    # The exchange takes this market's step and no finer. Rounding there could sell
    # more than is held, so the floor happens here, visibly.
    _mock_market(monkeypatch, bids=[(60.0, 1000)])
    _wallet(monkeypatch, size=40.6)
    pv = _run(tt.sell_position_preview(_sell()))
    assert pv["size"] == 40.0
    assert pv["fillableSize"] == 40.0
    assert any("0.6 will be left behind" in w for w in pv["warnings"]), pv["warnings"]


def test_a_market_with_a_fractional_step_sells_the_whole_fraction(
        paper_db, monkeypatch):
    """minimumTradeQty of 0.01: nothing is left behind, and nothing is warned of."""
    _mock_market(monkeypatch, bids=[(60.0, 1000)], min_qty=0.01)
    _wallet(monkeypatch, size=40.6)
    pv = _run(tt.sell_position_preview(_sell()))
    assert pv["size"] == 40.6 and pv["fillableSize"] == 40.6
    assert not any("left behind" in w for w in pv["warnings"]), pv["warnings"]


def test_a_position_under_the_markets_step_cannot_be_sold_and_says_why(
        paper_db, monkeypatch):
    _mock_market(monkeypatch, bids=[(60.0, 1000)])
    _wallet(monkeypatch, size=0.4)
    pv = _run(tt.sell_position_preview(_sell()))
    assert pv["blockers"] and "smallest order step of 1" in pv["blockers"][0]


def test_the_depth_warning_fires_when_the_sell_eats_the_book(
        paper_db, monkeypatch):
    _mock_market(monkeypatch, bids=[(60.0, 100)])
    _wallet(monkeypatch, size=90.0)
    pv = _run(tt.sell_position_preview(_sell()))
    assert any("visible" in w and "depth" in w for w in pv["warnings"]), \
        pv["warnings"]
