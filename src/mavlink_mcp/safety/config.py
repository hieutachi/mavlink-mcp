"""Strict immutable policy configuration and profile snapshots."""
from __future__ import annotations
from dataclasses import asdict, dataclass
from typing import Any, Mapping
from .models import ContractError, Environment, sha256_digest


@dataclass(frozen=True, slots=True)
class PolicyConfig:
    environment: Environment = Environment.MOCK
    max_altitude_m: int = 50
    max_radius_m: int = 200
    max_speed_mm_s: int = 5000
    min_battery_percent: int = 30
    readonly: bool = True
    max_age_ms: int = 1000
    max_skew_ms: int = 500
    numerical_margin_mm: int = 50
    min_satellites: int = 10

    def __post_init__(self):
        if not isinstance(self.environment, Environment) or type(self.readonly) is not bool:
            raise ContractError("invalid policy environment/readonly")
        for name, low, high in (
            ("max_altitude_m", 1, 1000000), ("max_radius_m", 1, 1000000),
            ("max_speed_mm_s", 1, 100000), ("min_battery_percent", 0, 100),
            ("max_age_ms", 1, 60000), ("max_skew_ms", 0, 60000),
            ("numerical_margin_mm", 1, 1000), ("min_satellites", 1, 100),
        ):
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise ContractError(f"invalid {name}")

    def canonical(self):
        return asdict(self) | {"environment": self.environment.value}


@dataclass(frozen=True, slots=True)
class PolicySnapshot:
    config: PolicyConfig
    version: str = "1"
    policy_hash: str = ""

    def __post_init__(self):
        digest = sha256_digest({"version": self.version, "config": self.config.canonical()})
        if self.policy_hash and self.policy_hash != digest:
            raise ContractError("policy hash mismatch")
        object.__setattr__(self, "policy_hash", digest)


def load_policy(data: Mapping[str, Any]) -> PolicySnapshot:
    if not isinstance(data, Mapping):
        raise ContractError("policy must be an object")
    allowed = set(PolicyConfig.__dataclass_fields__) | {"version"}
    if set(data) - allowed:
        raise ContractError(f"unknown policy fields: {sorted(set(data) - allowed)}")
    values = dict(data)
    version = values.pop("version", "1")
    if not isinstance(version, str) or not version:
        raise ContractError("policy version must be a nonempty string")
    try:
        values["environment"] = Environment(values.get("environment", "mock"))
    except (ValueError, TypeError) as exc:
        raise ContractError("invalid environment") from exc
    return PolicySnapshot(PolicyConfig(**values), version)
