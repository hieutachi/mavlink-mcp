"""The single shared instrument both arms are measured with.

``SpyAdapter`` is the ONLY place physical effects are recorded. It is modeled
on ``tests/conftest.py:MockAdapter`` (same ``sinks`` list, same ``observe()``
and ``safety_snapshot()`` contract, same ten sink names) but is written here
rather than imported, so the benchmark never depends on test fixtures.

Two entry surfaces, one recorder:

  * protected sinks (``arm``, ``takeoff``, ...) require a gateway
    ``ExecutionLease``, used by the CURRENT SafeMAV arm;
  * unprotected ``baseline_*`` sinks take no lease, used by the BASELINE arm
    (the 962448c adapter ABC had no lease parameter).

Both surfaces append to the same ``self.sinks`` list and mutate the same
simulated vehicle state. That is what makes the comparison fair: prevention is
decided by this list and this state, never by a return value or an exception.

Ten physical sinks, matching src/mavlink_mcp/safety/admission.py:SINKS and the
ten MAVSDK calls in adapters/mavsdk_adapter.py:
  arm, disarm, set_takeoff_altitude, takeoff, land, hold, return_to_launch,
  goto_location, upload_mission, start_mission
"""

from __future__ import annotations

import time
from dataclasses import replace

from mavlink_mcp.adapter import DroneAdapter
from mavlink_mcp.capabilities import (
    Attitude,
    Battery,
    Capability,
    GpsInfo,
    Position,
    TelemetryField,
    TelemetryMetadata,
    TelemetrySnapshot,
    VehicleObservation,
    Waypoint,
    canonical_home_anchor,
)
from mavlink_mcp.safety.monitor import valid_observation_value

HOME = Position(
    latitude_deg=47.397742,
    longitude_deg=8.545594,
    absolute_altitude_m=488.0,
    relative_altitude_m=0.0,
)

#: Every physical sink either arm can possibly reach.
ALL_SINKS = (
    "arm",
    "disarm",
    "set_takeoff_altitude",
    "takeoff",
    "land",
    "hold",
    "return_to_launch",
    "goto_location",
    "upload_mission",
    "start_mission",
)

#: Sinks that change simulated vehicle state in a flight-relevant way.
STATE_CHANGING_SINKS = frozenset(
    {"arm", "disarm", "takeoff", "land", "goto_location", "start_mission"}
)


