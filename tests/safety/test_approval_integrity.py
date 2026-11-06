"""Offline approval integrity regressions; private keys exist only in test memory."""
import base64
from dataclasses import replace
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mavlink_mcp.safety.approval import (
    DOMAIN, MAX_TOKEN_BYTES, ApprovalError, ApprovalExpired, ApprovalRevoked,
    ApprovalToken, InvalidSignature, KeyScopeError, TrustedKey, TrustStore,
    sign, verify,
)
from mavlink_mcp.safety.models import Action, Environment, FrozenRequest, canonical_bytes, parse_canonical_json
from mavlink_mcp.safety.store import SafetyStore, StoreError


@pytest.fixture
def approval(tmp_path):
    now = 1_000_000
    req = FrozenRequest(1, "request", "actor", "vehicle", "gateway", "boot", Environment.MOCK,
        "connection", "home", "policy", Action.FLY_MISSION,
        {"waypoints": [{"latitude_e7": 100, "relative_altitude_mm": 2000}]},
        home_anchor={"position": [1, 2]}, created_at_utc_ms=now - 20_000)
    private = Ed25519PrivateKey.generate()
    key = TrustedKey("key", "operator", private.public_key().public_bytes_raw(),
                     frozenset({"human_operator"}), frozenset({"vehicle"}), frozenset({"fly_mission"}))
    with SafetyStore(tmp_path / "ledger.sqlite3", bootstrap=True, clock=lambda: now) as store:
        store.create_approval(req, nonce="nonce", expires_at_utc_ms=now + 60_000,
                              required_operator_role="human_operator")
        token = sign(req, nonce="nonce", key_id="key", operator_id="operator", private_key=private,
                     issued_at_utc_ms=now, expires_at_utc_ms=now + 60_000)
        yield SimpleNamespace(request=req, private=private, key=key, trust=TrustStore({"key": key}),
                              store=store, token=token, now=now)


def check(a, token=None, **changes):
    options = dict(request=a.request, trust_store=a.trust, store=a.store, now_utc_ms=a.now)
    options.update(changes)
    return verify(a.token if token is None else token, **options)


def claims_of(token):
    return parse_canonical_json(canonical_bytes(token.claims))


def resign(a, claims):
    return ApprovalToken(claims, a.private.sign(DOMAIN + canonical_bytes(claims)))


def test_claims_are_deeply_immutable_and_detached(approval):
    a = approval
    claims = claims_of(a.token)
    token = ApprovalToken(claims, a.token.signature)
    claims["parameters"]["waypoints"][0]["relative_altitude_mm"] = 9000
    claims["home_anchor"]["position"][0] = 9
    assert token.claims["parameters"]["waypoints"][0]["relative_altitude_mm"] == 2000
    assert token.claims["home_anchor"]["position"] == (1, 2)
    with pytest.raises(TypeError): token.claims["nonce"] = "other"
    with pytest.raises(TypeError): token.claims["parameters"]["waypoints"][0]["relative_altitude_mm"] = 1
    with pytest.raises(TypeError): token.claims["home_anchor"]["position"][0] = 3
    assert check(a, token)["nonce"] == "nonce"


def test_nested_mapping_canonical_roundtrip(approval):
    a = approval
    encoded = a.token.encode()
    decoded = ApprovalToken.decode(encoded)
    assert decoded.encode() == encoded
    assert check(a, encoded)["parameters"]["waypoints"][0]["latitude_e7"] == 100
    assert check(a, decoded) == a.token.claims


def test_mutating_request_anchor_after_signing_cannot_change_approved_content(approval):
    a = approval
    # Model construction also freezes/validates content. Deliberately bypass it
    # to ensure verification does not trust a previously supplied digest.
    object.__setattr__(a.request, "home_anchor", {"position": [99, 2]})
    assert a.token.claims["home_anchor"]["position"][0] == 1
    with pytest.raises(ApprovalError, match="digest mismatch"): check(a)


