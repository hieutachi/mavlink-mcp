"""In-process kinematic UAV for MCP integration tests and demonstrations.

No sockets, MAVSDK, physics engine, sensor model, or energy model. The
synthetic WGS84 home is just an origin for a local tangent plane. Motion
uses constant horizontal/vertical speeds and monotonic elapsed time;
observations advance the state analytically, without a background thread.
This is NOT SITL, flight validation, or research evidence about energy.
"""

from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable

from mavlink_mcp.adapter import AdapterError, DroneAdapter, ExecutionLease, LeaseError
from mavlink_mcp.capabilities import (
    Attitude,
    Capability,
    Position,
    TelemetryField,
    TelemetryMetadata,
    TelemetrySnapshot,
    VehicleObservation,
    Waypoint,
    canonical_home_anchor,
)


@dataclass(frozen=True)
class _Segment:
    east_m: float
    north_m: float
    up_m: float
    speed_m_s: float


class SimAdapter(DroneAdapter):
    """One synthetic vehicle. A new process creates a fresh, disarmed UAV.

    Policy stays in the gateway, as with the MAVSDK adapter. Mutators consume
    the same ordered leases; synthetic does not mean authorization-free.
    The injected clock makes motion tests deterministic and sleep-free.
    """

    name = "sim"
    protected = True
    _HOME = Position(47.397742, 8.545594, 488.0, 0.0)
    _EARTH_RADIUS_M = 6_371_000.0
    _HORIZONTAL_SPEED_M_S = 5.0
    _VERTICAL_SPEED_M_S = 2.0

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._last_time = clock()
        self._elapsed_s = 0.0
        self._connected = False
        self._connection_epoch = 0
        self._home_epoch = 0
        self._last_sample: TelemetrySnapshot | None = None
        self._last_home: Position | None = None
        self._home_anchor = None
        self._armed = False
        self._east = self._north = self._up = self._yaw = 0.0
        self._mode = "READY"
        self._segments: deque[_Segment] = deque()
        self._mission_state = "idle"
        self._mission_total = self._mission_completed = 0

    def capabilities(self) -> frozenset[Capability]:
        return frozenset(Capability)

    def is_connected(self) -> bool:
        return self._connected

    @property
    def connection_epoch(self) -> int:
        return self._connection_epoch

    @property
    def home_epoch(self) -> int:
        return self._home_epoch

    async def connect(self) -> None:
        if not self._connected:
            self._last_time = self._clock()
            self._last_sample = None
            self._last_home = None
            self._home_anchor = None
            self._connection_epoch += 1
            self._home_epoch += 1
            self._connected = True

    async def disconnect(self) -> None:
        self._advance()
        self._cancel_plan()
        self._mode = "HOLD" if self._up > 0 else "READY"
        self._connected = False
        self._last_sample = None
        self._last_home = None
        self._home_anchor = None

    def _require_connected(self) -> None:
        if not self._connected:
            raise AdapterError("Simulator is not connected; call connect first.")

    def _require_flying(self) -> None:
        self._require_connected()
        if not self._armed or self._up <= 0:
            raise AdapterError("Simulator: arm and take off before navigation.")

    def _cancel_plan(self) -> None:
        self._segments.clear()
        if self._mission_state == "running":
            self._mission_state = "cancelled"

    def _advance(self) -> None:
        now = self._clock()
        dt = max(0.0, now - self._last_time) if self._connected else 0.0
        self._last_time = now
        self._elapsed_s += dt
        while self._segments:
            target = self._segments[0]
            de = target.east_m - self._east
            dn = target.north_m - self._north
            du = target.up_m - self._up
            duration = max(
                math.hypot(de, dn) / target.speed_m_s,
                abs(du) / self._VERTICAL_SPEED_M_S,
            )
            if duration > dt:
                ratio = dt / duration
                self._east += de * ratio
                self._north += dn * ratio
                self._up += du * ratio
                break
            self._east, self._north, self._up = (
                target.east_m, target.north_m, target.up_m
            )
            dt -= duration
            self._segments.popleft()
            if self._mode == "MISSION":
                self._mission_completed += 1
            if not self._segments:
                if self._mode in {"LAND", "RTL"}:
                    self._up = 0.0
                    self._armed = False
                    self._mode = "READY"
                else:
                    if self._mode == "MISSION":
                        self._mission_state = "completed"
                    self._mode = "HOLD"

    def _position(self) -> Position:
        return Position(
            latitude_deg=self._HOME.latitude_deg + math.degrees(
                self._north / self._EARTH_RADIUS_M
            ),
            longitude_deg=self._HOME.longitude_deg + math.degrees(
                self._east / (
                    self._EARTH_RADIUS_M
                    * math.cos(math.radians(self._HOME.latitude_deg))
                )
            ),
            absolute_altitude_m=self._HOME.absolute_altitude_m + self._up,
            relative_altitude_m=self._up,
        )

    def _segment(self, waypoint: Waypoint) -> _Segment:
        return _Segment(
            east_m=math.radians(waypoint.longitude_deg - self._HOME.longitude_deg)
            * self._EARTH_RADIUS_M * math.cos(math.radians(self._HOME.latitude_deg)),
            north_m=math.radians(waypoint.latitude_deg - self._HOME.latitude_deg)
            * self._EARTH_RADIUS_M,
            up_m=waypoint.relative_altitude_m,
            speed_m_s=(waypoint.speed_m_s if waypoint.speed_m_s is not None
                       else self._HORIZONTAL_SPEED_M_S),
        )

    def _validate_segments(self, segments: list[_Segment]) -> None:
        east, north, up = self._east, self._north, self._up
        for segment in segments:
            duration = max(
                math.hypot(segment.east_m - east, segment.north_m - north)
                / segment.speed_m_s,
                abs(segment.up_m - up) / self._VERTICAL_SPEED_M_S,
            )
            if not math.isfinite(duration):
                raise AdapterError("Simulator: segment duration is not representable; "
                                   "choose a practical finite speed and distance.")
            east, north, up = segment.east_m, segment.north_m, segment.up_m

    def simulation_info(self, *, advance: bool = True) -> dict:
        if advance:
            self._advance()
        return {
            "synthetic": True,
            "model": "kinematic-local-tangent-plane",
            "time_model": "monotonic-wall-clock; observations advance elapsed motion",
            "elapsed_s": self._elapsed_s,
            "local_position_enu_m": [self._east, self._north, self._up],
            "home": {
                "latitude_deg": self._HOME.latitude_deg,
                "longitude_deg": self._HOME.longitude_deg,
                "absolute_altitude_m": self._HOME.absolute_altitude_m,
            },
            "mission": {
                "state": self._mission_state,
                "completed_waypoints": self._mission_completed,
                "total_waypoints": self._mission_total,
            },
            "network_enabled": False,
            "energy_model": None,
            "limitations": "Not SITL. No dynamics, terrain, wind, collision, camera, "
                           "battery or GPS sensor model. Not research validation.",
        }

    def safety_snapshot(self) -> VehicleObservation:
        """Return the last generated sample without even consulting the clock."""
        sample = self._last_sample or TelemetrySnapshot(
            None, None, None, None, None, None, None,
            TelemetryMetadata(0.0, "SYNTHETIC", self._connection_epoch,
                              self._home_epoch, synthetic=True),
        )
        return VehicleObservation(sample, self._last_home, self._home_anchor,
                                  self._connected, self._connected)

    async def get_telemetry(self) -> TelemetrySnapshot:
        """Evaluate a new analytic observation, including while stationary."""
        self._require_connected()
        self._advance()
        now = self._last_time
        fields = {
            name: TelemetryField(now, "SYNTHETIC", self._connection_epoch,
                                 home_epoch=self._home_epoch if name == "home" else None)
            for name in ("position", "attitude", "flight_mode", "armed", "in_air", "home")
        }
        self._last_sample = TelemetrySnapshot(
            position=self._position(),
            attitude=Attitude(0.0, 0.0, self._yaw),
            battery=None,
            gps=None,
            flight_mode=self._mode,
            armed=self._armed,
            in_air=self._up > 0,
            metadata=TelemetryMetadata(now, "SYNTHETIC", self._connection_epoch, self._home_epoch, fields, synthetic=True),
        )
        self._last_home = self._HOME
        self._home_anchor = canonical_home_anchor(self._HOME, self._home_epoch)
        return self._last_sample

    async def get_flight_mode(self) -> str:
        self._require_connected()
        if self._last_sample is None:
            raise AdapterError("Simulator has not generated a telemetry sample.")
        return self._last_sample.flight_mode

    async def get_home(self) -> Position | None:
        self._require_connected()
        return self._last_home

    async def arm(self, *, lease: ExecutionLease | None = None) -> None:
        self._require_connected()
        self._advance()
        self._consume_lease(lease, "arm")
        self._armed = True

    async def disarm(self, *, lease: ExecutionLease | None = None) -> None:
        self._require_connected()
        self._advance()
        if self._up > 0:
            raise AdapterError("Simulator rejects disarm in air; use hold, land or RTL.")
        self._consume_lease(lease, "disarm")
        self._cancel_plan()
        self._armed = False
        self._mode = "READY"

    async def takeoff(self, altitude_m: float, *, lease: ExecutionLease | None = None) -> None:
        self._require_connected()
        self._advance()
        if not self._armed:
            raise AdapterError("Simulator: arm before takeoff.")
        if self._up > 0 or self._segments:
            raise AdapterError("Simulator: already airborne or takeoff is in progress.")
        segment = _Segment(self._east, self._north, altitude_m, self._HORIZONTAL_SPEED_M_S)
        self._consume_lease(lease, "set_takeoff_altitude", {"altitude_m": altitude_m})
        self._consume_lease(lease, "takeoff")
        self._cancel_plan()
        self._segments.append(segment)
        self._mode = "TAKEOFF"

    async def hold(self, *, lease: ExecutionLease | None = None) -> None:
        self._require_connected()
        self._advance()
        self._consume_lease(lease, "hold")
        self._cancel_plan()
        self._mode = "HOLD" if self._up > 0 else "READY"

    async def land(self, *, lease: ExecutionLease | None = None) -> None:
        self._require_connected()
        self._advance()
        self._consume_lease(lease, "land")
        self._cancel_plan()
        self._segments.append(_Segment(
            self._east, self._north, 0.0, self._HORIZONTAL_SPEED_M_S
        ))
        self._mode = "LAND"
        self._advance()

    async def return_to_launch(self, *, lease: ExecutionLease | None = None) -> None:
        self._require_connected()
        self._advance()
        self._consume_lease(lease, "return_to_launch")
        self._cancel_plan()
        if self._up <= 0:
            # Complete the ground transition directly, never consume a second
            # public land sink under an RTL lease.
            self._up = 0.0
            self._armed = False
            self._mode = "READY"
            return
        # Flat synthetic world only: return at current altitude, then descend.
        self._segments.extend([
            _Segment(0.0, 0.0, self._up, self._HORIZONTAL_SPEED_M_S),
            _Segment(0.0, 0.0, 0.0, self._HORIZONTAL_SPEED_M_S),
        ])
        self._mode = "RTL"

    async def goto(self, waypoint: Waypoint, yaw_deg: float = 0.0, *, lease: ExecutionLease | None = None, absolute_altitude_m: float | None = None) -> None:
        self._require_connected()
        if absolute_altitude_m is None:
            raise LeaseError("goto requires the gateway-resolved absolute altitude")
        self._advance()
        self._require_flying()
        if not math.isclose(absolute_altitude_m, self._HOME.absolute_altitude_m + waypoint.relative_altitude_m,
                            rel_tol=0.0, abs_tol=1e-6):
            raise AdapterError("Simulator: absolute altitude does not match the home-relative target.")
        segment = self._segment(waypoint)
        self._validate_segments([segment])
        self._consume_lease(lease, "goto_location", {
            "waypoint": waypoint, "yaw_deg": yaw_deg,
            "absolute_altitude_m": absolute_altitude_m,
        })
        self._cancel_plan()
        self._segments.append(segment)
        self._yaw = yaw_deg % 360.0
        self._mode = "GOTO"

    async def fly_mission(self, waypoints: list[Waypoint], *, lease: ExecutionLease | None = None) -> None:
        self._require_connected()
        self._advance()
        self._require_flying()
        segments = [self._segment(wp) for wp in waypoints]
        self._validate_segments(segments)
        self._consume_lease(lease, "upload_mission", {"waypoints": tuple(waypoints)})
        self._consume_lease(lease, "start_mission")
        self._cancel_plan()
        self._segments.extend(segments)
        self._mission_total = len(segments)
        self._mission_completed = 0
        self._mission_state = "running"
        self._mode = "MISSION"
