"""Immutable normalized SafeMAV v1 data and canonical digest primitives."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Mapping
from types import MappingProxyType


class _ValueEnum(str, Enum):
    def __str__(self) -> str: return self.value

class Action(_ValueEnum):
    ARM="arm"; DISARM="disarm"; TAKEOFF="takeoff"; LAND="land"; HOLD="hold"; RETURN_TO_LAUNCH="return_to_launch"; GOTO="goto"; FLY_MISSION="fly_mission"
class Environment(_ValueEnum): MOCK="mock"; KINEMATIC="kinematic"; PX4_SITL="px4_sitl"
class PolicyDecision(_ValueEnum): ALLOW="ALLOW"; DENY="DENY"; REQUIRE_APPROVAL="REQUIRE_APPROVAL"; SAFE_STATE="SAFE_STATE"
class RiskLabel(_ValueEnum): READ="LOW"; MEDIUM="MEDIUM"; HIGH="HIGH"; CRITICAL="CRITICAL"
class ReasonCode(_ValueEnum):
    ACKNOWLEDGED = "ACKNOWLEDGED"
    GATEWAY_UNAVAILABLE = "GATEWAY_UNAVAILABLE"
    UNAUTHORIZED="UNAUTHORIZED"; READONLY="READONLY"; APPROVAL_REQUIRED="APPROVAL_REQUIRED"; APPROVAL_INVALID="APPROVAL_INVALID"; APPROVAL_EXPIRED="APPROVAL_EXPIRED"; APPROVAL_REPLAY="APPROVAL_REPLAY"; PARAMETER_MISMATCH="PARAMETER_MISMATCH"; POLICY_CHANGED="POLICY_CHANGED"; VEHICLE_CHANGED="VEHICLE_CHANGED"; TELEMETRY_STALE="TELEMETRY_STALE"; TELEMETRY_INCOMPLETE="TELEMETRY_INCOMPLETE"; UNSAFE_STATE="UNSAFE_STATE"; OUTSIDE_GEOFENCE="OUTSIDE_GEOFENCE"; ALTITUDE_LIMIT="ALTITUDE_LIMIT"; BUSY="BUSY"; AUDIT_UNAVAILABLE="AUDIT_UNAVAILABLE"; MONITOR_UNHEALTHY="MONITOR_UNHEALTHY"; EXECUTION_UNKNOWN="EXECUTION_UNKNOWN"; INVALID_ARGUMENT="INVALID_ARGUMENT"

class ContractError(ValueError): pass


def _number(value: Any, name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ContractError(f"{name} must be a number")
    try: result = Decimal(str(value))
    except (InvalidOperation, ValueError): raise ContractError(f"{name} must be numeric")
    if not result.is_finite(): raise ContractError(f"{name} must be finite")
    return result

def _scaled(value: Any, name: str, scale: int, low: Decimal, high: Decimal, positive=False) -> int:
    d = _number(value, name)
    if d < low or d > high or (positive and d <= 0): raise ContractError(f"{name} is out of range")
    scaled = d * scale
    if scaled != scaled.to_integral_value(): raise ContractError(f"{name} has excessive precision")
    return int(scaled)

def _reject_unknown(data: Mapping[str, Any], allowed: set[str]) -> None:
    unknown = set(data) - allowed
    if unknown: raise ContractError(f"unknown fields: {sorted(unknown)!r}")

@dataclass(frozen=True, slots=True)
class Waypoint:
    latitude_e7: int
    longitude_e7: int
    relative_altitude_mm: int
    speed_mm_s: int | None = None
    yaw_mdeg: int = 0

    def __post_init__(self):
        for value, low, high in ((self.latitude_e7, -900000000, 900000000),
                                 (self.longitude_e7, -1800000000, 1800000000),
                                 (self.relative_altitude_mm, 1, 1000000000),
                                 (self.yaw_mdeg, 0, 359999)):
            if type(value) is not int or not low <= value <= high:
                raise ContractError("invalid canonical waypoint")
        if self.speed_mm_s is not None and (type(self.speed_mm_s) is not int or not 1 <= self.speed_mm_s <= 100000000):
            raise ContractError("invalid canonical speed")

    @classmethod
    def normalize(cls, value: Mapping[str, Any] | "Waypoint") -> "Waypoint":
        from mavlink_mcp.capabilities import Waypoint as PublicWaypoint
        if isinstance(value, cls):
            return cls(value.latitude_e7, value.longitude_e7, value.relative_altitude_mm, value.speed_mm_s, value.yaw_mdeg)
        if isinstance(value, PublicWaypoint):
            value = {"latitude_deg": value.latitude_deg, "longitude_deg": value.longitude_deg,
                     "relative_altitude_m": value.relative_altitude_m, "speed_m_s": value.speed_m_s}
        if not isinstance(value, Mapping): raise ContractError("waypoint must be an object")
        _reject_unknown(value, {"latitude_deg","longitude_deg","relative_altitude_m","speed_m_s","yaw_deg"})
        if "latitude_deg" not in value or "longitude_deg" not in value or "relative_altitude_m" not in value: raise ContractError("waypoint missing required field")
        speed = None if value.get("speed_m_s") is None else _scaled(value["speed_m_s"], "speed_m_s", 1000, Decimal("0"), Decimal("100000"), True)
        yaw = _scaled(value.get("yaw_deg", 0), "yaw_deg", 1000, Decimal("0"), Decimal("359.999"))
        return cls(_scaled(value["latitude_deg"], "latitude_deg", 10_000_000, Decimal("-90"), Decimal("90")), _scaled(value["longitude_deg"], "longitude_deg", 10_000_000, Decimal("-180"), Decimal("180")), _scaled(value["relative_altitude_m"], "relative_altitude_m", 1000, Decimal("0"), Decimal("1000000"), True), speed, yaw)

    def canonical(self) -> dict[str, Any]:
        result = {"latitude_e7": self.latitude_e7, "longitude_e7": self.longitude_e7, "relative_altitude_mm": self.relative_altitude_mm, "speed_mm_s": self.speed_mm_s, "yaw_mdeg": self.yaw_mdeg}
        return result

@dataclass(frozen=True, slots=True)
class Mission:
    waypoints: tuple[Waypoint, ...]
    def __post_init__(self):
        if not self.waypoints or len(self.waypoints) > 1000: raise ContractError("mission must contain 1 to 1000 waypoints")
        object.__setattr__(self, "waypoints", tuple(Waypoint.normalize(w) for w in self.waypoints))
    def canonical(self): return {"waypoints": [w.canonical() for w in self.waypoints]}


def parse_canonical_json(data: str | bytes) -> Any:
    """Parse bounded canonical JSON while rejecting duplicate object keys."""
    def pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result: raise ContractError(f"duplicate JSON key: {key!r}")
            result[key] = value
        return result
    try:
        return json.loads(data, object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(ContractError("nonfinite JSON number")))
    except ContractError: raise
    except (TypeError, ValueError, json.JSONDecodeError) as exc: raise ContractError("invalid JSON") from exc

def canonical_bytes(value: Any) -> bytes:
    def convert(v):
        if isinstance(v, Enum): return v.value
        if isinstance(v, (str, int)) or v is None or isinstance(v, bool): return v
        if isinstance(v, Mapping): return {str(k): convert(x) for k, x in v.items()}
        if isinstance(v, (tuple, list)): return [convert(x) for x in v]
        raise ContractError(f"unsupported canonical value: {type(v).__name__}")
    try: return json.dumps(convert(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc: raise ContractError("value is not canonicalizable") from exc

def sha256_digest(value: Any) -> str: return hashlib.sha256(canonical_bytes(value)).hexdigest()
def mission_digest(mission: Mission | tuple[Waypoint, ...] | list[Mapping[str, Any]]) -> str:
    m = mission if isinstance(mission, Mission) else Mission(tuple(Waypoint.normalize(w) for w in mission))
    return sha256_digest(m.canonical())

@dataclass(frozen=True, slots=True)
class FrozenRequest:
    schema_version: int
    request_id: str
    actor_id: str
    vehicle_id: str
    gateway_id: str
    boot_epoch: str
    environment: Environment
    connection_epoch: str
    home_epoch: str
    policy_hash: str
    action: Action
    parameters: Mapping[str, Any]
    reference_frame: str = "HOME_RELATIVE"
    home_anchor: Mapping[str, Any] | None = None
    mission_digest: str | None = None
    created_at_utc_ms: int = 0
    request_digest: str = field(default="")
    def __post_init__(self):
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ContractError("unsupported schema version")
        if not isinstance(self.action, Action) or not isinstance(self.environment, Environment):
            raise ContractError("request action/environment must be typed")
        for name in ("request_id", "actor_id", "vehicle_id", "gateway_id", "boot_epoch", "connection_epoch", "home_epoch", "policy_hash"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ContractError(f"invalid {name}")
        object.__setattr__(self, "parameters", _freeze(self.parameters))
        object.__setattr__(self, "home_anchor", _freeze(self.home_anchor))
        digest = request_digest(self)
        if self.request_digest and self.request_digest != digest:
            raise ContractError("request digest does not match content")
        object.__setattr__(self, "request_digest", digest)

def _freeze(v):
    if isinstance(v, Mapping): return MappingProxyType({k: _freeze(x) for k, x in v.items()})
    if isinstance(v, list): return tuple(_freeze(x) for x in v)
    if isinstance(v, tuple): return tuple(_freeze(x) for x in v)
    return v

def request_digest(request: FrozenRequest) -> str:
    return sha256_digest({"schema_version":request.schema_version,"request_id":request.request_id,"actor_id":request.actor_id,"vehicle_id":request.vehicle_id,"gateway_id":request.gateway_id,"boot_epoch":request.boot_epoch,"environment":request.environment,"connection_epoch":request.connection_epoch,"home_epoch":request.home_epoch,"policy_hash":request.policy_hash,"action":request.action,"parameters":request.parameters,"reference_frame":request.reference_frame,"home_anchor":request.home_anchor,"mission_digest":request.mission_digest,"created_at_utc_ms":request.created_at_utc_ms})

def normalize_parameters(action: Action, parameters: Mapping[str, Any]) -> Mapping[str, Any]:
    """Strict public input -> immutable canonical units, before any await."""
    if not isinstance(parameters, Mapping):
        raise ContractError("parameters must be an object")
    if action is Action.TAKEOFF:
        _reject_unknown(parameters, {"altitude_m"})
        if "altitude_m" not in parameters:
            raise ContractError("altitude_m is required")
        return _freeze({"altitude_mm": _scaled(parameters["altitude_m"], "altitude_m", 1000, Decimal(0), Decimal(1000000), True)})
    if action is Action.GOTO:
        _reject_unknown(parameters, {"latitude_deg", "longitude_deg", "relative_altitude_m", "yaw_deg"})
        wp = Waypoint.normalize(parameters)
        return _freeze(wp.canonical())
    if action is Action.FLY_MISSION:
        _reject_unknown(parameters, {"waypoints"})
        raw = parameters.get("waypoints")
        if not isinstance(raw, (list, tuple)) or not 1 <= len(raw) <= 1000:
            raise ContractError("mission requires 1 to 1000 waypoints")
        points = []
        for item in raw:
            wp = Waypoint.normalize(item)
            # Pin the MAVSDK default speed in the signed plan.
            point = wp.canonical()
            point["speed_mm_s"] = point["speed_mm_s"] if point["speed_mm_s"] is not None else 5000
            if point["yaw_mdeg"] != 0:
                raise ContractError("mission yaw is not supported by this adapter")
            points.append(point)
        return _freeze({"waypoints": points})
    _reject_unknown(parameters, set())
    return _freeze({})


def request_from_dict(value: Mapping[str, Any]) -> FrozenRequest:
    data = dict(value)
    data["action"] = Action(data["action"])
    data["environment"] = Environment(data["environment"])
    return FrozenRequest(**data)


def request_to_dict(request: FrozenRequest) -> dict[str, Any]:
    names = request.__dataclass_fields__
    return parse_canonical_json(canonical_bytes({name: getattr(request, name) for name in names}))


@dataclass(frozen=True, slots=True)
class PolicyResult:
    decision: PolicyDecision
    reason_code: ReasonCode
    risk: RiskLabel
    explanation: str = ""
