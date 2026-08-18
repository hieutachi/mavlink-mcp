"""Capability layer behavior against the mock adapter (no SITL needed)."""

from __future__ import annotations

import pytest

from mavlink_mcp.capabilities import (
    Capability,
    CapabilityNotSupported,
    Waypoint,
)
from mavlink_mcp.drone import Drone
from tests.conftest import PERMISSIVE, MockAdapter


async def test_capability_discovery(drone, adapter):
    assert drone.capabilities() == frozenset(Capability)
    assert Capability.TAKEOFF in drone.capabilities()


async def test_telemetry_roundtrip(drone, adapter):
    snapshot = await drone.get_telemetry()
    assert snapshot.position.latitude_deg == pytest.approx(47.397742)
    assert snapshot.battery.remaining_percent == 100.0
    assert snapshot.gps.fix_type == "FIX_3D"
    assert snapshot.armed is False


async def test_connects_lazily_once(drone, adapter):
    assert not adapter.connected
    await drone.get_telemetry()
    await drone.get_telemetry()
    assert adapter.connected
    assert adapter.call_names().count("connect") == 1


async def test_get_flight_mode(drone):
    assert await drone.get_flight_mode() == "HOLD"


async def test_arm_with_confirmation_reaches_adapter(drone, adapter):
    await drone.arm(confirm=True)
    assert "arm" in adapter.call_names()


async def test_takeoff_passes_altitude(drone, adapter):
    await drone.takeoff(20.0, confirm=True)
    assert ("takeoff", 20.0) in adapter.calls


async def test_goto_passes_waypoint_and_yaw(drone, adapter):
    await drone.goto(47.3980, 8.5460, 25.0, yaw_deg=90.0)
    name, waypoint, yaw = adapter.calls[-1]
    assert name == "goto"
    assert waypoint == Waypoint(47.3980, 8.5460, 25.0)
    assert yaw == 90.0


async def test_fly_mission_passes_waypoints(drone, adapter):
    waypoints = [
        Waypoint(47.3980, 8.5460, 20.0),
        Waypoint(47.3985, 8.5465, 25.0, speed_m_s=3.0),
    ]
    await drone.fly_mission(waypoints)
    assert adapter.calls[-1] == ("fly_mission", tuple(waypoints))


async def test_simple_safety_actions(drone, adapter):
    await drone.land()
    await drone.hold()
    await drone.return_to_launch()
    await drone.disarm()
    for name in ("land", "hold", "return_to_launch", "disarm"):
        assert name in adapter.call_names()


async def test_missing_capability_rejected():
    adapter = MockAdapter(capabilities=frozenset({Capability.TELEMETRY}))
    drone = Drone(adapter, guardrails=PERMISSIVE)
    await drone.get_telemetry()  # supported
    with pytest.raises(CapabilityNotSupported):
        await drone.takeoff(10.0, confirm=True)
    assert "takeoff" not in adapter.call_names()
