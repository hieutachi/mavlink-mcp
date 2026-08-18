"""Capability layer facade: guardrails + capability checks over an adapter.

``Drone`` is the single entry point for both surfaces of this project (the
Python library and the MCP server). It owns all policy: readonly mode,
explicit confirmation for arming/takeoff, the altitude ceiling, and the
soft geofence. The adapter underneath only translates already-approved
operations to its protocol.
"""

from __future__ import annotations

from mavlink_mcp.adapter import DroneAdapter
from mavlink_mcp.capabilities import (
    Capability,
    CapabilityNotSupported,
    Position,
    TelemetrySnapshot,
    Waypoint,
)
from mavlink_mcp.guardrails import (
    GuardrailConfig,
    GuardrailViolation,
    haversine_m,
)


class Drone:
    """One vehicle, exposed through the vendor-neutral capability API.

    Args:
        adapter: The backend that talks to the vehicle.
        guardrails: Fixed guardrail settings. When omitted, settings are
            re-read from environment variables on every check, so a running
            server honors ``MAVLINK_MCP_READONLY`` changes without restart.
    """

    def __init__(
        self,
        adapter: DroneAdapter,
        guardrails: GuardrailConfig | None = None,
    ) -> None:
        self._adapter = adapter
        self._fixed_guardrails = guardrails

    @property
    def adapter(self) -> DroneAdapter:
        return self._adapter

    @property
    def guardrails(self) -> GuardrailConfig:
        if self._fixed_guardrails is not None:
            return self._fixed_guardrails
        return GuardrailConfig.from_env()

    def capabilities(self) -> frozenset[Capability]:
        """Capabilities the connected backend declares."""
        return self._adapter.capabilities()

    # ------------------------------------------------------------------ #
    # Guardrail / capability checks
    # ------------------------------------------------------------------ #

    def _require_capability(self, capability: Capability) -> None:
        if capability not in self._adapter.capabilities():
            raise CapabilityNotSupported(
                f"The '{self._adapter.name}' adapter does not support the "
                f"'{capability.value}' capability."
            )

    def _require_write(self, operation: str) -> None:
        if self.guardrails.readonly:
            raise GuardrailViolation(
                f"Rejected: {operation} is disabled because readonly mode "
                "is active (MAVLINK_MCP_READONLY defaults to true). Set "
                "MAVLINK_MCP_READONLY=false to allow tools that can move "
                "the vehicle — recommended only against a simulator."
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

    async def _check_geofence(
        self, waypoints: list[Waypoint], operation: str
    ) -> None:
        radius = self.guardrails.geofence_radius_m
        if radius <= 0:
            return
        home = await self._adapter.get_home()
        if home is None:
            raise GuardrailViolation(
                f"Rejected: {operation} — the soft geofence is enabled "
                f"({radius} m around home) but the vehicle has not reported "
                "a home position yet, so the target cannot be verified. "
                "Wait for a GPS fix, or set MAVLINK_MCP_GEOFENCE_RADIUS_M=0 "
                "to disable the geofence (not recommended)."
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

    # ------------------------------------------------------------------ #
    # Telemetry capabilities (allowed in readonly mode)
    # ------------------------------------------------------------------ #

    async def get_telemetry(self) -> TelemetrySnapshot:
        self._require_capability(Capability.TELEMETRY)
        await self._ensure_connected()
        return await self._adapter.get_telemetry()

    async def get_flight_mode(self) -> str:
        self._require_capability(Capability.FLIGHT_MODE)
        await self._ensure_connected()
        return await self._adapter.get_flight_mode()

    async def get_home(self) -> Position | None:
        self._require_capability(Capability.TELEMETRY)
        await self._ensure_connected()
        return await self._adapter.get_home()

    # ------------------------------------------------------------------ #
    # Action capabilities (rejected in readonly mode)
    # ------------------------------------------------------------------ #

    async def arm(self, confirm: bool = False) -> None:
        self._require_capability(Capability.ARM_DISARM)
        self._require_write("arm")
        self._require_confirmation("arm", confirm)
        await self._ensure_connected()
        await self._adapter.arm()

    async def disarm(self) -> None:
        self._require_capability(Capability.ARM_DISARM)
        self._require_write("disarm")
        await self._ensure_connected()
        await self._adapter.disarm()

    async def takeoff(self, altitude_m: float, confirm: bool = False) -> None:
        self._require_capability(Capability.TAKEOFF)
        self._require_write("takeoff")
        self._require_confirmation("takeoff", confirm)
        self._check_altitude(altitude_m, "takeoff")
        await self._ensure_connected()
        await self._adapter.takeoff(altitude_m)

    async def land(self) -> None:
        self._require_capability(Capability.LAND)
        self._require_write("land")
        await self._ensure_connected()
        await self._adapter.land()

    async def hold(self) -> None:
        self._require_capability(Capability.FLIGHT_MODE)
        self._require_write("hold")
        await self._ensure_connected()
        await self._adapter.hold()

    async def return_to_launch(self) -> None:
        self._require_capability(Capability.RETURN_TO_LAUNCH)
        self._require_write("return_to_launch")
        await self._ensure_connected()
        await self._adapter.return_to_launch()

    async def goto(
        self,
        latitude_deg: float,
        longitude_deg: float,
        relative_altitude_m: float,
        yaw_deg: float = 0.0,
    ) -> None:
        self._require_capability(Capability.GOTO)
        self._require_write("goto")
        self._check_altitude(relative_altitude_m, "goto")
        waypoint = Waypoint(latitude_deg, longitude_deg, relative_altitude_m)
        await self._ensure_connected()
        await self._check_geofence([waypoint], "goto")
        await self._adapter.goto(waypoint, yaw_deg=yaw_deg)

    async def fly_mission(self, waypoints: list[Waypoint]) -> None:
        self._require_capability(Capability.MISSION)
        self._require_write("fly_mission")
        if not waypoints:
            raise GuardrailViolation(
                "Rejected: fly_mission requires at least one waypoint."
            )
        for wp in waypoints:
            self._check_altitude(wp.relative_altitude_m, "fly_mission")
        await self._ensure_connected()
        await self._check_geofence(waypoints, "fly_mission")
        await self._adapter.fly_mission(waypoints)
