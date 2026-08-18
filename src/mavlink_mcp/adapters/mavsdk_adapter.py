"""MAVLink backend built on MAVSDK-Python (BSD-3-Clause).

Talks MAVLink to PX4 SITL, ArduPilot SITL, or any MAVLink-speaking
autopilot via the MAVSDK library. This module is deliberately the *only*
place in the project that imports ``mavsdk`` — the capability layer and
the MCP server never see protocol types.

Licensing note: the core intentionally depends on MAVSDK-Python (BSD-3)
and not on pymavlink (LGPL-3), keeping the whole dependency tree
permissive under the project's MIT license.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from importlib.metadata import PackageNotFoundError, version
from typing import Any, AsyncIterator

from mavsdk import System
from mavsdk.mission import MissionItem, MissionPlan

from mavlink_mcp.adapter import AdapterError, DroneAdapter
from mavlink_mcp.capabilities import (
    Attitude,
    Battery,
    Capability,
    GpsInfo,
    Position,
    TelemetrySnapshot,
    Waypoint,
)

_TELEMETRY_FIELD_TIMEOUT_S = 5.0
_DEFAULT_CONNECT_TIMEOUT_S = 30.0
_DEFAULT_MISSION_SPEED_M_S = 5.0


def default_system_address() -> str:
    """Default MAVLink endpoint: listen on UDP 14540 (PX4 SITL's offboard
    port). MAVSDK changed the URL scheme in v3, so pick the right syntax
    for the installed version."""
    try:
        major = int(version("mavsdk").split(".")[0])
    except (PackageNotFoundError, ValueError):
        major = 3
    if major >= 3:
        return "udpin://0.0.0.0:14540"
    return "udp://:14540"


async def _first(stream: AsyncIterator[Any], timeout_s: float) -> Any:
    """Return the first item of a MAVSDK telemetry stream, or None."""
    iterator = stream.__aiter__()
    try:
        return await asyncio.wait_for(iterator.__anext__(), timeout_s)
    except (StopAsyncIteration, asyncio.TimeoutError):
        return None
    finally:
        with contextlib.suppress(Exception):
            await iterator.aclose()


class MavsdkAdapter(DroneAdapter):
    """DroneAdapter implementation backed by MAVSDK-Python."""

    name = "mavsdk"

    _CAPABILITIES = frozenset(
        {
            Capability.TELEMETRY,
            Capability.FLIGHT_MODE,
            Capability.ARM_DISARM,
            Capability.TAKEOFF,
            Capability.LAND,
            Capability.GOTO,
            Capability.MISSION,
            Capability.RETURN_TO_LAUNCH,
        }
    )

    def __init__(
        self,
        system_address: str | None = None,
        connect_timeout_s: float = _DEFAULT_CONNECT_TIMEOUT_S,
    ) -> None:
        self._system_address = (
            system_address
            or os.environ.get("MAVLINK_MCP_URL", "").strip()
            or default_system_address()
        )
        self._connect_timeout_s = connect_timeout_s
        self._system: System | None = None
        self._connected = False

    @property
    def system_address(self) -> str:
        return self._system_address

    def capabilities(self) -> frozenset[Capability]:
        return self._CAPABILITIES

    def is_connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        if self._connected:
            return
        system = System()
        try:
            await system.connect(system_address=self._system_address)

            async def _wait_discovered() -> None:
                async for state in system.core.connection_state():
                    if state.is_connected:
                        return

            await asyncio.wait_for(
                _wait_discovered(), timeout=self._connect_timeout_s
            )
        except asyncio.TimeoutError:
            raise AdapterError(
                f"No vehicle discovered on {self._system_address} within "
                f"{self._connect_timeout_s}s. Is the SITL/autopilot running "
                "and sending MAVLink to this endpoint? (Set MAVLINK_MCP_URL "
                "to change the endpoint.)"
            ) from None
        except Exception as exc:
            raise AdapterError(
                f"Failed to connect to {self._system_address}: {exc}"
            ) from exc
        self._system = system
        self._connected = True

    async def disconnect(self) -> None:
        self._system = None
        self._connected = False

    def _require_system(self) -> System:
        if self._system is None:
            raise AdapterError("Not connected — call connect() first.")
        return self._system

    # --- Telemetry -------------------------------------------------- #

    async def get_telemetry(self) -> TelemetrySnapshot:
        system = self._require_system()
        t = _TELEMETRY_FIELD_TIMEOUT_S
        position, attitude, battery, gps, mode, armed, in_air = (
            await asyncio.gather(
                _first(system.telemetry.position(), t),
                _first(system.telemetry.attitude_euler(), t),
                _first(system.telemetry.battery(), t),
                _first(system.telemetry.gps_info(), t),
                _first(system.telemetry.flight_mode(), t),
                _first(system.telemetry.armed(), t),
                _first(system.telemetry.in_air(), t),
            )
        )
        return TelemetrySnapshot(
            position=_to_position(position),
            attitude=(
                Attitude(attitude.roll_deg, attitude.pitch_deg, attitude.yaw_deg)
                if attitude is not None
                else None
            ),
            battery=(
                Battery(battery.voltage_v, battery.remaining_percent)
                if battery is not None
                else None
            ),
            gps=(
                GpsInfo(gps.num_satellites, _enum_name(gps.fix_type))
                if gps is not None
                else None
            ),
            flight_mode=_enum_name(mode) if mode is not None else None,
            armed=armed,
            in_air=in_air,
        )

    async def get_flight_mode(self) -> str:
        system = self._require_system()
        mode = await _first(
            system.telemetry.flight_mode(), _TELEMETRY_FIELD_TIMEOUT_S
        )
        if mode is None:
            raise AdapterError(
                "The vehicle did not report a flight mode within "
                f"{_TELEMETRY_FIELD_TIMEOUT_S}s."
            )
        return _enum_name(mode)

    async def get_home(self) -> Position | None:
        system = self._require_system()
        home = await _first(
            system.telemetry.home(), _TELEMETRY_FIELD_TIMEOUT_S
        )
        return _to_position(home)

    # --- Actions ----------------------------------------------------- #

    async def arm(self) -> None:
        await self._action("arm", self._require_system().action.arm())

    async def disarm(self) -> None:
        await self._action("disarm", self._require_system().action.disarm())

    async def takeoff(self, altitude_m: float) -> None:
        system = self._require_system()
        await self._action(
            "set_takeoff_altitude",
            system.action.set_takeoff_altitude(float(altitude_m)),
        )
        await self._action("takeoff", system.action.takeoff())

    async def land(self) -> None:
        await self._action("land", self._require_system().action.land())

    async def hold(self) -> None:
        await self._action("hold", self._require_system().action.hold())

    async def return_to_launch(self) -> None:
        await self._action(
            "return_to_launch",
            self._require_system().action.return_to_launch(),
        )

    async def goto(self, waypoint: Waypoint, yaw_deg: float = 0.0) -> None:
        system = self._require_system()
        home = await self.get_home()
        if home is None:
            raise AdapterError(
                "Cannot goto: the vehicle has not reported a home position, "
                "so the target's absolute altitude cannot be computed. "
                "Wait for a GPS fix."
            )
        absolute_altitude_m = (
            home.absolute_altitude_m + waypoint.relative_altitude_m
        )
        await self._action(
            "goto_location",
            system.action.goto_location(
                waypoint.latitude_deg,
                waypoint.longitude_deg,
                absolute_altitude_m,
                float(yaw_deg),
            ),
        )

    async def fly_mission(self, waypoints: list[Waypoint]) -> None:
        system = self._require_system()
        items = [_mission_item(wp) for wp in waypoints]
        await self._action(
            "upload_mission",
            system.mission.upload_mission(MissionPlan(items)),
        )
        await self._action(
            "start_mission", system.mission.start_mission()
        )

    @staticmethod
    async def _action(operation: str, coro: Any) -> None:
        """Await a MAVSDK call, wrapping backend failures in AdapterError."""
        try:
            await coro
        except Exception as exc:
            raise AdapterError(f"{operation} failed: {exc}") from exc


def _enum_name(value: Any) -> str:
    """Short readable name for a MAVSDK enum value, e.g. 'HOLD'."""
    return getattr(value, "name", str(value))


def _to_position(position: Any) -> Position | None:
    if position is None:
        return None
    return Position(
        latitude_deg=position.latitude_deg,
        longitude_deg=position.longitude_deg,
        absolute_altitude_m=position.absolute_altitude_m,
        relative_altitude_m=position.relative_altitude_m,
    )


def _mission_item(waypoint: Waypoint) -> MissionItem:
    nan = float("nan")
    kwargs = dict(
        latitude_deg=waypoint.latitude_deg,
        longitude_deg=waypoint.longitude_deg,
        relative_altitude_m=waypoint.relative_altitude_m,
        speed_m_s=(
            waypoint.speed_m_s
            if waypoint.speed_m_s is not None
            else _DEFAULT_MISSION_SPEED_M_S
        ),
        is_fly_through=True,
        gimbal_pitch_deg=nan,
        gimbal_yaw_deg=nan,
        camera_action=MissionItem.CameraAction.NONE,
        loiter_time_s=nan,
        camera_photo_interval_s=nan,
        acceptance_radius_m=nan,
        yaw_deg=nan,
        camera_photo_distance_m=nan,
    )
    # vehicle_action was added to MissionItem in newer MAVSDK versions.
    if hasattr(MissionItem, "VehicleAction"):
        try:
            return MissionItem(
                **kwargs, vehicle_action=MissionItem.VehicleAction.NONE
            )
        except TypeError:
            pass
    return MissionItem(**kwargs)
