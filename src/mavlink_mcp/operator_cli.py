"""Human-operated approval CLI; never an MCP approval surface."""
from __future__ import annotations
import argparse, json, sys, base64, os, time
from .safety.models import FrozenRequest, Action, Environment, parse_canonical_json
from pathlib import Path

def _read_once(path):
    try:
        with Path(path).open("rb") as source:
            data = source.read(256 * 1024 + 1)
    except OSError as exc:
        raise SystemExit("cannot read request") from exc
    if len(data) > 256 * 1024: raise SystemExit("request too large")
    try:
        obj = parse_canonical_json(data)
        if not isinstance(obj, dict) or not obj: raise ValueError("empty request")
        return obj
    except (ValueError, TypeError, RecursionError) as exc:
        raise SystemExit("invalid request JSON") from exc

def main(argv=None):
    parser=argparse.ArgumentParser(prog="safemav-operator")
    sub=parser.add_subparsers(dest="command",required=True)
    keygen=sub.add_parser("keygen"); keygen.add_argument("--output",required=True); keygen.add_argument("--key-id",required=True); keygen.add_argument("--operator-id",required=True)
    review=sub.add_parser("review"); review.add_argument("request")
    approve=sub.add_parser("approve", description="Sign a reviewed request at an interactive terminal; never renew its stored expiry.", epilog="Example: safemav-operator approve review.json --key operator.key --key-id k1 --operator-id alice --output approval.token. Review JSON must contain the public FrozenRequest fields plus nonce, issued_at_utc_ms and expires_at_utc_ms. Confirm the exact nonempty request ID. RPC acceptance is not flight completion.")
    approve.add_argument("request", help="gateway review JSON (maximum 256 KiB)")
    approve.add_argument("--key",required=True, help="local raw Ed25519 private key")
    approve.add_argument("--key-id",required=True)
    approve.add_argument("--operator-id", help="required signing operator identity; never taken from request JSON")
    approve.add_argument("--output",required=True, help="approval token output path")
    verify=sub.add_parser("verify"); verify.add_argument("token")
    args=parser.parse_args(argv)
    if args.command=="keygen":
        if os.path.exists(args.output): raise SystemExit("refusing to overwrite key")
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
            from cryptography.hazmat.primitives import serialization
        except ImportError: raise SystemExit("cryptography is required")
        key=Ed25519PrivateKey.generate(); Path(args.output).write_bytes(key.private_bytes(serialization.Encoding.Raw,serialization.PrivateFormat.Raw,serialization.NoEncryption()))
        os.chmod(args.output,0o600) if os.name != "nt" else None
        pub=key.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw)
        print(json.dumps({"key_id":args.key_id,"operator_id":args.operator_id,"public_key":base64.urlsafe_b64encode(pub).decode().rstrip("=")},sort_keys=True)); return 0
    if args.command=="review":
        obj=_read_once(args.request); print(json.dumps(obj,sort_keys=True,indent=2)); return 0
    if args.command=="approve":
        obj=_read_once(args.request); print(json.dumps(obj,sort_keys=True,indent=2))
        if not sys.stdin.isatty(): raise SystemExit("interactive confirmation required")
        if not args.operator_id or not args.operator_id.strip():
            parser.error("approve requires --operator-id")
        expected = obj.get("request_id")
        if not isinstance(expected, str) or not expected.strip():
            raise SystemExit("request must include a nonempty request ID")
        from .safety.approval import sign, _request_payload
        required = {"schema_version","request_id","actor_id","vehicle_id","gateway_id","boot_epoch","environment","connection_epoch","home_epoch","policy_hash","action","parameters","created_at_utc_ms","request_digest","nonce","issued_at_utc_ms","expires_at_utc_ms"}
        if not required.issubset(obj): raise SystemExit("request record is incomplete")
        try:
            request = FrozenRequest(schema_version=obj["schema_version"], request_id=expected, actor_id=obj["actor_id"], vehicle_id=obj["vehicle_id"], gateway_id=obj["gateway_id"], boot_epoch=obj["boot_epoch"], environment=Environment(obj["environment"]), connection_epoch=obj["connection_epoch"], home_epoch=obj["home_epoch"], policy_hash=obj["policy_hash"], action=Action(obj["action"]), parameters=obj["parameters"], reference_frame=obj.get("reference_frame", "HOME_RELATIVE"), home_anchor=obj.get("home_anchor"), mission_digest=obj.get("mission_digest"), created_at_utc_ms=obj["created_at_utc_ms"], request_digest=obj["request_digest"])
            if not isinstance(obj["request_digest"], str) or not obj["request_digest"]:
                raise ValueError("missing request digest")
            _request_payload(request)
            nonce, expires, requested = obj["nonce"], obj["expires_at_utc_ms"], obj["issued_at_utc_ms"]
            if not isinstance(nonce, str) or not nonce.strip() or not args.key_id.strip():
                raise ValueError("invalid signing identity or nonce")
            if type(expires) is not int or type(requested) is not int or requested < 0:
                raise ValueError("invalid approval times")
        except (KeyError, ValueError, TypeError, RecursionError) as exc:
            raise SystemExit("invalid approval request") from exc
        if input("Type the request ID to confirm: ") != expected:
            raise SystemExit("confirmation cancelled")
        # Timestamp after the human wait. Never replace the stored expiry with
        # a fresh TTL, even if confirmation took longer than the approval window.
        issued = int(time.time() * 1000)
        if not requested <= issued < expires or expires - issued > 60_000:
            raise SystemExit("approval request expired or outside its time window")
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        except ImportError: raise SystemExit("cryptography is required")
        try:
            with Path(args.key).open("rb") as source:
                raw = source.read(33)
            key = Ed25519PrivateKey.from_private_bytes(raw)
            token=sign(request, nonce=nonce, key_id=args.key_id, operator_id=args.operator_id, private_key=key, issued_at_utc_ms=issued, expires_at_utc_ms=expires)
            Path(args.output).write_text(token.encode(),encoding="ascii")
        except (OSError, ValueError, TypeError) as exc:
            raise SystemExit("cannot sign or write approval") from exc
        return 0
    if args.command=="verify":
        from .safety.approval import ApprovalToken
        ApprovalToken.decode(args.token); print("Token syntax is valid; trust-store verification is required."); return 0
    return 2
if __name__=="__main__": raise SystemExit(main())
