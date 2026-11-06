"""Trusted, fail-closed launch composition; never accepts MCP-supplied policy.

SAFEMAV_CONFIG points to a bounded JSON file. Without it, the selected backend
is read-only and has no gateway. Legacy environment guardrail overrides are
not authorization. Kinematic simulation is not physical safety evidence.
"""
from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from mavlink_mcp.adapter import AdapterError, DroneAdapter
from mavlink_mcp.drone import Drone
from mavlink_mcp.guardrails import GuardrailConfig
from .approval import TrustedKey, TrustStore
from .authorization import Principal, Role, TrustedContext
from .config import PolicySnapshot, load_policy
from .gateway import SafetyGateway
from .models import Action, Environment, parse_canonical_json
from .store import SafetyStore, StoreError

MAX_CONFIG_BYTES = 256 * 1024
MAX_PUBLIC_KEY_BYTES = 4096
_BACKEND_ENVIRONMENTS = {"sim": Environment.KINEMATIC, "mavsdk": Environment.PX4_SITL}


class RuntimeConfigError(AdapterError):
    """Invalid or unavailable trusted launch configuration; no fallback."""


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    actor_id: str
    vehicle_id: str
    gateway_id: str
    environment: Environment
    readonly: bool
    policy: PolicySnapshot
    trust_store: TrustStore
    ledger_path: Path
    bootstrap: bool = False


def _object(value: Any, required: set[str], optional: set[str] = frozenset()) -> dict:
    if not isinstance(value, dict) or not required <= value.keys() or value.keys() - required - optional:
        raise RuntimeConfigError("Invalid launch config object: missing or unknown fields")
    return value


def _text(value: Any, *, limit: int = 256) -> str:
    if (not isinstance(value, str) or not value or value != value.strip()
            or len(value) > limit or any(ord(c) < 32 for c in value)):
        raise RuntimeConfigError("Invalid launch config string")
    value.encode("utf-8", errors="strict")
    return value


def _boolean(value: Any) -> bool:
    if type(value) is not bool:
        raise RuntimeConfigError("Launch config flags must be JSON booleans")
    return value


def _path(value: Any, parent: Path) -> Path:
    path = Path(_text(value, limit=4096))
    return (path if path.is_absolute() else parent / path).resolve()


def _read_bounded(path: Path, limit: int) -> bytes:
    with path.open("rb") as source:
        data = source.read(limit + 1)
    if len(data) > limit:
        raise RuntimeConfigError("Launch config or public key exceeds size limit")
    return data


def _scope(value: Any, allowed: set[str] | None = None) -> frozenset[str]:
    # Empty scopes mean unrestricted in TrustStore, so never infer them here.
    if not isinstance(value, list) or not 1 <= len(value) <= 128:
        raise RuntimeConfigError("Trust key scopes must be nonempty bounded lists")
    result = frozenset(_text(item) for item in value)
    if len(result) != len(value) or (allowed is not None and not result <= allowed):
        raise RuntimeConfigError("Duplicate or unknown trust key scope")
    return result


