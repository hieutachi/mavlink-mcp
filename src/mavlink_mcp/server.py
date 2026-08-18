"""MCP server exposing UAV capabilities to AI agents.

Run with the ``mavlink-mcp`` console script (stdio transport).
Configuration is done via environment variables:

- ``MAVLINK_MCP_URL``: MAVLink endpoint (default listens on UDP 14540,
  where PX4 SITL sends offboard traffic).
- ``MAVLINK_MCP_READONLY``: **defaults to true.** While set, every tool
  that can move the vehicle is rejected; telemetry tools keep working.
  Set to ``false`` to enable flight commands.
- ``MAVLINK_MCP_MAX_ALTITUDE_M``: altitude ceiling for takeoff/goto/
  mission targets, in meters above the launch point (default 50; 0
  disables).
- ``MAVLINK_MCP_GEOFENCE_RADIUS_M``: soft geofence radius around the home
  position, in meters (default 200; 0 disables).
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastmcp import FastMCP

from mavlink_mcp.adapter import AdapterError
from mavlink_mcp.capabilities import CapabilityNotSupported, Waypoint
from mavlink_mcp.drone import Drone
from mavlink_mcp.guardrails import GuardrailViolation

mcp = FastMCP(
    "mavlink-mcp",
    instructions=(
        "Vendor-neutral capability layer for a MAVLink drone (PX4/ArduPilot, "
        "simulator-first). Call get_capabilities first to see what the "
        "vehicle supports and which guardrails are active, get_telemetry to "
        "observe it, and arm/takeoff/goto_location/fly_mission/land/"
        "return_to_launch to fly it. Arming and takeoff require explicit "
        "operator-approved confirm=true. The server starts in readonly mode "
        "by default."
    ),
)

_drone: Drone | None = None


def get_drone() -> Drone:
    """Return the shared Drone, creating it (MAVSDK backend) on first use."""
    global _drone
    if _drone is None:
        # Imported lazily so the capability layer stays usable (e.g. in
        # tests) without the MAVSDK backend installed.
        from mavlink_mcp.adapters.mavsdk_adapter import MavsdkAdapter

        _drone = Drone(MavsdkAdapter())
    return _drone


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
    drone = get_drone()
    return {
        "adapter": drone.adapter.name,
        "capabilities": sorted(c.value for c in drone.capabilities()),
        "guardrails": asdict(drone.guardrails),
        "connected": drone.adapter.is_connected(),
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
        snapshot = await get_drone().get_telemetry()
    except _HANDLED as exc:
        return _error(exc)
    return asdict(snapshot)


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
    drone = get_drone()
    status: dict[str, Any] = {
        "adapter": drone.adapter.name,
        "connected": drone.adapter.is_connected(),
        "guardrails": asdict(drone.guardrails),
    }
    address = getattr(drone.adapter, "system_address", None)
    if address is not None:
        status["system_address"] = address
    return status


async def arm(confirm: bool = False) -> dict[str, Any]:
    """Arm the vehicle (spin up motors). DANGEROUS on real hardware.

    Rejected in readonly mode. Additionally requires confirm=true: ask the
    human operator for approval first — never set confirm=true on your own
    initiative. Preflight: check get_telemetry (GPS fix, battery) before
    arming. Returns {"armed": true} or {"error": ...}.
    """
    try:
        await get_drone().arm(confirm=confirm)
    except _HANDLED as exc:
        return _error(exc)
    return {"armed": True}


async def disarm() -> dict[str, Any]:
    """Disarm the vehicle (stop motors). Autopilots reject this in flight.

    Rejected in readonly mode. Returns {"disarmed": true} or
    {"error": ...}.
    """
    try:
        await get_drone().disarm()
    except _HANDLED as exc:
        return _error(exc)
    return {"disarmed": True}


async def takeoff(altitude_m: float = 10.0, confirm: bool = False) -> dict[str, Any]:
    """Take off vertically to `altitude_m` meters above the launch point.

    Rejected in readonly mode. Additionally requires confirm=true after
    human operator approval — never set it on your own initiative. The
    vehicle must be armed first (call arm). Altitude is validated against
    the configured ceiling (MAVLINK_MCP_MAX_ALTITUDE_M, default 50 m).

    Returns {"takeoff_started": true, "target_altitude_m": ...} — takeoff
    is asynchronous; poll get_telemetry to watch the climb.
    """
    try:
        await get_drone().takeoff(float(altitude_m), confirm=confirm)
    except _HANDLED as exc:
        return _error(exc)
    return {"takeoff_started": True, "target_altitude_m": float(altitude_m)}


async def land() -> dict[str, Any]:
    """Land at the current position.

    Rejected in readonly mode (no confirmation needed — landing makes the
    vehicle safer). Returns {"landing_started": true}; poll get_telemetry
    until "in_air" is false.
    """
    try:
        await get_drone().land()
    except _HANDLED as exc:
        return _error(exc)
    return {"landing_started": True}


async def hold() -> dict[str, Any]:
    """Pause: stop and loiter at the current position (HOLD mode).

    Rejected in readonly mode. Use this to interrupt a goto or mission
    safely. Returns {"holding": true}.
    """
    try:
        await get_drone().hold()
    except _HANDLED as exc:
        return _error(exc)
    return {"holding": True}


async def return_to_launch() -> dict[str, Any]:
    """Fly back to the launch point and land there (RTL).

    Rejected in readonly mode (no confirmation needed — RTL is a recovery
    action). Returns {"rtl_started": true}; poll get_telemetry until
    "in_air" is false.
    """
    try:
        await get_drone().return_to_launch()
    except _HANDLED as exc:
        return _error(exc)
    return {"rtl_started": True}


async def goto_location(
    latitude_deg: float,
    longitude_deg: float,
    relative_altitude_m: float,
    yaw_deg: float = 0.0,
) -> dict[str, Any]:
    """Fly to a single waypoint and loiter there. The vehicle must be
    flying already (armed + taken off).

    Args:
        latitude_deg: Target latitude (WGS84).
        longitude_deg: Target longitude (WGS84).
        relative_altitude_m: Target altitude above the launch point, in
            meters. Checked against the altitude ceiling.
        yaw_deg: Heading at the target, 0 = north (default 0).

    Rejected in readonly mode. The target is also checked against the soft
    geofence around home (MAVLINK_MCP_GEOFENCE_RADIUS_M, default 200 m).
    Returns {"goto_started": true, "target": {...}} — travel is
    asynchronous; poll get_telemetry to track progress.
    """
    try:
        await get_drone().goto(
            float(latitude_deg),
            float(longitude_deg),
            float(relative_altitude_m),
            yaw_deg=float(yaw_deg),
        )
    except _HANDLED as exc:
        return _error(exc)
    return {
        "goto_started": True,
        "target": {
            "latitude_deg": float(latitude_deg),
            "longitude_deg": float(longitude_deg),
            "relative_altitude_m": float(relative_altitude_m),
            "yaw_deg": float(yaw_deg),
        },
    }


async def fly_mission(waypoints: list[dict[str, float]]) -> dict[str, Any]:
    """Upload a waypoint mission and start flying it. The vehicle must be
    armed and flying (or the autopilot will take off per its mission
    config).

    Args:
        waypoints: List of {"latitude_deg", "longitude_deg",
            "relative_altitude_m", "speed_m_s"?} objects, flown in order.
            Every waypoint is validated against the altitude ceiling and
            the soft geofence before anything is uploaded.

    Rejected in readonly mode. Returns {"mission_started": true,
    "waypoint_count": N}. Use hold to pause or return_to_launch to abort;
    poll get_flight_mode — mode leaves MISSION when the mission ends.
    """
    try:
        parsed = [
            Waypoint(
                latitude_deg=float(wp["latitude_deg"]),
                longitude_deg=float(wp["longitude_deg"]),
                relative_altitude_m=float(wp["relative_altitude_m"]),
                speed_m_s=(
                    float(wp["speed_m_s"]) if wp.get("speed_m_s") is not None else None
                ),
            )
            for wp in waypoints
        ]
    except (KeyError, TypeError, ValueError) as exc:
        return {
            "error": (
                "Invalid waypoint list: each waypoint needs latitude_deg, "
                f"longitude_deg and relative_altitude_m ({exc!r})."
            )
        }
    try:
        await get_drone().fly_mission(parsed)
    except _HANDLED as exc:
        return _error(exc)
    return {"mission_started": True, "waypoint_count": len(parsed)}


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
    """Console entry point: run the MCP server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
