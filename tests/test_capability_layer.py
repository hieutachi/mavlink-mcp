"""Capability behavior through the signed gateway (no SITL needed)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from mavlink_mcp.capabilities import Capability, CapabilityNotSupported, Waypoint
from tests.conftest import MockAdapter


async def test_capability_discovery(drone, adapter):
    assert drone.capabilities() == frozenset(Capability)
    assert Capability.TAKEOFF in drone.capabilities()
    assert adapter.protected is True


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


async def test_arm_with_signed_approval_reaches_adapter(rig, drone, adapter):
    await rig.run(drone.arm)
    assert adapter.sinks == [("arm", None)]
    assert adapter.telemetry.armed is True


async def test_takeoff_passes_altitude(rig, drone, adapter):
    adapter.telemetry = replace(adapter.telemetry, armed=True, in_air=False)
    await rig.run(drone.takeoff, 20.0)
    assert adapter.sinks == [
        ("set_takeoff_altitude", {"altitude_m": 20.0}),
        ("takeoff", None),
    ]
    assert any(call[0] == "takeoff" and call[1] == 20.0 for call in adapter.calls)
    assert adapter.telemetry.in_air is True


async def test_goto_passes_waypoint_and_yaw(rig, drone, adapter):
    adapter.telemetry = replace(adapter.telemetry, armed=True, in_air=True)
    await rig.run(drone.goto, 47.3980, 8.5460, 25.0, yaw_deg=90.0)
    name, waypoint, yaw, _lease, absolute_altitude = next(
        call for call in adapter.calls if call[0] == "goto"
    )
    assert name == "goto"
    assert waypoint == Waypoint(47.3980, 8.5460, 25.0)
    assert yaw == 90.0
    assert absolute_altitude == pytest.approx(513.0)
    assert adapter.sinks == [("goto_location", {
        "waypoint": waypoint, "yaw_deg": 90.0, "absolute_altitude_m": 513.0,
    })]


async def test_fly_mission_passes_waypoints(rig, drone, adapter):
    adapter.telemetry = replace(adapter.telemetry, armed=True, in_air=True)
    waypoints = [
        Waypoint(47.3980, 8.5460, 20.0),
        Waypoint(47.3985, 8.5465, 25.0, speed_m_s=3.0),
    ]
    await rig.run(drone.fly_mission, waypoints)
    # The default speed is pinned in the signed plan, not left to the adapter.
    expected = (replace(waypoints[0], speed_m_s=5.0), waypoints[1])
    assert next(call for call in adapter.calls if call[0] == "fly_mission")[:2] == (
        "fly_mission", expected,
    )
    assert adapter.sinks == [
        ("upload_mission", {"waypoints": expected}), ("start_mission", None),
    ]


async def test_simple_safety_actions(rig, drone, adapter):
    for method in (drone.land, drone.hold, drone.return_to_launch, drone.disarm):
        await rig.run(method)
    assert adapter.sinks == [
        ("land", None), ("hold", None), ("return_to_launch", None), ("disarm", None),
    ]


async def test_missing_capability_rejected(rig_factory):
    adapter = MockAdapter(capabilities=frozenset({Capability.TELEMETRY}))
    drone = rig_factory(adapter).drone
    await drone.get_telemetry()  # supported
    with pytest.raises(CapabilityNotSupported):
        await drone.takeoff(10.0, confirm=True)
    assert adapter.sinks == []
