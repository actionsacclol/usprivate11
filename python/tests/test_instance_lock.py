"""Cross-process wallet mutual-exclusion lock (instance_lock).

Two accounts importing the SAME wallet key would otherwise trade one on-chain
wallet concurrently. The lock lets the first backend claim the wallet by address;
a live sibling on the same address is told to back off until the holder exits
(or its heartbeat goes stale).
"""
from __future__ import annotations

import instance_lock as il


def test_claim_and_refresh_by_same_holder(tmp_path, monkeypatch):
    monkeypatch.setattr(il, "_lock_dir", lambda: tmp_path / "locks")
    assert il.claim("0xWALLET") is True
    assert il.claim("0xWALLET") is True            # heartbeat refresh by us is fine
    assert il.foreign_holder("0xWALLET") == ""      # we hold it -> no foreign holder


def test_second_instance_blocked_until_stale(tmp_path, monkeypatch):
    monkeypatch.setattr(il, "_lock_dir", lambda: tmp_path / "locks")
    assert il.claim("0xW") is True                  # instance A holds it
    monkeypatch.setattr(il, "_HOLDER_ID", "other-pid-999")  # now act as instance B
    assert il.claim("0xW") is False                 # B blocked while A's stamp is fresh
    assert il.foreign_holder("0xW") != ""
    assert il.claim("0xW", stale_sec=0.0) is True    # A's stamp "stale" -> B may take over


def test_release_frees_the_wallet(tmp_path, monkeypatch):
    monkeypatch.setattr(il, "_lock_dir", lambda: tmp_path / "locks")
    assert il.claim("0xW") is True
    il.release("0xW")
    monkeypatch.setattr(il, "_HOLDER_ID", "other")
    assert il.claim("0xW") is True                  # freed -> a sibling can claim now


def test_empty_address_is_fail_open(tmp_path, monkeypatch):
    monkeypatch.setattr(il, "_lock_dir", lambda: tmp_path / "locks")
    assert il.claim("") is True                      # no wallet -> never blocks trading
    assert il.foreign_holder("") == ""
