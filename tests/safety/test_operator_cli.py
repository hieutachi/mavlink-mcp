import json
from dataclasses import fields

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mavlink_mcp.operator_cli import main
from mavlink_mcp.safety.approval import ApprovalToken, TrustedKey, TrustStore, verify
from mavlink_mcp.safety.models import Action, Environment, FrozenRequest, canonical_bytes
from mavlink_mcp.safety.store import SafetyStore


def test_approve_refuses_non_tty_before_signing(tmp_path, monkeypatch):
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"request_id": "r1"}), encoding="utf-8")
    monkeypatch.setattr("sys.stdin", type("Input", (), {"isatty": lambda self: False})())
    with pytest.raises(SystemExit, match="interactive confirmation required"):
        main(["approve", str(request), "--key", str(tmp_path / "missing"), "--key-id", "k", "--output", str(tmp_path / "token")])
    assert not (tmp_path / "token").exists()


@pytest.fixture
def review(tmp_path, monkeypatch):
    now = 1_000_000
    private = Ed25519PrivateKey.generate()
    key_path = tmp_path / "test-only.key"
    key_path.write_bytes(private.private_bytes_raw())
    req = FrozenRequest(1, "r1", "actor", "vehicle", "gateway", "boot", Environment.MOCK,
                        "connection", "home", "policy", Action.ARM, {}, created_at_utc_ms=now - 50_000)
    obj = {f.name: getattr(req, f.name) for f in fields(FrozenRequest)}
    obj.update(nonce="nonce", issued_at_utc_ms=now, expires_at_utc_ms=now + 60_000,
               operator_id="untrusted-payload-identity")
    path = tmp_path / "review.json"
    path.write_bytes(canonical_bytes(obj))
    output = tmp_path / "approval.token"
    monkeypatch.setattr("sys.stdin", type("Input", (), {"isatty": lambda self: True})())
    monkeypatch.setattr("builtins.input", lambda prompt: "r1")
    monkeypatch.setattr("mavlink_mcp.operator_cli.time.time", lambda: (now + 10_000) / 1000)
    args = ["approve", str(path), "--key", str(key_path), "--key-id", "key", "--output", str(output)]
    return req, obj, path, output, args, private, now


def test_simulated_tty_test_key_roundtrip_uses_explicit_operator_and_wall_issue(review, tmp_path):
    req, obj, path, output, args, private, now = review
    with SafetyStore(tmp_path / "ledger.sqlite3", bootstrap=True, clock=lambda: now) as store:
        store.create_approval(req, nonce="nonce", expires_at_utc_ms=now + 60_000,
                              required_operator_role="human_operator")
        assert main(args + ["--operator-id", "human"]) == 0
        token = ApprovalToken.decode(output.read_text(encoding="ascii"))
        trust = TrustStore({"key": TrustedKey("key", "human", private.public_key().public_bytes_raw(),
                                              frozenset({"human_operator"}))})
        claims = verify(token, request=req, trust_store=trust, store=store, now_utc_ms=now + 10_000)
        assert claims["operator_id"] == "human"
        assert claims["issued_at_utc_ms"] == now + 10_000
        assert claims["expires_at_utc_ms"] == obj["expires_at_utc_ms"]
        assert claims["request_id"] == "r1"


def test_tty_requires_operator_id_instead_of_payload_identity(review, capsys):
    _, _, _, output, args, _, _ = review
    with pytest.raises(SystemExit) as caught: main(args)
    assert caught.value.code == 2
    assert "approve requires --operator-id" in capsys.readouterr().err
    assert not output.exists()


@pytest.mark.parametrize("content", ['{}', '[]', 'null', '{"request_id":"a","request_id":"b"}', '{"x":NaN}', pytest.param('x' * (256 * 1024 + 1), id="oversized")])
def test_invalid_duplicate_or_oversized_json_rejected_before_signing(review, content):
    _, _, path, output, args, _, _ = review
    path.write_text(content, encoding="utf-8")
    with pytest.raises(SystemExit): main(args + ["--operator-id", "human"])
    assert not output.exists()


@pytest.mark.parametrize("request_id", [None, "", "   ", 123])
def test_confirmation_never_accepts_missing_or_empty_id(review, monkeypatch, request_id):
    _, obj, path, output, args, _, _ = review
    obj["request_id"] = request_id
    path.write_bytes(canonical_bytes(obj))
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("must not prompt for invalid ID"))
    with pytest.raises(SystemExit, match="nonempty request ID"): main(args + ["--operator-id", "human"])
    assert not output.exists()


@pytest.mark.parametrize("response", ["", "r1 ", "R1", "another-id"])
def test_confirmation_requires_exact_request_id(review, monkeypatch, response):
    _, _, _, output, args, _, _ = review
    monkeypatch.setattr("builtins.input", lambda prompt: response)
    with pytest.raises(SystemExit, match="confirmation cancelled"): main(args + ["--operator-id", "human"])
    assert not output.exists()


def test_confirmation_wait_cannot_renew_expiry(review, monkeypatch):
    _, obj, _, output, args, _, _ = review
    def wait_then_confirm(prompt):
        monkeypatch.setattr("mavlink_mcp.operator_cli.time.time", lambda: obj["expires_at_utc_ms"] / 1000)
        return "r1"
    monkeypatch.setattr("builtins.input", wait_then_confirm)
    with pytest.raises(SystemExit, match="expired"): main(args + ["--operator-id", "human"])
    assert not output.exists()


@pytest.mark.parametrize("field,value", [
    ("expires_at_utc_ms", "1060000"), ("issued_at_utc_ms", True),
    ("schema_version", True), ("parameters", []), ("request_digest", ""),
    ("request_digest", "0" * 64), ("nonce", ""),
])
def test_invalid_review_fields_rejected_without_signing(review, field, value):
    _, obj, path, output, args, _, _ = review
    obj[field] = value
    path.write_bytes(canonical_bytes(obj))
    with pytest.raises(SystemExit, match="invalid approval request"): main(args + ["--operator-id", "human"])
    assert not output.exists()


def test_approve_help_documents_review_contract_and_identity(capsys):
    with pytest.raises(SystemExit) as caught: main(["approve", "--help"])
    assert caught.value.code == 0
    help_text = capsys.readouterr().out
    assert "--operator-id" in help_text
    assert "issued_at_utc_ms" in help_text
    assert "expires_at_utc_ms" in help_text
