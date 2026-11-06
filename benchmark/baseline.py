"""Faithful reconstruction of the ORIGINAL pre-gateway guardrail design.

PROVENANCE
----------
Vendored verbatim-in-logic from git commit ``962448c`` (the initial MVP1
scaffold, "feat: initial MVP1 scaffold - UAV capability layer + MCP server
over MAVSDK"):

  * ``git show 962448c:src/mavlink_mcp/guardrails.py``  -> GuardrailConfig,
    GuardrailViolation, haversine_m, DEFAULT_* constants
  * ``git show 962448c:src/mavlink_mcp/drone.py``       -> BaselineDrone
    (class ``Drone`` at that commit), including every ``_require_*`` /
    ``_check_*`` method and the exact per-action check ordering.

This module is a RECONSTRUCTION FOR COMPARISON ONLY. It is **not** current
code and is never imported by ``src/mavlink_mcp``. It is deliberately
self-contained: importing anything from the present-day ``src`` tree would
measure the new gateway twice and destroy the comparison.

WHAT THE BASELINE DESIGN HAD
----------------------------
  * ``MAVLINK_MCP_READONLY`` kill switch (default true) checked in-process
  * caller-supplied ``confirm: bool`` gate on ``arm`` and ``takeoff`` only
  * altitude ceiling check (``MAVLINK_MCP_MAX_ALTITUDE_M``, default 50 m)
  * haversine soft geofence around home (``MAVLINK_MCP_GEOFENCE_RADIUS_M``,
    default 200 m)
  * ``haversine_m`` great-circle helper

WHAT IT DID NOT HAVE
--------------------
  no human approval, no cryptographic operator signature, no nonce / replay
  protection, no durable ledger or audit chain, no telemetry freshness or
  staleness check, no per-sink revalidation, no execution lease, no request
  digest or parameter binding, no vehicle/session binding, no canonical
  argument normalization.

Only two deliberate, documented adaptations were made, both required to run
the two arms against an identical spy adapter (see ADAPTATIONS below). No
baseline *policy* was weakened, strengthened, added, or removed.

ADAPTATIONS (declared, not hidden)
----------------------------------
1. ``BaselineDrone`` calls the shared benchmark ``SpyAdapter`` instead of the
   historical ``DroneAdapter`` ABC. The 962448c adapter had no ``lease=``
   keyword, so the spy exposes unprotected ``baseline_*`` entry points that
   record the SAME physical sink names the current arm records. This is a
   call-schema shim only: it adds no check and removes none.
2. The readonly default is explicitly set to ``readonly=False`` for the
   writable comparison arm, matching the master prompt's statement of the old
   design (``Drone(adapter, GuardrailConfig(readonly=False))``). A separate
   readonly control arm is also measured, so the kill switch is credited
   where it actually works.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass

# --------------------------------------------------------------------- #
# From 962448c:src/mavlink_mcp/guardrails.py
# --------------------------------------------------------------------- #

_FALSY = {"0", "false", "no", "off"}

DEFAULT_MAX_ALTITUDE_M = 50.0
DEFAULT_GEOFENCE_RADIUS_M = 200.0


class GuardrailViolation(Exception):
    """Raised when an operation is rejected by a safety guardrail."""


class CapabilityNotSupported(Exception):
    """Raised when an operation needs a capability the adapter lacks."""


@dataclass(frozen=True)
class GuardrailConfig:
    """Active guardrail settings (962448c semantics).

    - ``readonly``: when true, every tool that can move the vehicle is
      rejected. **Defaults to true** - you must opt in to flight.
    - ``max_altitude_m``: commanded altitudes above this (relative to the
      launch point) are rejected. ``0`` disables the ceiling.
    - ``geofence_radius_m``: commanded locations farther than this from the
      home position are rejected. ``0`` disables the soft geofence.
    """

    readonly: bool = True
    max_altitude_m: float = DEFAULT_MAX_ALTITUDE_M
    geofence_radius_m: float = DEFAULT_GEOFENCE_RADIUS_M

    @classmethod
    def from_env(cls) -> "GuardrailConfig":
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
        return float(raw)
    except ValueError:
        return default


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
    return 2 * earth_radius_m * math.asin(math.sqrt(a))


# --------------------------------------------------------------------- #
# From 962448c:src/mavlink_mcp/drone.py (class Drone)
# --------------------------------------------------------------------- #


@dataclass(frozen=True)
class BaselineWaypoint:
    """962448c capabilities.Waypoint, vendored so no src import is needed."""

    latitude_deg: float
    longitude_deg: float
    relative_altitude_m: float
    speed_m_s: float | None = None


class BaselineDrone:
    """The pre-gateway capability facade, exactly as commit 962448c shipped it.

    Every guardrail decision below is a line-for-line port of that commit.
    The only difference is that the adapter calls go through the benchmark
    spy's unprotected ``baseline_*`` entry points (see ADAPTATIONS).
    """

    def __init__(self, adapter, guardrails: GuardrailConfig | None = None) -> None:
        self._adapter = adapter
        self._fixed_guardrails = guardrails

    @property
    def adapter(self):
        return self._adapter

    @property
    def guardrails(self) -> GuardrailConfig:
        if self._fixed_guardrails is not None:
            return self._fixed_guardrails
        return GuardrailConfig.from_env()

    def capabilities(self):
        return self._adapter.capabilities()

    # -------------------- guardrail / capability checks -------------- #

    def _require_capability(self, capability) -> None:
        if capability not in self._adapter.capabilities():
            raise CapabilityNotSupported(
                f"The '{self._adapter.name}' adapter does not support the "
                f"'{getattr(capability, 'value', capability)}' capability."
            )

    def _require_write(self, operation: str) -> None:
        if self.guardrails.readonly:
            raise GuardrailViolation(
                f"Rejected: {operation} is disabled because readonly mode "
                "is active (MAVLINK_MCP_READONLY defaults to true). Set "
                "MAVLINK_MCP_READONLY=false to allow tools that can move "
                "the vehicle - recommended only against a simulator."
            )

    @staticmethod
    def _require_confirmation(operation: str, confirm: bool) -> None:
        if not confirm:
            raise GuardrailViolation(
                f"Rejected: {operation} requires explicit confirmation. "
                "Ask the human operator to approve, then call again with "
                "confirm=true. Do not set confirm=true on your own."
            )

    def _check_altitude(self, altitude_m: float, operation: str) -> None:
        if altitude_m <= 0:
            raise GuardrailViolation(
                f"Rejected: {operation} altitude must be positive "
                f"(got {altitude_m})."
            )
        limit = self.guardrails.max_altitude_m
        if limit > 0 and altitude_m > limit:
            raise GuardrailViolation(
                f"Rejected: {operation} altitude {altitude_m} m exceeds the "
                f"configured ceiling of {limit} m "
                "(MAVLINK_MCP_MAX_ALTITUDE_M)."
            )

    async def _check_geofence(self, waypoints, operation: str) -> None:
        radius = self.guardrails.geofence_radius_m
        if radius <= 0:
            return
        home = await self._adapter.get_home()
        if home is None:
            raise GuardrailViolation(
                f"Rejected: {operation} - the soft geofence is enabled "
                f"({radius} m around home) but the vehicle has not reported "
                "a home position yet, so the target cannot be verified."
            )
        for wp in waypoints:
            distance = haversine_m(
                home.latitude_deg,
                home.longitude_deg,
                wp.latitude_deg,
                wp.longitude_deg,
            )
            if distance > radius:
                raise GuardrailViolation(
                    f"Rejected: {operation} target "
                    f"({wp.latitude_deg}, {wp.longitude_deg}) is "
                    f"{distance:.0f} m from home, outside the "
                    f"{radius:.0f} m soft geofence "
                    "(MAVLINK_MCP_GEOFENCE_RADIUS_M)."
                )

    async def _ensure_connected(self) -> None:
        if not self._adapter.is_connected():
            await self._adapter.connect()

    # -------------------- telemetry (allowed in readonly) ------------ #

    async def get_telemetry(self):
        self._require_capability("telemetry")
        await self._ensure_connected()
        return await self._adapter.get_telemetry()

    async def get_flight_mode(self) -> str:
        self._require_capability("flight_mode")
        await self._ensure_connected()
        return await self._adapter.get_flight_mode()

    async def get_home(self):
        self._require_capability("telemetry")
        await self._ensure_connected()
        return await self._adapter.get_home()

    # -------------------- actions (rejected in readonly) ------------- #

    async def arm(self, confirm: bool = False) -> None:
        self._require_capability("arm_disarm")
        self._require_write("arm")
        self._require_confirmation("arm", confirm)
        await self._ensure_connected()
        await self._adapter.baseline_arm()

    async def disarm(self) -> None:
        self._require_capability("arm_disarm")
        self._require_write("disarm")
        await self._ensure_connected()
        await self._adapter.baseline_disarm()

    async def takeoff(self, altitude_m: float, confirm: bool = False) -> None:
        self._require_capability("takeoff")
        self._require_write("takeoff")
        self._require_confirmation("takeoff", confirm)
        self._check_altitude(altitude_m, "takeoff")
        await self._ensure_connected()
        await self._adapter.baseline_takeoff(altitude_m)

    async def land(self) -> None:
        self._require_capability("land")
        self._require_write("land")
        await self._ensure_connected()
        await self._adapter.baseline_land()

    async def hold(self) -> None:
        self._require_capability("flight_mode")
        self._require_write("hold")
        await self._ensure_connected()
        await self._adapter.baseline_hold()

    async def return_to_launch(self) -> None:
        self._require_capability("return_to_launch")
        self._require_write("return_to_launch")
        await self._ensure_connected()
        await self._adapter.baseline_return_to_launch()

    async def goto(
        self,
        latitude_deg: float,
        longitude_deg: float,
        relative_altitude_m: float,
        yaw_deg: float = 0.0,
    ) -> None:
        self._require_capability("goto")
        self._require_write("goto")
        self._check_altitude(relative_altitude_m, "goto")
        waypoint = BaselineWaypoint(latitude_deg, longitude_deg, relative_altitude_m)
        await self._ensure_connected()
        await self._check_geofence([waypoint], "goto")
        await self._adapter.baseline_goto(waypoint, yaw_deg=yaw_deg)

    async def fly_mission(self, waypoints) -> None:
        self._require_capability("mission")
        self._require_write("fly_mission")
        if not waypoints:
            raise GuardrailViolation(
                "Rejected: fly_mission requires at least one waypoint."
            )
        for wp in waypoints:
            self._check_altitude(wp.relative_altitude_m, "fly_mission")
        await self._ensure_connected()
        await self._check_geofence(waypoints, "fly_mission")
        await self._adapter.baseline_fly_mission(waypoints)
