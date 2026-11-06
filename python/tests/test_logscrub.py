"""Credentials never reach a log line, pinned.

Users paste their logs into chats when something breaks. Every test here is a
way a wallet key, a CLOB secret, an agent token or an AI key would otherwise
ride along — or a way the scrubber would shred the ids that make a trading
log useful.
"""
from __future__ import annotations

import io
import json
import logging

import pytest

import logscrub


@pytest.fixture(autouse=True)
def _clean():
    saved = list(logscrub._values)
    logscrub.set_known_secrets([])
    yield
    logscrub.set_known_secrets(saved)


def test_shapes_are_redacted_even_if_never_registered():
    for secret in ("MTIzNDU2Nzg5MDEyMzQ1Njc4.GabcDE.abcdefghijklmnopqrstuvwxyz0123",  # discord
                   "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsawQ",                 # telegram
                   "sk-ant-api03-abcdefghijklmnopqrstuvwxyz",
                   "sk-or-v1-0123456789abcdef0123456789abcdef",
                   "AIzaSyA1234567890abcdefghijklmnopqrstuv",
                   "kp_" + "Ab9_-" * 9):
        out = logscrub.scrub(f"value={secret} end")
        assert secret not in out and logscrub.REDACTED in out, secret


def test_a_telegram_token_in_a_url_path_is_redacted():
    line = "GET https://api.telegram.org/bot1234:abcSECRETdef/getUpdates"
    out = logscrub.scrub(line)
    assert "abcSECRETdef" not in out and "api.telegram.org/bot" in out


def test_a_pem_block_and_a_labelled_hex_key_are_redacted():
    pem = "-----BEGIN PRIVATE KEY-----\nMIIEv\n-----END PRIVATE KEY-----"
    assert "MIIEv" not in logscrub.scrub(f"key: {pem}")
    hexkey = "0x" + "9f" * 32
    for line in (f"private_key={hexkey}", f"privateKey: {hexkey[2:]}", f"secret {hexkey}"):
        assert "9f9f9f9f" not in logscrub.scrub(line), line


def test_condition_ids_addresses_and_order_ids_survive():
    cid = "0x" + "ab" * 32
    addr = "0x" + "12" * 20
    oid = "0x" + "cd" * 32
    uid = "3f2a1b9c-1111-2222-3333-444455556666"
    line = f"BUY 10 YES in {cid} from {addr} order {oid} id {uid}"
    assert logscrub.scrub(line) == line


def test_authorization_and_signature_headers_keep_their_name():
    for line in ("Authorization: Bearer kp_abcdefghijklmnop",
                 "X-PM-Signature: abcdefghijklmnop=="):
        out = logscrub.scrub(line)
        assert out.split(":")[0].strip().lower() in out.lower()
        assert "abcdefghijklmnop" not in out


def test_a_registered_secret_is_redacted_in_any_context():
    logscrub.set_known_secrets(["my-very-secret-passphrase"])
    out = logscrub.scrub('{"passphrase":"my-very-secret-passphrase"}')
    assert "my-very-secret" not in out


def test_longer_secrets_are_redacted_before_shorter_ones():
    logscrub.set_known_secrets(["abcdefghijklmnop", "abcdefghijklmnopqrstuvwxyz"])
    assert logscrub.scrub("x abcdefghijklmnopqrstuvwxyz y") == f"x {logscrub.REDACTED} y"


def test_trivially_short_values_are_not_registered():
    logscrub.set_known_secrets(["short", "", None, "abc"])  # type: ignore[list-item]
    assert logscrub.known_count() == 0
    assert logscrub.scrub("a short abc line") == "a short abc line"


def test_looks_secret_folds_case_and_catches_fragments():
    tok = "kp_" + "Zz9" * 14
    logscrub.set_known_secrets([tok])
    assert logscrub.looks_secret(tok.upper())
    assert logscrub.looks_secret(tok[5:25])          # a clipped fragment
    assert not logscrub.looks_secret("claude-code 2.1")
    assert not logscrub.looks_secret("")


def test_empty_and_none_are_safe():
    assert logscrub.scrub("") == ""
    assert logscrub.scrub(None) is None  # type: ignore[arg-type]


def _logger_with_handler():
    stream = io.StringIO()
    h = logging.StreamHandler(stream)
    h.setFormatter(logging.Formatter("%(name)s %(message)s"))
    parent = logging.getLogger("scrubtest")
    parent.handlers[:] = [h]
    parent.propagate = False
    parent.setLevel(logging.INFO)
    logscrub.install(parent)
    return parent, stream


def test_a_child_loggers_record_is_scrubbed_at_the_handler():
    parent, stream = _logger_with_handler()
    logscrub.set_known_secrets(["wallet-secret-value-123"])
    logging.getLogger("scrubtest.child").info("key is wallet-secret-value-123")
    assert "wallet-secret-value-123" not in stream.getvalue()
    assert logscrub.REDACTED in stream.getvalue()


def test_a_secret_passed_as_a_lazy_arg_is_still_scrubbed():
    parent, stream = _logger_with_handler()
    logscrub.set_known_secrets(["lazy-arg-secret-xyz"])
    parent.info("got %s", "lazy-arg-secret-xyz")
    assert "lazy-arg-secret-xyz" not in stream.getvalue()


def test_a_traceback_is_scrubbed():
    parent, stream = _logger_with_handler()
    logscrub.set_known_secrets(["traceback-secret-abc"])
    try:
        raise ValueError("bad key traceback-secret-abc")
    except ValueError:
        parent.exception("failed")
    assert "traceback-secret-abc" not in stream.getvalue()


def test_install_is_idempotent():
    parent, _ = _logger_with_handler()
    logscrub.install(parent)
    logscrub.install(parent)
    assert sum(isinstance(f, logscrub.ScrubFilter) for f in parent.handlers[0].filters) == 1


def test_refresh_reads_every_secret_in_the_store(tmp_path, monkeypatch):
    """The store, not config, is the list: API secret, agent tokens, bot tokens, AI keys."""
    import polymarket_auth as auth
    monkeypatch.setattr(auth, "_credentials_dir", lambda: tmp_path)
    secret = "c2VjcmV0LXNlY3JldC1zZWNyZXQtc2VjcmV0LXNlY3JldA=="
    key_id = "11111111-aaaa-bbbb-cccc-222222222222"
    files = {
        "apikey.mainnet.key": json.dumps({"keyId": key_id, "secretKey": secret}),
        "mcp.token": "kp_default_token_value_abcdefghij",
        "mcp_sam.token": "kp_sam_token_value_abcdefghijklm",
        "bot.discord.token": "discord-bot-token-value-xyz",
        "ai.anthropic.key": "anthropic-key-value-0123456789",
    }
    for name, val in files.items():
        auth._write_secret_bytes(tmp_path / name, val.encode())
    logscrub.refresh_known_secrets()
    for v in (secret, "kp_default_token_value_abcdefghij", "kp_sam_token_value_abcdefghijklm",
              "discord-bot-token-value-xyz", "anthropic-key-value-0123456789"):
        assert v not in logscrub.scrub(f"line {v} end"), v
    # The key id is public: it goes out on every request and tells keys apart.
    assert key_id in logscrub.scrub(f"key {key_id}")


def test_the_backend_installs_the_filter_on_its_handlers():
    import pathlib
    src = (pathlib.Path(__file__).resolve().parents[1] / "service.py").read_text(encoding="utf-8")
    assert "logscrub.install(root)" in src