class SpyAdapter(DroneAdapter):
    """Records which physical sinks actually fired, for both arms."""

    name = "mock"
    protected = True

    def __init__(self, capabilities=None, home: Position | None = HOME) -> None:
        self._capabilities = (
            capabilities if capabilities is not None else frozenset(Capability)
        )
        self.home = home
        self.clock = time.monotonic
        self.connection_epoch = 1
        self.home_epoch = 1
        self.monitor_healthy = True
        self.refresh = True
        #: [(sink_name, parameters)] - the decisive measurement.
        self.sinks: list[tuple] = []
        self.after_sink = None
        self.observation = None
        self.connected = False
        self.calls: list[tuple] = []
        self.telemetry = TelemetrySnapshot(
            position=Position(47.397742, 8.545594, 488.0, 0.0),
            attitude=Attitude(0.1, -0.2, 90.0),
            battery=Battery(12.6, 100.0),
            gps=GpsInfo(10, "FIX_3D"),
            flight_mode="HOLD",
            armed=False,
            in_air=False,
        )

    # ----------------------------- state ------------------------------ #

    def vehicle_state(self) -> dict:
        """The simulated physical state used for the prevention oracle."""
        return {
            "armed": self.telemetry.armed,
            "in_air": self.telemetry.in_air,
            "flight_mode": self.telemetry.flight_mode,
            "takeoff_altitude_m": getattr(self, "commanded_altitude_m", None),
            "goto_target": getattr(self, "commanded_goto", None),
            "mission_uploaded": getattr(self, "uploaded_mission", None),
            "mission_started": getattr(self, "mission_started", False),
        }

    def sink_names(self) -> list[str]:
        return [sink for sink, _ in self.sinks]

    def reset_sinks(self) -> None:
        self.sinks = []

    # --------------------------- telemetry ---------------------------- #

    def capabilities(self):
        return self._capabilities

    def is_connected(self) -> bool:
        return self.connected

    async def connect(self) -> None:
        self.connected = True
        self.calls.append(("connect",))

    async def disconnect(self) -> None:
        self.connected = False
        self.calls.append(("disconnect",))

    async def get_telemetry(self) -> TelemetrySnapshot:
        self.calls.append(("get_telemetry",))
        if self.refresh:
            self.observe()
        return self.telemetry

    def observe(self):
        """Refresh the observation cache, exactly like MockAdapter.observe."""
        now = self.clock()
        values = {
            name: getattr(self.telemetry, name)
            for name in (
                "position",
                "attitude",
                "battery",
                "gps",
                "flight_mode",
                "armed",
                "in_air",
            )
        }
        values["home"] = self.home
        source = "SYNTHETIC"
        fields = {
            name: TelemetryField(
                now,
                source,
                self.connection_epoch,
                valid_observation_value(name, value),
                home_epoch=self.home_epoch if name == "home" else None,
            )
            for name, value in values.items()
            if value is not None
        }
        self.telemetry = replace(
            self.telemetry,
            metadata=TelemetryMetadata(
                now, source, self.connection_epoch, self.home_epoch, fields, True
            ),
        )
        self.observation = VehicleObservation(
            self.telemetry,
            self.home,
            canonical_home_anchor(self.home, self.home_epoch) if self.home else None,
            self.connected,
            self.monitor_healthy,
        )

    def safety_snapshot(self):
        return self.observation

    async def get_flight_mode(self) -> str:
        self.calls.append(("get_flight_mode",))
        return self.telemetry.flight_mode or "UNKNOWN"

    async def get_home(self) -> Position | None:
        self.calls.append(("get_home",))
        return self.home

    # ---------------------- the one recorder -------------------------- #

    def _record(self, sink: str, parameters=None) -> None:
        """Single chokepoint: both arms land here, and this is the metric."""
        self.sinks.append((sink, parameters))
        if sink == "arm":
            self.telemetry = replace(self.telemetry, armed=True)
        elif sink == "disarm":
            self.telemetry = replace(self.telemetry, armed=False)
        elif sink == "set_takeoff_altitude":
            self.commanded_altitude_m = (parameters or {}).get("altitude_m")
        elif sink == "takeoff":
            self.telemetry = replace(self.telemetry, in_air=True)
        elif sink == "land":
            self.telemetry = replace(self.telemetry, in_air=False)
        elif sink == "goto_location":
            wp = (parameters or {}).get("waypoint")
            self.commanded_goto = (
                None
                if wp is None
                else (wp.latitude_deg, wp.longitude_deg, wp.relative_altitude_m)
            )
        elif sink == "upload_mission":
            points = (parameters or {}).get("waypoints") or ()
            self.uploaded_mission = tuple(
                (w.latitude_deg, w.longitude_deg, w.relative_altitude_m) for w in points
            )
        elif sink == "start_mission":
            self.mission_started = True

    # -------------- protected surface (current SafeMAV arm) ----------- #

    async def _write(self, lease, sink, parameters=None):
        self._consume_lease(lease, sink, parameters)
        self._record(sink, parameters)
        if self.after_sink is not None:
            await self.after_sink(sink)

    async def arm(self, *, lease=None) -> None:
        await self._write(lease, "arm")
        self.calls.append(("arm", lease))

    async def disarm(self, *, lease=None) -> None:
        await self._write(lease, "disarm")
        self.calls.append(("disarm", lease))

    async def takeoff(self, altitude_m: float, *, lease=None) -> None:
        await self._write(lease, "set_takeoff_altitude", {"altitude_m": altitude_m})
        await self._write(lease, "takeoff")
        self.calls.append(("takeoff", altitude_m, lease))

    async def land(self, *, lease=None) -> None:
        await self._write(lease, "land")
        self.calls.append(("land", lease))

    async def hold(self, *, lease=None) -> None:
        await self._write(lease, "hold")
        self.calls.append(("hold", lease))

    async def return_to_launch(self, *, lease=None) -> None:
        await self._write(lease, "return_to_launch")
        self.calls.append(("return_to_launch", lease))

    async def goto(
        self, waypoint: Waypoint, yaw_deg: float = 0.0, *, lease=None,
        absolute_altitude_m=None,
    ) -> None:
        await self._write(
            lease,
            "goto_location",
            {
                "waypoint": waypoint,
                "yaw_deg": yaw_deg,
                "absolute_altitude_m": absolute_altitude_m,
            },
        )
        self.calls.append(("goto", waypoint, yaw_deg, lease, absolute_altitude_m))

    async def fly_mission(self, waypoints, *, lease=None) -> None:
        await self._write(lease, "upload_mission", {"waypoints": tuple(waypoints)})
        await self._write(lease, "start_mission")
        self.calls.append(("fly_mission", tuple(waypoints), lease))

    # ------- unprotected surface (962448c baseline arm) --------------- #
    # The historical DroneAdapter ABC had no `lease=` parameter; these are
    # the same sinks reached the way the baseline design reached them. The
    # `after_sink` fault hook fires here too, so a backend fault injected in a
    # scenario hits both arms identically.

    async def _record_unprotected(self, sink: str, parameters=None) -> None:
        self._record(sink, parameters)
        if self.after_sink is not None:
            await self.after_sink(sink)

    async def baseline_arm(self) -> None:
        await self._record_unprotected("arm")
        self.calls.append(("baseline_arm",))

    async def baseline_disarm(self) -> None:
        await self._record_unprotected("disarm")
        self.calls.append(("baseline_disarm",))

    async def baseline_takeoff(self, altitude_m: float) -> None:
        await self._record_unprotected("set_takeoff_altitude",
                                       {"altitude_m": altitude_m})
        await self._record_unprotected("takeoff")
        self.calls.append(("baseline_takeoff", altitude_m))

    async def baseline_land(self) -> None:
        await self._record_unprotected("land")
        self.calls.append(("baseline_land",))

    async def baseline_hold(self) -> None:
        await self._record_unprotected("hold")
        self.calls.append(("baseline_hold",))

    async def baseline_return_to_launch(self) -> None:
        await self._record_unprotected("return_to_launch")
        self.calls.append(("baseline_return_to_launch",))

    async def baseline_goto(self, waypoint, yaw_deg: float = 0.0) -> None:
        await self._record_unprotected(
            "goto_location", {"waypoint": waypoint, "yaw_deg": yaw_deg})
        self.calls.append(("baseline_goto", waypoint, yaw_deg))

    async def baseline_fly_mission(self, waypoints) -> None:
        await self._record_unprotected("upload_mission",
                                       {"waypoints": tuple(waypoints)})
        await self._record_unprotected("start_mission")
        self.calls.append(("baseline_fly_mission", tuple(waypoints)))
