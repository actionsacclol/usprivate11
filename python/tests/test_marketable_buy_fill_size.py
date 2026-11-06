"""What a placement reply says filled, read from the order inside it.

`POST /v1/orders` answers {id, executions}, and the newest execution carries the
order's state. A reply with no order is the exchange accepting it and not
answering inside its wait: that is unknown, and must never read as nothing filled.
"""
import mcp_rails
import terminal_trade as tt


def _reply(order=None):
    execs = [{"type": "EXECUTION_TYPE_NEW", "order": {"cumQuantity": 0}},
             {"type": "EXECUTION_TYPE_FILL", "order": order}] if order else []
    return {"raw": {"id": "o1", "executions": execs}}


def _order(**kw):
    return {"id": "o1", "intent": "ORDER_INTENT_BUY_LONG", "state": "ORDER_STATE_FILLED",
            "quantity": 6, **kw}


def test_the_newest_execution_says_what_filled():
    r = _reply(_order(cumQuantity=6, avgPx={"value": "0.17", "currency": "USD"}))
    assert tt._matched_size(r) == 6.0
    assert tt._reply_avg_cents(r) == 17.0


def test_a_fractional_fill_keeps_its_fraction():
    r = _reply(_order(cumQuantity=0.25, avgPx={"value": "0.5", "currency": "USD"}))
    assert tt._matched_size(r) == 0.25


def test_a_no_order_fill_reads_the_price_of_no():
    """The venue quotes every order on YES: a NO buy filled at .83 of YES cost 17."""
    r = _reply(_order(intent="ORDER_INTENT_BUY_SHORT", cumQuantity=2,
                      avgPx={"value": "0.83", "currency": "USD"}))
    assert tt._reply_avg_cents(r) == 17.0


def test_an_order_that_filled_nothing_is_a_known_zero_with_no_price():
    r = _reply(_order(cumQuantity=0, state="ORDER_STATE_NEW",
                      price={"value": "0.2", "currency": "USD"}))
    assert tt._matched_size(r) == 0.0
    assert tt._reply_avg_cents(r) is None


def test_a_reply_with_no_order_is_unknown_not_zero():
    assert tt._matched_size(_reply()) is None
    assert tt._reply_avg_cents(_reply()) is None


def test_a_recovered_send_is_unknown_not_zero():
    """The reply was lost and the order was found again by its open-order row."""
    assert tt._matched_size({"raw": {"recovered": True}}) is None


def test_ledger_books_a_fill_above_the_requested_size_as_filled():
    assert mcp_rails._fak_status(7.058824, 6.0) == "filled"