def test_altered_request_with_copied_digest_is_rejected(approval):
    a = approval
    altered = replace(a.request)
    object.__setattr__(altered, "parameters", {"waypoints": [{"relative_altitude_mm": 9999}]})
    with pytest.raises(ApprovalError, match="digest mismatch"): check(a, request=altered)


def test_resigned_altered_request_still_must_equal_stored_payload(approval):
    a = approval
    altered = replace(a.request, parameters={"waypoints": [{"relative_altitude_mm": 9999}]}, request_digest="")
    token = sign(altered, nonce="nonce", key_id="key", operator_id="operator", private_key=a.private,
                 issued_at_utc_ms=a.now, expires_at_utc_ms=a.now + 60_000)
    with pytest.raises(ApprovalError, match="stored request binding"): check(a, token, request=altered)


@pytest.mark.parametrize("field", ["parameters", "created_at_utc_ms", "actor_id"])
def test_exact_stored_payload_not_only_stored_digest_is_checked(approval, field):
    a = approval
    payload = a.store.get_request(a.request.request_id)[0]
    payload[field] = {"changed": 1} if field == "parameters" else (0 if field == "created_at_utc_ms" else "other")
    a.store._conn.execute("UPDATE requests SET request_json=? WHERE request_id=?",
                         (canonical_bytes(payload), a.request.request_id))
    with pytest.raises(ApprovalError, match="stored request binding"): check(a)


@pytest.mark.parametrize("field,value", [
    ("parameters", {"waypoints": [{"relative_altitude_mm": 3000}]}),
    ("vehicle_id", "other"), ("request_digest", "0" * 64),
    ("boot_epoch", "new-boot"), ("connection_epoch", "new-connection"),
    ("home_epoch", "new-home"), ("policy_hash", "new-policy"),
    ("environment", "px4_sitl"), ("action", "arm"),
])
def test_signature_parameter_and_context_mutation_rejected(approval, field, value):
    a = approval
    claims = claims_of(a.token)
    claims[field] = value
    with pytest.raises(ApprovalError): check(a, ApprovalToken(claims, a.token.signature))


def test_nonce_mutation_without_resigning_is_invalid_signature(approval):
    a = approval
    claims = claims_of(a.token)
    claims["nonce"] = "other-nonce"
    with pytest.raises(InvalidSignature): check(a, ApprovalToken(claims, a.token.signature))


def test_signature_bytes_mutation_rejected(approval):
    a = approval
    signature = bytes([a.token.signature[0] ^ 1]) + a.token.signature[1:]
    with pytest.raises(InvalidSignature): check(a, ApprovalToken(a.token.claims, signature))


@pytest.mark.parametrize("field,value", [
    ("approval_schema_version", True), ("approval_schema_version", "1"),
    ("key_id", []), ("key_id", None), ("operator_id", {}), ("nonce", []), ("nonce", ""),
    ("issued_at_utc_ms", True), ("issued_at_utc_ms", "1000000"),
    ("expires_at_utc_ms", False), ("expires_at_utc_ms", "1060000"),
    ("algorithm", "none"),
])
def test_malformed_signed_fields_raise_approval_error(approval, field, value):
    a = approval
    claims = claims_of(a.token)
    claims[field] = value
    with pytest.raises(ApprovalError): check(a, resign(a, claims))


@pytest.mark.parametrize("field", ["nonce", "key_id", "operator_id", "issued_at_utc_ms", "parameters", "request_id"])
def test_missing_signed_fields_raise_approval_error(approval, field):
    claims = claims_of(approval.token)
    del claims[field]
    with pytest.raises(ApprovalError): check(approval, resign(approval, claims))


@pytest.mark.parametrize("value", [None, 3, {}, [], b"token", "", "a.b.c", "!.AA", "é.AA", pytest.param("a" * (MAX_TOKEN_BYTES + 1), id="oversized")])
def test_malformed_wire_tokens_raise_approval_error(value):
    with pytest.raises(ApprovalError): ApprovalToken.decode(value)


