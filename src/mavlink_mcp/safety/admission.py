"""Pure observation admission and deterministic adapter plan translation."""
from __future__ import annotations
import math
import struct
from types import MappingProxyType
from mavlink_mcp.adapter import LeaseError
from mavlink_mcp.capabilities import Waypoint, canonical_home_anchor
from mavlink_mcp.guardrails import haversine_m
from .models import Action, Environment, PolicyDecision, ReasonCode
from .monitor import MonitorConfig, MonitorState, assess_snapshot, valid_observation_value
from .policy import evaluate

SINKS = {
    Action.ARM: ("arm",), Action.DISARM: ("disarm",),
    Action.TAKEOFF: ("set_takeoff_altitude", "takeoff"), Action.LAND: ("land",),
    Action.HOLD: ("hold",), Action.RETURN_TO_LAUNCH: ("return_to_launch",),
    Action.GOTO: ("goto_location",), Action.FLY_MISSION: ("upload_mission", "start_mission"),
}

class AdmissionError(LeaseError):
    def __init__(self, reason):
        super().__init__(reason.value)
        self.reason = reason


def waypoint(p):
    return Waypoint(p["latitude_e7"] / 1e7, p["longitude_e7"] / 1e7,
                    p["relative_altitude_mm"] / 1000,
                    None if p["speed_mm_s"] is None else p["speed_mm_s"] / 1000)


def execution_parameters(request):
    p = request.parameters
    if request.action is Action.TAKEOFF:
        return MappingProxyType({"altitude_m": p["altitude_mm"] / 1000})
    if request.action is Action.GOTO:
        return MappingProxyType({"waypoint": waypoint(p), "yaw_deg": p["yaw_mdeg"] / 1000,
            "absolute_altitude_m": (request.home_anchor["absolute_altitude_amsl_mm"] + p["relative_altitude_mm"]) / 1000})
    if request.action is Action.FLY_MISSION:
        return MappingProxyType({"waypoints": tuple(waypoint(w) for w in p["waypoints"])})
    return MappingProxyType({})


