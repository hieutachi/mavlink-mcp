"""Pure deterministic checks over canonical parameters; no I/O or LLM."""
from __future__ import annotations
import math
from typing import Any, Mapping
from .authorization import TrustedContext, authorize
from .config import PolicySnapshot
from .models import Action, PolicyDecision, PolicyResult, ReasonCode, RiskLabel


def evaluate(context: TrustedContext, snapshot: PolicySnapshot, action: Action,
             parameters: Mapping[str, Any] | None = None, *, battery_percent=None) -> PolicyResult:
    def deny(reason):
        return PolicyResult(PolicyDecision.DENY, reason, RiskLabel.HIGH)
    if context.environment != snapshot.config.environment.value:
        return deny(ReasonCode.POLICY_CHANGED)
    if snapshot.config.readonly:
        return deny(ReasonCode.READONLY)
    result = authorize(context, action)
    if result.decision is PolicyDecision.DENY:
        return result
    if battery_percent is not None:
        if isinstance(battery_percent, bool) or not isinstance(battery_percent, (int, float)) or not math.isfinite(battery_percent) or not 0 <= battery_percent <= 100:
            return deny(ReasonCode.INVALID_ARGUMENT)
        if battery_percent < snapshot.config.min_battery_percent and action in {Action.ARM, Action.TAKEOFF, Action.GOTO, Action.FLY_MISSION}:
            return deny(ReasonCode.UNSAFE_STATE)
    params = parameters or {}
    ceiling = snapshot.config.max_altitude_m * 1000 - snapshot.config.numerical_margin_mm
    if action is Action.TAKEOFF:
        altitude = params.get("altitude_mm")
        if set(params) != {"altitude_mm"} or type(altitude) is not int or altitude <= 0:
            return deny(ReasonCode.INVALID_ARGUMENT)
        if altitude > ceiling:
            return deny(ReasonCode.ALTITUDE_LIMIT)
    elif action in {Action.GOTO, Action.FLY_MISSION}:
        points = (params,) if action is Action.GOTO else params.get("waypoints", ())
        if action is Action.FLY_MISSION and (set(params) != {"waypoints"} or not isinstance(points, (list, tuple)) or not 1 <= len(points) <= 1000):
            return deny(ReasonCode.INVALID_ARGUMENT)
        for point in points:
            if not isinstance(point, Mapping) or set(point) != {"latitude_e7", "longitude_e7", "relative_altitude_mm", "speed_mm_s", "yaw_mdeg"}:
                return deny(ReasonCode.INVALID_ARGUMENT)
            for name, low, high in (("latitude_e7", -900000000, 900000000), ("longitude_e7", -1800000000, 1800000000), ("relative_altitude_mm", 1, 1000000000), ("yaw_mdeg", 0, 359999)):
                value = point[name]
                if type(value) is not int or not low <= value <= high:
                    return deny(ReasonCode.INVALID_ARGUMENT)
            if point["relative_altitude_mm"] > ceiling:
                return deny(ReasonCode.ALTITUDE_LIMIT)
            speed = point["speed_mm_s"]
            if speed is not None and (type(speed) is not int or not 0 < speed <= snapshot.config.max_speed_mm_s):
                return deny(ReasonCode.INVALID_ARGUMENT)
    elif params:
        return deny(ReasonCode.INVALID_ARGUMENT)
    return result
