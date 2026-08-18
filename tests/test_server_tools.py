"""MCP tool functions with an injected mock drone (no SITL, no MAVSDK)."""

from __future__ import annotations

import pytest

import mavlink_mcp.server as server
from mavlink_mcp.drone import Drone
from mavlink_mcp.guardrails import GuardrailConfig
from tests.conftest import PERMISSIVE, MockAdapter


@pytest.fixture
def adapter(monkeypatch) -> MockAdapter:
    """Route the server's shared drone to a fresh mock adapter."""
    mock = MockAdapter()
    monkeypatch.setattr(server, "_drone", Drone(mock, guardrails=PERMISSIVE))
    return mock


@pytest.fixture
def readonly_server(monkeypatch) -> MockAdapter:
    mock = MockAdapter()
    monkeypatch.setattr(
        server, "_drone", Drone(mock, guardrails=GuardrailConfig())
    )
    return mock


async def test_get_capabilities(adapter):
    result = await server.get_capabilities()
    assert result["adapter"] == "mock"
    assert "takeoff" in result["capabilities"]
    assert "telemetry" in result["capabilities"]
    assert result["guardrails"]["readonly"] is False
    assert result["connected"] is False


async def test_get_telemetry_shape(adapter):
    result = await server.get_telemetry()
    assert result["position"]["latitude_deg"] == pytest.approx(47.397742)
    assert result["battery"]["voltage_v"] == 12.6
    assert result["flight_mode"] == "HOLD"
    assert result["in_air"] is False


async def test_get_flight_mode(adapter):
    assert await server.get_flight_mode() == {"flight_mode": "HOLD"}


async def test_get_connection_status(adapter):
    before = await server.get_connection_status()
    assert before["connected"] is False
    await server.get_telemetry()
    after = await server.get_connection_status()
    assert after["connected"] is True
    assert after["adapter"] == "mock"


async def test_full_flight_sequence(adapter):
    assert await server.arm(confirm=True) == {"armed": True}
    takeoff = await server.takeoff(altitude_m=15.0, confirm=True)
    assert takeoff == {"takeoff_started": True, "target_altitude_m": 15.0}
    goto = await server.goto_location(47.3990, 8.5470, 20.0, yaw_deg=45.0)
    assert goto["goto_started"] is True
    assert goto["target"]["yaw_deg"] == 45.0
    assert await server.land() == {"landing_started": True}
    assert await server.disarm() == {"disarmed": True}
    assert adapter.call_names().count("connect") == 1


async def test_fly_mission_tool(adapter):
    result = await server.fly_mission(
        [
            {"latitude_deg": 47.3990, "longitude_deg": 8.5470,
             "relative_altitude_m": 20.0},
            {"latitude_deg": 47.3985, "longitude_deg": 8.5465,
             "relative_altitude_m": 25.0, "speed_m_s": 3.0},
        ]
    )
    assert result == {"mission_started": True, "waypoint_count": 2}


async def test_fly_mission_invalid_waypoints(adapter):
    result = await server.fly_mission([{"latitude_deg": 47.0}])
    assert "Invalid waypoint list" in result["error"]
    assert "fly_mission" not in adapter.call_names()


async def test_arm_without_confirm_returns_guardrail_error(adapter):
    result = await server.arm()
    assert result["guardrail"] is True
    assert "confirm" in result["error"]
    assert "arm" not in adapter.call_names()


async def test_readonly_error_shape(readonly_server):
    result = await server.takeoff(confirm=True)
    assert result["guardrail"] is True
    assert "readonly" in result["error"]
    # Telemetry still works.
    telemetry = await server.get_telemetry()
    assert telemetry["flight_mode"] == "HOLD"


async def test_geofence_violation_surfaces_as_error(adapter):
    result = await server.goto_location(47.4077, 8.5456, 20.0)  # ~1.1 km
    assert result["guardrail"] is True
    assert "geofence" in result["error"]


async def test_all_tools_are_registered():
    tools = await server.mcp.get_tools()
    expected = {
        "get_capabilities",
        "get_telemetry",
        "get_flight_mode",
        "get_connection_status",
        "arm",
        "disarm",
        "takeoff",
        "land",
        "hold",
        "return_to_launch",
        "goto_location",
        "fly_mission",
    }
    assert expected <= set(tools)
