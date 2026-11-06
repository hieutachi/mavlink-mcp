"""Safety guardrails: readonly mode, altitude limit, soft geofence.

Same philosophy as rosbridge-mcp, tightened for flight: the server starts
in **readonly mode by default** (``MAVLINK_MCP_READONLY`` defaults to
true), arming and takeoff additionally require an explicit ``confirm=true``
argument, and every commanded location is checked against a configurable
altitude ceiling and a soft geofence around the launch point.

These checks run inside this process, in the capability layer. They are a
policy filter for the connected AI agent — not a substitute for the
autopilot's own failsafes or for keeping real hardware off untrusted
networks.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from decimal import Decimal


def finite_number(value: float) -> bool:
    """Reject nonnumeric, nonfinite and unrepresentable Python numbers."""
    try:
        return math.isfinite(value)
    except (TypeError, ValueError, OverflowError):
        return False


_FALSY = {"0", "false", "no", "off"}

DEFAULT_MAX_ALTITUDE_M = 50.0
DEFAULT_GEOFENCE_RADIUS_M = 200.0


class GuardrailViolation(Exception):
    """Raised when an operation is rejected by a safety guardrail."""


@dataclass(frozen=True)
class GuardrailConfig:
    """Active guardrail settings.

    - ``readonly``: when true, every tool that can move the vehicle is
      rejected. **Defaults to true** — you must opt in to flight.
    - ``max_altitude_m``: commanded altitudes above this (relative to the
      launch point) are rejected. ``0`` disables the ceiling.
    - ``geofence_radius_m``: commanded locations farther than this from the
      home position are rejected. ``0`` disables the soft geofence.
    """

    readonly: bool = True
    max_altitude_m: float = DEFAULT_MAX_ALTITUDE_M
    geofence_radius_m: float = DEFAULT_GEOFENCE_RADIUS_M

    def __post_init__(self) -> None:
        for name in ("max_altitude_m", "geofence_radius_m"):
            value = getattr(self, name)
            if not finite_number(value) or value < 0:
                raise ValueError(f"{name} must be finite and non-negative.")

    @classmethod
    def from_env(cls) -> "GuardrailConfig":
        """Read guardrail settings from environment variables.

        ``MAVLINK_MCP_READONLY`` (default ``true``),
        ``MAVLINK_MCP_MAX_ALTITUDE_M`` (default ``50``),
        ``MAVLINK_MCP_GEOFENCE_RADIUS_M`` (default ``200``).
        Unparseable numbers fall back to the defaults.
        """
        readonly = (
            os.environ.get("MAVLINK_MCP_READONLY", "true").strip().lower()
            not in _FALSY
        )
        return cls(
            readonly=readonly,
            max_altitude_m=_env_float(
                "MAVLINK_MCP_MAX_ALTITUDE_M", DEFAULT_MAX_ALTITUDE_M
            ),
            geofence_radius_m=_env_float(
                "MAVLINK_MCP_GEOFENCE_RADIUS_M", DEFAULT_GEOFENCE_RADIUS_M
            ),
        )


def _env_float(var: str, default: float) -> float:
    raw = os.environ.get(var, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if not math.isfinite(value) or value < 0:
        return default
    # Only explicit textual zero may disable a limit, not float underflow.
    if value == 0 and Decimal(raw) != 0:
        return default
    return value


def haversine_m(
    lat1_deg: float, lon1_deg: float, lat2_deg: float, lon2_deg: float
) -> float:
    """Great-circle distance between two WGS84 points, in meters."""
    earth_radius_m = 6371000.0
    phi1 = math.radians(lat1_deg)
    phi2 = math.radians(lat2_deg)
    dphi = math.radians(lat2_deg - lat1_deg)
    dlambda = math.radians(lon2_deg - lon1_deg)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return 2 * earth_radius_m * math.asin(math.sqrt(min(1.0, max(0.0, a))))
