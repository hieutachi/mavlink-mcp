"""Supported Python facade: every write must use SafetyGateway.

confirm is retained for compatibility, never authorization. Private in-process
objects are trusted composition, not an isolation boundary (G1, not G2).
"""

from __future__ import annotations

from mavlink_mcp.adapter import DroneAdapter
from mavlink_mcp.safety.gateway import GatewayResult, SafetyGateway
from mavlink_mcp.safety.models import Action, PolicyDecision, ReasonCode
from mavlink_mcp.capabilities import (
    Capability,
    CapabilityNotSupported,
    Position,
    TelemetrySnapshot,
    Waypoint,
)
from mavlink_mcp.guardrails import GuardrailConfig


class Drone:
    """Read-only unless composed with a trusted gateway and signed approvals."""

    def __init__(
        self,
        adapter: DroneAdapter,
        guardrails: GuardrailConfig | None = None,
        *,
        safety_gateway: SafetyGateway | None = None,
    ) -> None:
        if safety_gateway is not None and safety_gateway.adapter is not adapter:
            raise ValueError("gateway and facade must share the same adapter")
        self._adapter = adapter
        self._fixed_guardrails = guardrails
        self._gateway = safety_gateway

    @property
    def backend_info(self) -> dict[str, object]:
        """Safe non-mutating backend descriptor; never exposes raw adapter."""
        return {"name": self._adapter.name, "protected": self._adapter.protected,
                "connected": self._adapter.is_connected(), "gateway_configured": self._gateway is not None}

    def simulation_metadata(self, *, advance: bool = True) -> dict[str, object] | None:
        info = getattr(self._adapter, "simulation_info", None)
        return info(advance=advance) if info is not None else None

    @property
    def guardrails(self) -> GuardrailConfig:
        if self._gateway is not None:
            cfg = self._gateway.policy.config
            return GuardrailConfig(readonly=cfg.readonly or self._gateway.context.principal.readonly,
                                   max_altitude_m=cfg.max_altitude_m, geofence_radius_m=cfg.max_radius_m)
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

    async def _execute(self, action, capability, parameters, *, request_id=None, approval_token=None):
        self._require_capability(capability)
        if self._gateway is None:
            reason = ReasonCode.READONLY if self.guardrails.readonly else ReasonCode.GATEWAY_UNAVAILABLE
            return GatewayResult(PolicyDecision.DENY, reason, request_id)
        return await self._gateway.execute(action, parameters, request_id=request_id, approval_token=approval_token)

    async def arm(self, confirm: bool = False, *, request_id=None, approval_token=None) -> GatewayResult:
        return await self._execute(Action.ARM, Capability.ARM_DISARM, {}, request_id=request_id, approval_token=approval_token)

    async def disarm(self, *, request_id=None, approval_token=None) -> GatewayResult:
        return await self._execute(Action.DISARM, Capability.ARM_DISARM, {}, request_id=request_id, approval_token=approval_token)

    async def takeoff(self, altitude_m: float, confirm: bool = False, *, request_id=None, approval_token=None) -> GatewayResult:
        return await self._execute(Action.TAKEOFF, Capability.TAKEOFF, {"altitude_m": altitude_m}, request_id=request_id, approval_token=approval_token)

    async def land(self, *, request_id=None, approval_token=None) -> GatewayResult:
        return await self._execute(Action.LAND, Capability.LAND, {}, request_id=request_id, approval_token=approval_token)

    async def hold(self, *, request_id=None, approval_token=None) -> GatewayResult:
        return await self._execute(Action.HOLD, Capability.FLIGHT_MODE, {}, request_id=request_id, approval_token=approval_token)

    async def return_to_launch(self, *, request_id=None, approval_token=None) -> GatewayResult:
        return await self._execute(Action.RETURN_TO_LAUNCH, Capability.RETURN_TO_LAUNCH, {}, request_id=request_id, approval_token=approval_token)

    async def goto(self, latitude_deg: float, longitude_deg: float, relative_altitude_m: float,
                   yaw_deg: float = 0.0, *, request_id=None, approval_token=None) -> GatewayResult:
        return await self._execute(Action.GOTO, Capability.GOTO,
            {"latitude_deg": latitude_deg, "longitude_deg": longitude_deg,
             "relative_altitude_m": relative_altitude_m, "yaw_deg": yaw_deg},
            request_id=request_id, approval_token=approval_token)

    async def fly_mission(self, waypoints: list[Waypoint], *, request_id=None, approval_token=None) -> GatewayResult:
        # execute copies caller-owned waypoints before the first await.
        return await self._execute(Action.FLY_MISSION, Capability.MISSION, {"waypoints": waypoints},
                                   request_id=request_id, approval_token=approval_token)
