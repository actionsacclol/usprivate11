"""A rule-based split settles between 0 and 100c a share for the engines, not as a full win or loss."""
import polymarket_api as api
import trader


def _raw(prices, status="MARKET_STATUS_RESOLVED", closed=True):
    return {"slug": "game-3-winner", "question": "Game 3 winner?", "closed": closed,
            "status": status, "outcomes": '["Yes","No"]', "outcomePrices": prices}


def test_split_settles_at_half_for_both_sides():
    m = api._normalize_market(_raw('["0.5","0.5"]'))
    assert m["result"] == ""
    assert m["settlement_value_dollars"] == 0.5
    assert m["status"] == "settled"
    assert trader._market_yes_payout(m) == 0.5


def test_decided_markets_unchanged():
    assert api._normalize_market(_raw('["1","0"]'))["result"] == "yes"
    assert api._normalize_market(_raw('["0","1"]'))["result"] == "no"


def test_unresolved_half_prices_are_not_a_split():
    m = api._normalize_market(_raw('["0.5","0.5"]', status="MARKET_STATUS_RESOLVING"))
    assert m["result"] == "" and m["settlement_value_dollars"] is None
    assert trader._market_yes_payout(m) is None
