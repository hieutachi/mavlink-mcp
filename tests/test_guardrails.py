"""Guardrail behavior: readonly, confirmation, altitude ceiling, geofence."""

from __future__ import annotations

import pytest

from mavlink_mcp.capabilities import Waypoint
from mavlink_mcp.drone import Drone
from mavlink_mcp.guardrails import (
    GuardrailConfig,
    GuardrailViolation,
    haversine_m,
)
from tests.conftest import PERMISSIVE, MockAdapter

# --------------------------- readonly mode --------------------------- #


async def test_readonly_rejects_all_mutating_ops(readonly_drone, adapter):
    with pytest.raises(GuardrailViolation, match="readonly"):
        await readonly_drone.arm(confirm=True)
    with pytest.raises(GuardrailViolation, match="readonly"):
        await readonly_drone.disarm()
    with pytest.raises(GuardrailViolation, match="readonly"):
        await readonly_drone.takeoff(10.0, confirm=True)
    with pytest.raises(GuardrailViolation, match="readonly"):
        await readonly_drone.land()
    with pytest.raises(GuardrailViolation, match="readonly"):
        await readonly_drone.hold()
    with pytest.raises(GuardrailViolation, match="readonly"):
        await readonly_drone.return_to_launch()
    with pytest.raises(GuardrailViolation, match="readonly"):
        await readonly_drone.goto(47.398, 8.546, 20.0)
    with pytest.raises(GuardrailViolation, match="readonly"):
        await readonly_drone.fly_mission([Waypoint(47.398, 8.546, 20.0)])
    assert adapter.calls == []  # nothing reached the adapter


async def test_readonly_allows_telemetry(readonly_drone, adapter):
    snapshot = await readonly_drone.get_telemetry()
    assert snapshot.flight_mode == "HOLD"
    assert await readonly_drone.get_flight_mode() == "HOLD"


async def test_readonly_is_the_default():
    assert GuardrailConfig().readonly is True


# ------------------------ confirmation gates ------------------------- #


async def test_arm_requires_confirmation(drone, adapter):
    with pytest.raises(GuardrailViolation, match="confirm"):
        await drone.arm()
    with pytest.raises(GuardrailViolation, match="confirm"):
        await drone.arm(confirm=False)
    assert "arm" not in adapter.call_names()


async def test_takeoff_requires_confirmation(drone, adapter):
    with pytest.raises(GuardrailViolation, match="confirm"):
        await drone.takeoff(10.0)
    assert "takeoff" not in adapter.call_names()


async def test_land_rtl_hold_do_not_require_confirmation(drone, adapter):
    # Safety actions must stay friction-free.
    await drone.land()
    await drone.return_to_launch()
    await drone.hold()


# ------------------------- altitude ceiling -------------------------- #


async def test_takeoff_above_ceiling_rejected(drone, adapter):
    with pytest.raises(GuardrailViolation, match="ceiling"):
        await drone.takeoff(101.0, confirm=True)  # ceiling is 100 m
    assert "takeoff" not in adapter.call_names()


async def test_goto_above_ceiling_rejected(drone, adapter):
    with pytest.raises(GuardrailViolation, match="ceiling"):
        await drone.goto(47.398, 8.546, 150.0)


async def test_mission_waypoint_above_ceiling_rejected(drone, adapter):
    waypoints = [Waypoint(47.398, 8.546, 20.0), Waypoint(47.398, 8.546, 150.0)]
    with pytest.raises(GuardrailViolation, match="ceiling"):
        await drone.fly_mission(waypoints)
    assert "fly_mission" not in adapter.call_names()


async def test_non_positive_altitude_rejected(drone):
    with pytest.raises(GuardrailViolation, match="positive"):
        await drone.takeoff(0.0, confirm=True)
    with pytest.raises(GuardrailViolation, match="positive"):
        await drone.goto(47.398, 8.546, -5.0)


async def test_altitude_ceiling_zero_disables_check(adapter):
    drone = Drone(
        adapter,
        guardrails=GuardrailConfig(
            readonly=False, max_altitude_m=0.0, geofence_radius_m=0.0
        ),
    )
    await drone.takeoff(500.0, confirm=True)
    assert ("takeoff", 500.0) in adapter.calls


# --------------------------- soft geofence --------------------------- #