def check_observation(request, observation, *, context, policy, adapter_name, now, preflight=True):
    if observation is None or observation.telemetry.metadata is None:
        raise AdmissionError(ReasonCode.TELEMETRY_INCOMPLETE)
    t, m, cfg = observation.telemetry, observation.telemetry.metadata, policy.config
    physical = request.environment is Environment.PX4_SITL
    if adapter_name != {Environment.PX4_SITL: "mavsdk", Environment.KINEMATIC: "sim", Environment.MOCK: "mock"}[request.environment]:
        raise AdmissionError(ReasonCode.VEHICLE_CHANGED)
    if str(m.connection_epoch) != request.connection_epoch or str(m.home_epoch) != request.home_epoch or observation.home_anchor != request.home_anchor:
        raise AdmissionError(ReasonCode.VEHICLE_CHANGED)
    fields = ("position", "flight_mode", "armed", "in_air", "home") + (("battery", "gps") if physical else ())
    health = assess_snapshot(t, now_monotonic=now, connection_epoch=m.connection_epoch,
        home_epoch=m.home_epoch, connected=observation.connected,
        monitor_healthy=observation.monitor_healthy, expected_source="MAVSDK" if physical else "SYNTHETIC",
        config=MonitorConfig(max_age_s=cfg.max_age_ms / 1000, max_skew_s=cfg.max_skew_ms / 1000, required_fields=fields))
    if health.state is not MonitorState.READY:
        reason = (ReasonCode.MONITOR_UNHEALTHY if health.state in {MonitorState.FAULT, MonitorState.DISCONNECTED} else
                  ReasonCode.TELEMETRY_STALE if health.stale_fields else
                  ReasonCode.TELEMETRY_INCOMPLETE if health.missing_fields or health.invalid_fields else
                  ReasonCode.MONITOR_UNHEALTHY)
        raise AdmissionError(reason)
    hm, anchor = m.fields.get("home"), observation.home_anchor
    if observation.home is None or anchor is None or hm is None:
        raise AdmissionError(ReasonCode.TELEMETRY_INCOMPLETE)
    if (not hm.valid or not math.isfinite(hm.received_monotonic)
            or not 0 <= now - hm.received_monotonic <= cfg.max_age_ms / 1000
            or hm.connection_epoch != m.connection_epoch):
        raise AdmissionError(ReasonCode.TELEMETRY_STALE)
    if (anchor.get("vertical_datum") != "AMSL" or str(anchor.get("home_epoch")) != request.home_epoch
            or any(type(anchor.get(n)) is not int for n in ("latitude_e7", "longitude_e7", "absolute_altitude_amsl_mm"))
            or abs(anchor["latitude_e7"]) > 900000000 or abs(anchor["longitude_e7"]) > 1800000000):
        raise AdmissionError(ReasonCode.TELEMETRY_INCOMPLETE)
    if (not valid_observation_value("home", observation.home)
            or canonical_home_anchor(observation.home, m.home_epoch) != anchor):
        raise AdmissionError(ReasonCode.TELEMETRY_INCOMPLETE)
    result = evaluate(context, policy, request.action, request.parameters,
                      battery_percent=t.battery.remaining_percent if t.battery else None)
    if result.decision is PolicyDecision.DENY:
        raise AdmissionError(result.reason_code)
    if physical and (t.gps.num_satellites < cfg.min_satellites or t.gps.fix_type not in {"FIX_3D", "FIX_DGPS", "RTK_FLOAT", "RTK_FIXED"}):
        raise AdmissionError(ReasonCode.UNSAFE_STATE)
    if preflight:
        if request.action is Action.ARM and (t.in_air is not False or t.armed is not False):
            raise AdmissionError(ReasonCode.UNSAFE_STATE)
        if request.action is Action.DISARM and t.in_air is not False:
            raise AdmissionError(ReasonCode.UNSAFE_STATE)
        if request.action is Action.TAKEOFF and (t.in_air is not False or t.armed is not True):
            raise AdmissionError(ReasonCode.UNSAFE_STATE)
        if request.action in {Action.GOTO, Action.FLY_MISSION} and (t.in_air is not True or t.armed is not True or t.flight_mode not in {"HOLD", "POSCTL"}):
            raise AdmissionError(ReasonCode.UNSAFE_STATE)
    check_targets(request, cfg)


def check_targets(request, cfg):
    """Bound target and effective float32 altitude, not GPS/dynamic errors."""
    p, home, margin = request.parameters, request.home_anchor, cfg.numerical_margin_mm / 1000
    def f32(value):
        return struct.unpack("!f", struct.pack("!f", value))[0]
    if request.action is Action.TAKEOFF:
        intended = p["altitude_mm"] / 1000
        if abs(f32(intended) - intended) > margin or f32(intended) > cfg.max_altitude_m - margin:
            raise AdmissionError(ReasonCode.ALTITUDE_LIMIT)
    points = (p,) if request.action is Action.GOTO else p.get("waypoints", ())
    for point in points:
        distance = haversine_m(home["latitude_e7"] / 1e7, home["longitude_e7"] / 1e7,
                               point["latitude_e7"] / 1e7, point["longitude_e7"] / 1e7)
        if distance > cfg.max_radius_m - margin:
            raise AdmissionError(ReasonCode.OUTSIDE_GEOFENCE)
        altitude = point["relative_altitude_mm"] / 1000
        base = home["absolute_altitude_amsl_mm"] / 1000 if request.action is Action.GOTO else 0
        effective = f32(base + altitude) - base
        if abs(effective - altitude) + 0.0005 > margin or effective > cfg.max_altitude_m - margin:
            raise AdmissionError(ReasonCode.ALTITUDE_LIMIT)
        speed = point["speed_mm_s"]
        if speed is not None and f32(speed / 1000) > cfg.max_speed_mm_s / 1000:
            raise AdmissionError(ReasonCode.UNSAFE_STATE)