def _public_key(key: dict, parent: Path) -> bytes:
    if ("public_key" in key) == ("public_key_path" in key):
        raise RuntimeConfigError("Specify exactly one public_key or public_key_path")
    if "public_key" in key:
        # Match safemav-operator keygen's unpadded base64url public output.
        encoded = _text(key["public_key"], limit=64)
        raw = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
        if base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=") != encoded:
            raise RuntimeConfigError("Public key must be canonical unpadded base64url")
        public = Ed25519PublicKey.from_public_bytes(raw)
    else:
        raw = _read_bounded(_path(key["public_key_path"], parent), MAX_PUBLIC_KEY_BYTES)
        # Only public-key parsing; PEM private keys are deliberately rejected.
        public = (Ed25519PublicKey.from_public_bytes(raw) if len(raw) == 32
                  else serialization.load_pem_public_key(raw))
        if not isinstance(public, Ed25519PublicKey):
            raise RuntimeConfigError("Only Ed25519 public keys are trusted")
    return public.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def load_runtime_config(path: str | os.PathLike[str]) -> RuntimeConfig:
    """Validate all config and key material before opening a ledger or adapter.

    All identity/policy fields are required. Missing bootstrap means false;
    creating a ledger requires the literal JSON boolean true. Both top-level
    readonly (principal) and policy.readonly are independent denial switches.
    Public keys are inline base64url or raw/PEM files relative to this config.
    """
    try:
        source = Path(path).resolve()
        data = _object(parse_canonical_json(_read_bounded(source, MAX_CONFIG_BYTES)), {
            "actor_id", "vehicle_id", "gateway_id", "environment", "readonly",
            "policy", "trust_keys", "ledger_path",
        }, {"bootstrap"})
        actor, vehicle, gateway = (_text(data[name]) for name in ("actor_id", "vehicle_id", "gateway_id"))
        environment = Environment(_text(data["environment"]))
        if environment not in _BACKEND_ENVIRONMENTS.values():
            raise RuntimeConfigError("Mock environments are not allowed in server launch config")
        readonly = _boolean(data["readonly"])
        bootstrap = _boolean(data.get("bootstrap", False))
        if not isinstance(data["policy"], dict):
            raise RuntimeConfigError("Policy must be an object")
        # No implicit mock profile; do not weaken load_policy's readonly default.
        policy = load_policy({"environment": environment.value, **data["policy"]})
        if policy.config.environment is not environment:
            raise RuntimeConfigError("Launch and policy environments differ")
        entries = data["trust_keys"]
        if not isinstance(entries, list) or len(entries) > 64:
            raise RuntimeConfigError("trust_keys must be a bounded list")
        keys = {}
        for item in entries:
            key = _object(item, {"key_id", "operator_id", "roles", "vehicles", "actions"},
                          {"public_key", "public_key_path", "revoked"})
            key_id = _text(key["key_id"])
            if key_id in keys:
                raise RuntimeConfigError("Duplicate trust key ID")
            keys[key_id] = TrustedKey(
                key_id, _text(key["operator_id"]), _public_key(key, source.parent),
                _scope(key["roles"], {role.value for role in Role}),
                _scope(key["vehicles"]), _scope(key["actions"], {action.value for action in Action}),
                _boolean(key.get("revoked", False)),
            )
        return RuntimeConfig(actor, vehicle, gateway, environment, readonly, policy,
                             TrustStore(keys), _path(data["ledger_path"], source.parent), bootstrap)
    except RuntimeConfigError:
        raise
    except (OSError, ValueError, TypeError, RecursionError, OverflowError) as exc:
        raise RuntimeConfigError("Invalid or unreadable SAFEMAV_CONFIG; no fallback") from exc


def compose_drone(adapter: DroneAdapter, config: RuntimeConfig | None = None) -> Drone:
    """Bind one adapter, immutable policy, principal, trust store and ledger."""
    if adapter.name not in _BACKEND_ENVIRONMENTS:
        raise RuntimeConfigError("Server backend must be sim or mavsdk; no mock fallback")
    if config is None:
        return Drone(adapter, guardrails=GuardrailConfig(readonly=True))
    if config.environment is not _BACKEND_ENVIRONMENTS[adapter.name]:
        raise RuntimeConfigError("Backend and trusted launch environment differ")
    store = None
    try:
        store = SafetyStore(config.ledger_path, bootstrap=config.bootstrap)
        context = TrustedContext(
            Principal(config.actor_id, Role.FLIGHT_AGENT, config.vehicle_id, config.readonly),
            config.environment.value, config.gateway_id, store.boot_epoch,
        )
        gateway = SafetyGateway(adapter=adapter, context=context, store=store,
                                policy=config.policy, approval_store=config.trust_store)
        guardrails = GuardrailConfig(
            readonly=config.readonly or config.policy.config.readonly,
            max_altitude_m=config.policy.config.max_altitude_m,
            geofence_radius_m=config.policy.config.max_radius_m,
        )
        return Drone(adapter, guardrails=guardrails, safety_gateway=gateway)
    except BaseException as exc:
        if store is not None:
            store.close()
        if isinstance(exc, (StoreError, ValueError)):
            raise RuntimeConfigError("Trusted safety runtime unavailable; no fallback") from exc
        raise


def create_drone() -> Drone:
    """Read trusted launch settings once, without connecting to any vehicle."""
    config_path = os.environ.get("SAFEMAV_CONFIG")
    if config_path is not None and not config_path.strip():
        raise RuntimeConfigError("SAFEMAV_CONFIG must name a launch config file")
    config = load_runtime_config(config_path) if config_path is not None else None
    backend = os.environ.get("MAVLINK_MCP_BACKEND", "mavsdk").strip().lower()
    if backend not in _BACKEND_ENVIRONMENTS:
        raise RuntimeConfigError(f"Unknown MAVLINK_MCP_BACKEND {backend!r}; use sim or mavsdk. No fallback connection was attempted.")
    if config is not None and config.environment is not _BACKEND_ENVIRONMENTS[backend]:
        raise RuntimeConfigError("Backend and trusted launch environment differ")
    if backend == "sim":
        from mavlink_mcp.adapters.sim_adapter import SimAdapter
        adapter = SimAdapter()
    else:
        from mavlink_mcp.adapters.mavsdk_adapter import MavsdkAdapter
        adapter = MavsdkAdapter()
    return compose_drone(adapter, config)