def test_duplicate_token_json_and_oversize_in_memory_claims_rejected():
    raw = base64.urlsafe_b64encode(b'{"nonce":"a","nonce":"b"}').decode().rstrip("=")
    signature = base64.urlsafe_b64encode(bytes(64)).decode().rstrip("=")
    with pytest.raises(ApprovalError): ApprovalToken.decode(raw + "." + signature)
    with pytest.raises(ApprovalError): ApprovalToken({"padding": "x" * MAX_TOKEN_BYTES}, bytes(64))
    with pytest.raises(ApprovalError): ApprovalToken({1: "not a string key"}, bytes(64))
    with pytest.raises(ApprovalError): ApprovalToken({}, b"short")


@pytest.mark.parametrize("token", [None, {}, 4, [], object()])
def test_verify_wrong_token_types_raise_approval_error(approval, token):
    with pytest.raises(ApprovalError):
        verify(token, request=approval.request, trust_store=approval.trust,
               store=approval.store, now_utc_ms=approval.now)


@pytest.mark.parametrize("issued_delta,expiry_delta,now_delta", [
    (1, 60_000, 0), (-1, 59_999, 0), (0, 60_000, 60_000),
    (0, 60_001, 0), (0, 0, 0), (0, 59_999, 0),
    (10_000, 70_000, 10_000),
])
def test_expiry_and_issue_window_cannot_be_changed_or_renewed(approval, issued_delta, expiry_delta, now_delta):
    a = approval
    claims = claims_of(a.token)
    claims["issued_at_utc_ms"] = a.now + issued_delta
    claims["expires_at_utc_ms"] = a.now + expiry_delta
    with pytest.raises(ApprovalExpired): check(a, resign(a, claims), now_utc_ms=a.now + now_delta)


def test_later_issue_preserves_original_expiry_and_claim_deadline(approval):
    a = approval
    claims = claims_of(a.token)
    claims["issued_at_utc_ms"] += 10_000
    assert check(a, resign(a, claims), now_utc_ms=a.now + 10_000)["expires_at_utc_ms"] == a.now + 60_000
    a.store.clock = lambda: a.now + 60_000
    with pytest.raises(StoreError):
        a.store.claim_and_intent(request_id=a.request.request_id, nonce="nonce", boot_epoch="boot")


@pytest.mark.parametrize("changes,error", [
    ({"operator_id": "other"}, KeyScopeError),
    ({"roles": frozenset({"observer"})}, KeyScopeError),
    ({"vehicles": frozenset({"other"})}, KeyScopeError),
    ({"actions": frozenset({"arm"})}, KeyScopeError),
    ({"revoked": True}, ApprovalRevoked),
])
def test_key_scopes_are_enforced(approval, changes, error):
    a = approval
    trust = TrustStore({"key": replace(a.key, **changes)})
    with pytest.raises(error): check(a, trust_store=trust)


def test_stored_role_is_enforced(approval):
    a = approval
    a.store._conn.execute("UPDATE approval_nonces SET required_operator_role='supervisor'")
    with pytest.raises(KeyScopeError): check(a)


def test_validly_signed_nonce_must_match_store(approval):
    a = approval
    claims = claims_of(a.token)
    claims["nonce"] = "unrelated-nonce"
    with pytest.raises(ApprovalError, match="nonce is not pending"): check(a, resign(a, claims))


def test_store_errors_are_not_wrapped_as_approval_errors(approval):
    approval.store.close()
    with pytest.raises(StoreError): check(approval)


@pytest.mark.parametrize("changes", [
    {"request": None}, {"nonce": []}, {"operator_id": ""}, {"key_id": {}},
    {"issued_at_utc_ms": True}, {"expires_at_utc_ms": "1060000"}, {"private_key": None},
])
def test_malformed_signing_inputs_raise_approval_error(approval, changes):
    a = approval
    options = dict(request=a.request, nonce="nonce", key_id="key", operator_id="operator",
                   private_key=a.private, issued_at_utc_ms=a.now, expires_at_utc_ms=a.now + 60_000)
    options.update(changes)
    with pytest.raises(ApprovalError): sign(**options)
