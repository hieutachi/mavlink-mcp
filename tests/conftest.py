"""Shared fixtures: a mock adapter so no SITL is needed to run tests."""

from __future__ import annotations

import pytest

from mavlink_mcp.adapter import DroneAdapter
from mavlink_mcp.capabilities import (
    Attitude,
    Battery,
    Capability,
    GpsInfo,
    Position,
    TelemetrySnapshot,
    Waypoint,
)
from mavlink_mcp.drone import Drone
from mavlink_mcp.guardrails import GuardrailConfig

HOME = Position(
    latitude_deg=47.397742,
    longitude_deg=8.545594,
    absolute_altitude_m=488.0,
    relative_altitude_m=0.0,
)


class MockAdapter(DroneAdapter):
    """In-memory DroneAdapter that records every call."""

    name = "mock"

    def __init__(
        self,
        capabilities: frozenset[Capability] | None = None,
        home: Position | None = HOME,
    ) -> None:
        self._capabilities = (
            capabilities
            if capabilities is not None
            else frozenset(Capability)
        )
        self.home = home
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

    def capabilities(self) -> frozenset[Capability]:
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
        return self.telemetry

    async def get_flight_mode(self) -> str:
        self.calls.append(("get_flight_mode",))
        return self.telemetry.flight_mode or "UNKNOWN"

    async def get_home(self) -> Position | None:
        self.calls.append(("get_home",))
        return self.home

    async def arm(self) -> None:
        self.calls.append(("arm",))

    async def disarm(self) -> None:
        self.calls.append(("disarm",))

    async def takeoff(self, altitude_m: float) -> None:
        self.calls.append(("takeoff", altitude_m))

    async def land(self) -> None:
        self.calls.append(("land",))

    async def hold(self) -> None:
        self.calls.append(("hold",))

    async def return_to_launch(self) -> None:
        self.calls.append(("return_to_launch",))

    async def goto(self, waypoint: Waypoint, yaw_deg: float = 0.0) -> None:
        self.calls.append(("goto", waypoint, yaw_deg))

    async def fly_mission(self, waypoints: list[Waypoint]) -> None:
        self.calls.append(("fly_mission", tuple(waypoints)))

    def call_names(self) -> list[str]:
        return [call[0] for call in self.calls]


PERMISSIVE = GuardrailConfig(
    readonly=False, max_altitude_m=100.0, geofence_radius_m=500.0
)


@pytest.fixture
def adapter() -> MockAdapter:
    return MockAdapter()


@pytest.fixture
def drone(adapter: MockAdapter) -> Drone:
    """A Drone with write access and generous (but active) limits."""
    return Drone(adapter, guardrails=PERMISSIVE)


@pytest.fixture
def readonly_drone(adapter: MockAdapter) -> Drone:
    """A Drone with default guardrails (readonly)."""
    return Drone(adapter, guardrails=GuardrailConfig())