async def test_goto_outside_geofence_rejected(drone, adapter):
    # ~1.1 km north of home; fence is 500 m.
    with pytest.raises(GuardrailViolation, match="geofence"):
        await drone.goto(47.4077, 8.5456, 20.0)
    assert "goto" not in adapter.call_names()


async def test_goto_inside_geofence_allowed(drone, adapter):
    await drone.goto(47.3990, 8.5470, 20.0)  # ~150 m from home
    assert "goto" in adapter.call_names()


async def test_mission_with_far_waypoint_rejected(drone, adapter):
    waypoints = [
        Waypoint(47.3990, 8.5470, 20.0),  # inside
        Waypoint(47.4077, 8.5456, 20.0),  # ~1.1 km — outside
    ]
    with pytest.raises(GuardrailViolation, match="geofence"):
        await drone.fly_mission(waypoints)
    assert "fly_mission" not in adapter.call_names()


async def test_geofence_requires_home_position():
    adapter = MockAdapter(home=None)
    drone = Drone(adapter, guardrails=PERMISSIVE)
    with pytest.raises(GuardrailViolation, match="home position"):
        await drone.goto(47.3990, 8.5470, 20.0)


async def test_geofence_zero_disables_check(adapter):
    drone = Drone(
        adapter,
        guardrails=GuardrailConfig(
            readonly=False, max_altitude_m=100.0, geofence_radius_m=0.0
        ),
    )
    await drone.goto(48.0, 9.0, 20.0)  # far away, but fence disabled
    assert "goto" in adapter.call_names()


async def test_empty_mission_rejected(drone):
    with pytest.raises(GuardrailViolation, match="at least one waypoint"):
        await drone.fly_mission([])


# ----------------------- env var configuration ----------------------- #


def test_from_env_defaults(monkeypatch):
    for var in (
        "MAVLINK_MCP_READONLY",
        "MAVLINK_MCP_MAX_ALTITUDE_M",
        "MAVLINK_MCP_GEOFENCE_RADIUS_M",
    ):
        monkeypatch.delenv(var, raising=False)
    config = GuardrailConfig.from_env()
    assert config.readonly is True
    assert config.max_altitude_m == 50.0
    assert config.geofence_radius_m == 200.0


def test_from_env_overrides(monkeypatch):
    monkeypatch.setenv("MAVLINK_MCP_READONLY", "false")
    monkeypatch.setenv("MAVLINK_MCP_MAX_ALTITUDE_M", "120")
    monkeypatch.setenv("MAVLINK_MCP_GEOFENCE_RADIUS_M", "1000")
    config = GuardrailConfig.from_env()
    assert config.readonly is False
    assert config.max_altitude_m == 120.0
    assert config.geofence_radius_m == 1000.0


def test_from_env_invalid_numbers_fall_back(monkeypatch):
    monkeypatch.setenv("MAVLINK_MCP_MAX_ALTITUDE_M", "not-a-number")
    monkeypatch.setenv("MAVLINK_MCP_GEOFENCE_RADIUS_M", "")
    config = GuardrailConfig.from_env()
    assert config.max_altitude_m == 50.0
    assert config.geofence_radius_m == 200.0


def test_readonly_truthy_variants(monkeypatch):
    for value in ("1", "true", "TRUE", "yes", "on", "anything"):
        monkeypatch.setenv("MAVLINK_MCP_READONLY", value)
        assert GuardrailConfig.from_env().readonly is True
    for value in ("0", "false", "False", "no", "off"):
        monkeypatch.setenv("MAVLINK_MCP_READONLY", value)
        assert GuardrailConfig.from_env().readonly is False


async def test_env_guardrails_are_dynamic(adapter, monkeypatch):
    """A Drone without fixed guardrails re-reads the env on every check."""
    drone = Drone(adapter)  # no fixed config → env-driven
    monkeypatch.setenv("MAVLINK_MCP_READONLY", "true")
    with pytest.raises(GuardrailViolation, match="readonly"):
        await drone.land()
    monkeypatch.setenv("MAVLINK_MCP_READONLY", "false")
    await drone.land()
    assert "land" in adapter.call_names()


# ----------------------------- haversine ----------------------------- #


def test_haversine_zero_distance():
    assert haversine_m(47.4, 8.5, 47.4, 8.5) == 0.0


def test_haversine_known_distance():
    # One degree of latitude is ~111.2 km.
    distance = haversine_m(47.0, 8.5, 48.0, 8.5)
    assert distance == pytest.approx(111_200, rel=0.01)
