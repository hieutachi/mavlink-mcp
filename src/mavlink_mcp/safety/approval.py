"""Fixed Ed25519 approval tokens and protected trust-store verification."""
from __future__ import annotations
import base64, time
from dataclasses import dataclass, fields
from typing import Any, Mapping
from .models import Action, Environment, FrozenRequest, _freeze, canonical_bytes, parse_canonical_json, request_digest
from .store import SafetyStore

MAX_TOKEN_BYTES = 256 * 1024
DOMAIN=b"SafeMAV-Approval-v1\0"
class ApprovalError(ValueError): pass
class InvalidSignature(ApprovalError): pass
class ApprovalExpired(ApprovalError): pass
class ApprovalRevoked(ApprovalError): pass
class KeyScopeError(ApprovalError): pass
class TrustStoreError(ApprovalError): pass

@dataclass(frozen=True, slots=True)
class TrustedKey:
    key_id: str; operator_id: str; public_key: bytes; roles: frozenset[str]; vehicles: frozenset[str] = frozenset(); actions: frozenset[str] = frozenset(); revoked: bool = False

class TrustStore:
    def __init__(self, keys: Mapping[str, TrustedKey]):
        if not isinstance(keys, Mapping): raise TrustStoreError("keys must be a mapping")
        self._keys = {}
        for key_id, key in keys.items():
            if not isinstance(key, TrustedKey) or not _text(key_id) or key.key_id != key_id or not _text(key.operator_id):
                raise TrustStoreError("invalid key identity")
            if not isinstance(key.public_key, bytes) or len(key.public_key) != 32 or type(key.revoked) is not bool:
                raise TrustStoreError("invalid Ed25519 key")
            for scope in (key.roles, key.vehicles, key.actions):
                if not isinstance(scope, (set, frozenset)) or not all(_text(v) for v in scope):
                    raise TrustStoreError("invalid key scope")
            self._keys[key_id] = TrustedKey(key.key_id, key.operator_id, key.public_key,
                frozenset(key.roles), frozenset(key.vehicles), frozenset(key.actions), key.revoked)

    def lookup(self, key_id):
        if not _text(key_id): raise TrustStoreError("invalid key id")
        key = self._keys.get(key_id)
        if key is None: raise TrustStoreError("unknown key")
        if key.revoked: raise ApprovalRevoked("key is revoked")
        return key


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _json_types(value):
    """Reject non-string keys and non-JSON containers before canonicalization."""
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str): raise ApprovalError("claim keys must be strings")
            _json_types(child)
    elif isinstance(value, (tuple, list)):
        for child in value: _json_types(child)
    elif value is not None and not isinstance(value, (str, int, bool)):
        raise ApprovalError("unsupported claim value")


@dataclass(frozen=True, slots=True)
class ApprovalToken:
    claims: Mapping[str,Any]
    signature: bytes

    def __post_init__(self):
        try:
            if not isinstance(self.claims, Mapping) or not isinstance(self.signature, bytes) or len(self.signature) != 64:
                raise ApprovalError("malformed approval token")
            _json_types(self.claims)
            object.__setattr__(self, "claims", _freeze(self.claims))
            if len(self.encode()) > MAX_TOKEN_BYTES:
                raise ApprovalError("approval token too large")
        except (TypeError, ValueError, RecursionError, OverflowError) as exc:
            raise ApprovalError("malformed approval token") from exc

    def encode(self) -> str:
        return base64.urlsafe_b64encode(canonical_bytes(self.claims)).decode().rstrip("=")+"."+base64.urlsafe_b64encode(self.signature).decode().rstrip("=")

    @classmethod
    def decode(cls, value: str):
        try:
            if not isinstance(value, str) or len(value) > MAX_TOKEN_BYTES or not value.isascii():
                raise ApprovalError("invalid token size or encoding")
            parts = value.split(".")
            if len(parts) != 2 or not all(parts): raise ApprovalError("malformed token")
            decoded = []
            for part in parts:
                if any(ch not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for ch in part):
                    raise ApprovalError("invalid base64url")
                raw = base64.b64decode(part + "=" * (-len(part) % 4), altchars=b"-_", validate=True)
                if base64.urlsafe_b64encode(raw).decode().rstrip("=") != part:
                    raise ApprovalError("noncanonical base64url")
                decoded.append(raw)
            return cls(parse_canonical_json(decoded[0]), decoded[1])
        except (TypeError, ValueError, RecursionError, OverflowError) as exc:
            raise ApprovalError("malformed approval token") from exc

def _claims(request: FrozenRequest, *, key_id, operator_id, nonce, issued_at_utc_ms, expires_at_utc_ms):
    return {"approval_schema_version":1,"key_id":key_id,"operator_id":operator_id,"request_id":request.request_id,"request_digest":request.request_digest,"actor_id":request.actor_id,"vehicle_id":request.vehicle_id,"gateway_id":request.gateway_id,"boot_epoch":request.boot_epoch,"environment":request.environment,"connection_epoch":request.connection_epoch,"home_epoch":request.home_epoch,"policy_hash":request.policy_hash,"action":request.action,"parameters":request.parameters,"reference_frame":request.reference_frame,"home_anchor":request.home_anchor,"mission_digest":request.mission_digest,"nonce":nonce,"issued_at_utc_ms":issued_at_utc_ms,"expires_at_utc_ms":expires_at_utc_ms}

