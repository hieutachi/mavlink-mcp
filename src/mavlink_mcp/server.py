"""MCP telemetry and gateway-mediated UAV requests over stdio.

SAFEMAV_CONFIG selects trusted launch policy, public approval keys and a durable
ledger. Without it the server is read-only, regardless of legacy safety env
variables. MAVLINK_MCP_BACKEND selects sim or mavsdk; MAVLINK_MCP_URL selects
the latter's endpoint. No MCP tool can modify trust, policy, or issue approval.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastmcp import FastMCP
from pydantic import StrictBool, StrictFloat, StrictInt, StrictStr

from mavlink_mcp.adapter import AdapterError
from mavlink_mcp.capabilities import CapabilityNotSupported
from mavlink_mcp.drone import Drone
from mavlink_mcp.guardrails import GuardrailViolation
from mavlink_mcp.safety.runtime import create_drone

# Preserve JSON numeric types; never turn booleans or numeric strings into motion.
Number = StrictInt | StrictFloat

mcp = FastMCP(
    "mavlink-mcp",
    instructions=(
        "Vendor-neutral capability layer for a MAVLink drone (PX4/ArduPilot, "
        "simulator-first). Call get_capabilities first to see what the "
        "vehicle supports and which guardrails are active, get_telemetry to "
        "observe it, and arm/takeoff/goto_location/fly_mission/land/"
        "return_to_launch to request flight actions. Every mutation requires "
        "a signed human approval from the trusted operator, including recovery "
        "actions. Resubmit the exact parameters with the approval's request_id "
        "and approval_token. confirm is deprecated and never authorizes. "
        "Without trusted launch config the server is read-only. Backend sim "
        "is a network-free kinematic demonstration, not SITL or safety evidence. "
        "When simulation.synthetic is true, "
        "null battery/GPS mean unmodelled sensors, not a preflight fault. "
        "Action acknowledgements mean accepted, not completed; observe "
        "telemetry before claiming the flight finished."
    ),
)

_drone: Drone | None = None


def get_drone() -> Drone:
    """Compose trusted runtime once; invalid launch settings never fall back."""
    global _drone
    if _drone is None:
        _drone = create_drone()
    return _drone


def _simulation_metadata(drone: Drone, *, advance: bool = True) -> dict[str, Any]:
    info = drone.simulation_metadata(advance=advance)
    return {"simulation": info} if info is not None else {}


def _error(exc: Exception) -> dict[str, Any]:
    result: dict[str, Any] = {"error": str(exc)}
    if isinstance(exc, GuardrailViolation):
        result["guardrail"] = True
    elif isinstance(exc, CapabilityNotSupported):
        result["capability_missing"] = True
    return result


_HANDLED = (GuardrailViolation, CapabilityNotSupported, AdapterError)


# ---------------------------------------------------------------------- #
# Tool implementations (plain async functions, registered with mcp below)
# ---------------------------------------------------------------------- #


async def get_capabilities() -> dict[str, Any]:
    """Discover what this vehicle can do and which guardrails are active.

    Takes no arguments; does not require a live vehicle. Call this first.
    Returns {"adapter", "capabilities": ["telemetry", "arm_disarm", ...],
    "guardrails": {"readonly", "max_altitude_m", "geofence_radius_m"},
    "connected"}. Plan only with listed capabilities; if "readonly" is
    true, every tool that can move the vehicle will be rejected.
    """
    try:
        drone = get_drone()
    except _HANDLED as exc:
        return _error(exc)
    return {
        "adapter": drone.backend_info["name"],
        "capabilities": sorted(c.value for c in drone.capabilities()),
        "guardrails": asdict(drone.guardrails),
        "connected": bool(drone.backend_info["connected"]),
        **_simulation_metadata(drone),
    }


async def get_telemetry() -> dict[str, Any]:
    """Read one snapshot of the vehicle's telemetry. Read-only.

    Takes no arguments. Returns {"position": {latitude_deg, longitude_deg,
    absolute_altitude_m, relative_altitude_m}, "attitude": {roll_deg,
    pitch_deg, yaw_deg}, "battery": {voltage_v, remaining_percent},
    "gps": {num_satellites, fix_type}, "flight_mode", "armed", "in_air"}.
    Fields the vehicle did not report within a few seconds are null.
    Always check telemetry before commanding motion.
    """
    try:
        drone = get_drone()
        snapshot = await drone.get_telemetry()
    except _HANDLED as exc:
        return _error(exc)
    result = {
        "position": asdict(snapshot.position) if snapshot.position is not None else None,
        "attitude": asdict(snapshot.attitude) if snapshot.attitude is not None else None,
        "battery": asdict(snapshot.battery) if snapshot.battery is not None else None,
        "gps": asdict(snapshot.gps) if snapshot.gps is not None else None,
        "flight_mode": snapshot.flight_mode,
        "armed": snapshot.armed,
        "in_air": snapshot.in_air,
    }
    if snapshot.metadata is not None:
        result["telemetry_metadata"] = {
            "captured_monotonic": snapshot.metadata.captured_monotonic,
            "source": snapshot.metadata.source,
            "connection_epoch": snapshot.metadata.connection_epoch,
            "home_epoch": snapshot.metadata.home_epoch,
            "synthetic": snapshot.metadata.synthetic,
            "fields": {
                name: asdict(observation)
                for name, observation in snapshot.metadata.fields.items()
            },
        }
    return {**result, **_simulation_metadata(drone, advance=False)}


async def get_flight_mode() -> dict[str, Any]:
    """Report the current flight mode (e.g. HOLD, TAKEOFF, MISSION, RTL).

    Takes no arguments. Read-only. Returns {"flight_mode": "HOLD"}.
    """
    try:
        mode = await get_drone().get_flight_mode()
    except _HANDLED as exc:
        return _error(exc)
    return {"flight_mode": mode}


async def get_connection_status() -> dict[str, Any]:
    """Report the MAVLink endpoint, connection state, and guardrail config.

    Takes no arguments. The connection is opened lazily, so "connected" is
    false until another tool has been used. Check this first when other
    tools report errors.
    """
    try:
        drone = get_drone()
    except _HANDLED as exc:
        return _error(exc)
    status: dict[str, Any] = {
        "adapter": drone.backend_info["name"],
        "connected": bool(drone.backend_info["connected"]),
        "guardrails": asdict(drone.guardrails),
        **_simulation_metadata(drone),
    }
    address = None
    if address is not None:
        status["system_address"] = address
    return status


async def arm(
    confirm: StrictBool = False,
    request_id: StrictStr | None = None,
    approval_token: StrictStr | None = None,
) -> dict[str, Any]:
    """Request arming; dangerous on hardware. Requires signed human approval.

    First call returns an approval_request, never an armed-state assertion.
    Resubmit its request_id and the operator's approval_token unchanged.
    confirm is deprecated and ignored for authorization. ACKNOWLEDGED means
    command acceptance, not completion. Simulator results are not safety evidence.
    """
    try:
        return (await get_drone().arm(
            confirm=confirm, request_id=request_id, approval_token=approval_token,
        )).to_dict()
    except _HANDLED as exc:
        return _error(exc)


async def disarm(
    request_id: StrictStr | None = None,
    approval_token: StrictStr | None = None,
) -> dict[str, Any]:
    """Request disarming with signed human approval; rejected while in flight.

    Resubmit the exact request_id and approval_token. ACKNOWLEDGED is only
    acceptance; observe telemetry. Simulator results are not safety evidence.
    """
    try:
        return (await get_drone().disarm(
            request_id=request_id, approval_token=approval_token,
        )).to_dict()
    except _HANDLED as exc:
        return _error(exc)


async def takeoff(
    altitude_m: Number = 10.0,
    confirm: StrictBool = False,
    request_id: StrictStr | None = None,
    approval_token: StrictStr | None = None,
) -> dict[str, Any]:
    """Request vertical takeoff to altitude_m above home; must be armed first.

    Requires signed human approval of the exact altitude and request_id.
    Resubmit with approval_token; confirm is deprecated and never authorizes.
    Trusted policy bounds altitude. ACKNOWLEDGED is not completed flight;
    observe telemetry. Simulator results are not safety evidence.
    """
    try:
        return (await get_drone().takeoff(
            altitude_m, confirm=confirm, request_id=request_id,
            approval_token=approval_token,
        )).to_dict()
    except _HANDLED as exc:
        return _error(exc)


async def land(
    request_id: StrictStr | None = None,
    approval_token: StrictStr | None = None,
) -> dict[str, Any]:
    """Request landing here. Recovery still requires signed human approval.

    Resubmit request_id and approval_token unchanged. ACKNOWLEDGED does not
    mean landed; observe telemetry. Simulator results are not safety evidence.
    """
    try:
        return (await get_drone().land(
            request_id=request_id, approval_token=approval_token,
        )).to_dict()
    except _HANDLED as exc:
        return _error(exc)


async def hold(
    request_id: StrictStr | None = None,
    approval_token: StrictStr | None = None,
) -> dict[str, Any]:
    """Request HOLD with signed human approval, including for mission pauses.

    Resubmit request_id and approval_token unchanged. ACKNOWLEDGED means
    acceptance only. Observe telemetry; simulation is not safety evidence.
    """
    try:
        return (await get_drone().hold(
            request_id=request_id, approval_token=approval_token,
        )).to_dict()
    except _HANDLED as exc:
        return _error(exc)


async def return_to_launch(
    request_id: StrictStr | None = None,
    approval_token: StrictStr | None = None,
) -> dict[str, Any]:
    """Request return and landing at home. Requires signed human approval.

    Recovery is not an authorization bypass. Resubmit request_id and
    approval_token unchanged. ACKNOWLEDGED does not mean returned or landed;
    observe telemetry. Simulator results are not safety evidence.
    """
    try:
        return (await get_drone().return_to_launch(
            request_id=request_id, approval_token=approval_token,
        )).to_dict()
    except _HANDLED as exc:
        return _error(exc)


async def goto_location(
    latitude_deg: Number,
    longitude_deg: Number,
    relative_altitude_m: Number,
    yaw_deg: Number = 0.0,
    request_id: StrictStr | None = None,
    approval_token: StrictStr | None = None,
) -> dict[str, Any]:
    """Request a WGS84 target and home-relative altitude while armed and flying.

    yaw_deg is heading (0 north). Requires signed human approval of the exact
    target; resubmit identical parameters, request_id and approval_token.
    Trusted policy checks altitude and geofence. ACKNOWLEDGED is acceptance,
    not arrival; observe telemetry. Simulation is not safety evidence.
    """
    try:
        return (await get_drone().goto(
            latitude_deg, longitude_deg, relative_altitude_m, yaw_deg=yaw_deg,
            request_id=request_id, approval_token=approval_token,
        )).to_dict()
    except _HANDLED as exc:
        return _error(exc)


async def fly_mission(
    waypoints: list[dict[str, Number | None]],
    request_id: StrictStr | None = None,
    approval_token: StrictStr | None = None,
) -> dict[str, Any]:
    """Request a mission while armed and flying, with signed human approval.

    waypoints: objects with latitude_deg, longitude_deg, relative_altitude_m
    and optional speed_m_s. The gateway validates every raw field and target.
    Resubmit the exact list with request_id and approval_token unchanged.
    ACKNOWLEDGED means upload/start acceptance, never mission completion;
    observe telemetry. Simulator results are not safety evidence.
    """
    try:
        return (await get_drone().fly_mission(
            waypoints, request_id=request_id, approval_token=approval_token,
        )).to_dict()
    except _HANDLED as exc:
        return _error(exc)


for _tool in (
    get_capabilities,
    get_telemetry,
    get_flight_mode,
    get_connection_status,
    arm,
    disarm,
    takeoff,
    land,
    hold,
    return_to_launch,
    goto_location,
    fly_mission,
):
    mcp.tool(_tool)


def main() -> None:
    """Validate backend selection before exposing tools; never auto-connect."""
    get_drone()
    mcp.run()


if __name__ == "__main__":
    main()