def sign(request: FrozenRequest, *, nonce: str, key_id: str, operator_id: str, private_key, issued_at_utc_ms: int, expires_at_utc_ms: int) -> ApprovalToken:
    try:
        _request_payload(request)
        if not all(_text(value) for value in (nonce, key_id, operator_id)):
            raise ApprovalError("invalid signing identity or nonce")
        if type(issued_at_utc_ms) is not int or type(expires_at_utc_ms) is not int or not 0 <= issued_at_utc_ms < expires_at_utc_ms:
            raise ApprovalError("invalid signing times")
        claims=_claims(request,key_id=key_id,operator_id=operator_id,nonce=nonce,issued_at_utc_ms=issued_at_utc_ms,expires_at_utc_ms=expires_at_utc_ms)
        return ApprovalToken(claims,private_key.sign(DOMAIN+canonical_bytes(claims)))
    except ApprovalError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError, RecursionError, OverflowError) as exc:
        raise ApprovalError("malformed signing input") from exc

def _request_payload(request):
    if not isinstance(request, FrozenRequest): raise ApprovalError("invalid request")
    if type(request.schema_version) is not int or request.schema_version != 1:
        raise ApprovalError("invalid request schema")
    if type(request.created_at_utc_ms) is not int or request.created_at_utc_ms < 0:
        raise ApprovalError("invalid request time")
    for name in ("request_id", "actor_id", "vehicle_id", "gateway_id", "boot_epoch",
                 "connection_epoch", "home_epoch", "policy_hash", "reference_frame", "request_digest"):
        if not _text(getattr(request, name)): raise ApprovalError("invalid request field: " + name)
    if not isinstance(request.parameters, Mapping) or (request.home_anchor is not None and not isinstance(request.home_anchor, Mapping)):
        raise ApprovalError("invalid request parameters or anchor")
    if request.mission_digest is not None and not _text(request.mission_digest):
        raise ApprovalError("invalid mission digest")
    Action(request.action)
    Environment(request.environment)
    payload = {f.name: getattr(request, f.name) for f in fields(FrozenRequest)}
    _json_types(payload)
    if request_digest(request) != request.request_digest:
        raise ApprovalError("request digest mismatch")
    return payload


def verify(token: ApprovalToken | str, *, request: FrozenRequest, trust_store: TrustStore, store: SafetyStore, now_utc_ms: int | None=None, max_ttl_ms=60_000, allowed_role="human_operator"):
    # Keep untrusted parsing separate from store I/O: StoreError must propagate.
    try:
        token = ApprovalToken.decode(token) if isinstance(token, str) else token
        if not isinstance(token, ApprovalToken): raise ApprovalError("invalid approval token")
        payload = _request_payload(request)
        c = token.claims
        now = now_utc_ms if now_utc_ms is not None else int(time.time() * 1000)
        if type(now) is not int or now < 0 or type(max_ttl_ms) is not int or max_ttl_ms <= 0:
            raise ApprovalExpired("invalid verification time or TTL")
        if not _text(allowed_role): raise KeyScopeError("invalid required role")
        if type(c.get("approval_schema_version")) is not int or c.get("approval_schema_version") != 1:
            raise ApprovalError("invalid approval schema")
        for name in ("key_id", "operator_id", "nonce"):
            if not _text(c.get(name)): raise ApprovalError("invalid approval field: " + name)
        issued, expires = c.get("issued_at_utc_ms"), c.get("expires_at_utc_ms")
        if type(issued) is not int or type(expires) is not int:
            raise ApprovalExpired("invalid approval times")
        if issued < 0 or issued > now or expires <= issued or expires - issued > max_ttl_ms or now >= expires:
            raise ApprovalExpired("approval expired")
        expected = _claims(request, key_id=c["key_id"], operator_id=c["operator_id"], nonce=c["nonce"], issued_at_utc_ms=issued, expires_at_utc_ms=expires)
        # Canonical bytes compare mappings/tuples to decoded JSON mappings/lists
        # without permitting Python's bool == int equivalence.
        if canonical_bytes(c) != canonical_bytes(expected): raise ApprovalError("claims mismatch")
        key = trust_store.lookup(c["key_id"])
        if key.operator_id != c["operator_id"] or allowed_role not in key.roles: raise KeyScopeError("operator scope mismatch")
        if key.vehicles and request.vehicle_id not in key.vehicles: raise KeyScopeError("vehicle scope mismatch")
        if key.actions and str(request.action) not in key.actions: raise KeyScopeError("action scope mismatch")
    except ApprovalError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError, RecursionError, OverflowError) as exc:
        raise ApprovalError("malformed approval or request") from exc
    # Only Ed25519; no caller-selected algorithm.
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        Ed25519PublicKey.from_public_bytes(key.public_key).verify(token.signature, DOMAIN + canonical_bytes(c))
    except Exception as exc:
        raise InvalidSignature("invalid signature") from exc
    record = store.get_request(request.request_id)
    if not record or record[2] != "AWAITING_APPROVAL": raise ApprovalError("request is not awaiting approval")
    try:
        if record[1] != request.request_digest or canonical_bytes(record[0]) != canonical_bytes(payload):
            raise ApprovalError("stored request binding mismatch")
    except (TypeError, ValueError, RecursionError) as exc:
        raise ApprovalError("stored request binding mismatch") from exc
    approval = store.get_approval_record(request.request_id)
    if not approval or approval.nonce != c["nonce"] or approval.nonce_state != "PENDING":
        raise ApprovalError("nonce is not pending for request")
    if issued < approval.issued_at_utc_ms or now < approval.issued_at_utc_ms or now >= approval.expires_at_utc_ms:
        raise ApprovalExpired("nonce outside approval window")
    if expires != approval.expires_at_utc_ms:
        raise ApprovalExpired("signed expiry differs from stored deadline")
    if approval.required_operator_role not in key.roles:
        raise KeyScopeError("stored operator role mismatch")
    return c
