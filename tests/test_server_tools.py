"""MCP tool functions over the trusted runtime (no SITL, no MAVSDK, no network)."""

from __future__ import annotations

from dataclasses import replace

import pytest

import mavlink_mcp.server as server
from mavlink_mcp.drone import Drone
from mavlink_mcp.guardrails import GuardrailConfig
from mavlink_mcp.safety.models import ReasonCode
from tests.conftest import MockAdapter


@pytest.fixture
def rig(monkeypatch, rig_factory):
    """Route the server's shared drone through a real signed-approval gateway."""
    created = rig_factory()
    monkeypatch.setattr(server, "_drone", created.drone)
    return created


@pytest.fixture
def adapter(rig) -> MockAdapter:
    return rig.adapter


@pytest.fixture
def readonly_server(monkeypatch) -> MockAdapter:
    mock = MockAdapter()
    monkeypatch.setattr(server, "_drone", Drone(mock, guardrails=GuardrailConfig()))
    return mock


async def approve(rig, tool, **kwargs):
    """Two-step MCP flow: propose, sign out of band, resubmit unchanged."""
    pending = await tool(**kwargs)
    assert pending["decision"] == "REQUIRE_APPROVAL", pending
    assert pending["execution_status"] == "NOT_STARTED"
    token = rig.token_for(pending["request_id"])
    return await tool(**kwargs, request_id=pending["request_id"], approval_token=token)


async def test_backend_info_is_a_property_and_server_reads_it(adapter):
    info = server.get_drone().backend_info
    assert info == {"name": "mock", "protected": True, "connected": False,
                    "gateway_configured": True}
    result = await server.get_capabilities()
    assert result["adapter"] == "mock"


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


async def test_full_flight_sequence_requires_signed_approval_per_step(rig):
    armed = await approve(rig, server.arm, confirm=True)
    assert armed["execution_status"] == "ACKNOWLEDGED"
    assert armed["decision"] == "ALLOW"
    # Acceptance is reported as such; there is no "armed": true assertion.
    assert "armed" not in armed
    takeoff = await approve(rig, server.takeoff, altitude_m=15.0, confirm=True)
    assert takeoff["execution_status"] == "ACKNOWLEDGED"
    goto = await approve(rig, server.goto_location, latitude_deg=47.3990,
                         longitude_deg=8.5470, relative_altitude_m=20.0, yaw_deg=45.0)
    assert goto["execution_status"] == "ACKNOWLEDGED"
    assert (await approve(rig, server.land))["execution_status"] == "ACKNOWLEDGED"
    assert (await approve(rig, server.disarm))["execution_status"] == "ACKNOWLEDGED"
    assert [sink for sink, _ in rig.adapter.sinks] == [
        "arm", "set_takeoff_altitude", "takeoff", "goto_location", "land", "disarm"]


async def test_fly_mission_tool(rig):
    rig.adapter.telemetry = replace(rig.adapter.telemetry, armed=True, in_air=True)
    result = await approve(rig, server.fly_mission, waypoints=[
        {"latitude_deg": 47.3990, "longitude_deg": 8.5470, "relative_altitude_m": 20.0},
        {"latitude_deg": 47.3985, "longitude_deg": 8.5465, "relative_altitude_m": 25.0,
         "speed_m_s": 3.0},
    ])
    assert result["execution_status"] == "ACKNOWLEDGED"
    assert [sink for sink, _ in rig.adapter.sinks] == ["upload_mission", "start_mission"]
    uploaded = rig.adapter.sinks[0][1]["waypoints"]
    assert [wp.speed_m_s for wp in uploaded] == [5.0, 3.0]


async def test_fly_mission_invalid_waypoints(adapter):
    result = await server.fly_mission([{"latitude_deg": 47.0}])
    assert result["error"] == ReasonCode.INVALID_ARGUMENT.value
    assert adapter.sinks == []


@pytest.mark.parametrize("waypoints", [
    [],
    [{"latitude_deg": 47.0, "longitude_deg": 8.5, "relative_altitude_m": 20.0, "x": 1}],
    [{"latitude_deg": 47.00000001, "longitude_deg": 8.5, "relative_altitude_m": 20.0}],
    "not-a-list",
])
async def test_fly_mission_strict_input_never_reaches_adapter(adapter, waypoints):
    result = await server.fly_mission(waypoints)
    assert "error" in result
    assert adapter.sinks == []


async def test_confirm_alone_never_authorizes_and_token_alone_is_rejected(rig):
    unconfirmed = await server.arm()
    assert unconfirmed["decision"] == "REQUIRE_APPROVAL"
    # confirm=true is deprecated: it yields an approval request, not an action.
    confirmed = await server.arm(confirm=True)
    assert confirmed["decision"] == "REQUIRE_APPROVAL"
    token = rig.token_for(confirmed["request_id"])
    assert (await server.arm(approval_token=token))["error"] == ReasonCode.APPROVAL_INVALID.value
    forged = await server.arm(request_id=confirmed["request_id"], approval_token="e30.AAAA")
    assert forged["error"] == ReasonCode.APPROVAL_INVALID.value
    assert rig.adapter.sinks == []


async def test_readonly_error_shape(readonly_server):
    result = await server.takeoff(confirm=True)
    assert result["guardrail"] is True
    assert result["error"] == ReasonCode.READONLY.value
    assert readonly_server.sinks == []
    # Telemetry still works.
    telemetry = await server.get_telemetry()
    assert telemetry["flight_mode"] == "HOLD"


async def test_geofence_violation_surfaces_as_error(rig):
    rig.adapter.telemetry = replace(rig.adapter.telemetry, armed=True, in_air=True)
    result = await server.goto_location(47.4077, 8.5456, 20.0)  # ~1.1 km from home
    assert result["guardrail"] is True
    assert result["error"] == ReasonCode.OUTSIDE_GEOFENCE.value
    assert rig.adapter.sinks == []


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


async def test_sim_backend_metadata_and_no_network_import(monkeypatch):
    import builtins

    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "mavsdk" or name.startswith("mavsdk.") or "mavsdk_adapter" in name:
            raise AssertionError("sim must never load the MAVSDK backend")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    monkeypatch.setattr(server, "_drone", None)
    monkeypatch.delenv("SAFEMAV_CONFIG", raising=False)
    monkeypatch.setenv("MAVLINK_MCP_BACKEND", "sim")
    monkeypatch.setenv("MAVLINK_MCP_READONLY", "false")
    monkeypatch.setenv("MAVLINK_MCP_URL", "udp://unreachable.invalid:14540")
    capabilities = await server.get_capabilities()
    assert capabilities["adapter"] == "sim"
    assert capabilities["simulation"]["network_enabled"] is False
    telemetry = await server.get_telemetry()
    assert telemetry["simulation"]["synthetic"] is True
    assert telemetry["battery"] is None
    assert "system_address" not in await server.get_connection_status()
    # Backend cannot switch underneath a running client.
    monkeypatch.setenv("MAVLINK_MCP_BACKEND", "mavsdk")
    assert server.get_drone().backend_info["name"] == "sim"


@pytest.mark.parametrize("name", ["", "sim_typo", "SIMULATOR", "mock"])
async def test_unknown_backend_fails_closed(monkeypatch, name):
    monkeypatch.setattr(server, "_drone", None)
    monkeypatch.delenv("SAFEMAV_CONFIG", raising=False)
    monkeypatch.setenv("MAVLINK_MCP_BACKEND", name)
    result = await server.get_capabilities()
    assert "error" in result
    assert server._drone is None
    assert "error" in await server.get_connection_status()


async def test_no_trusted_config_means_no_gateway_and_no_writes(monkeypatch):
    """Absent SAFEMAV_CONFIG the simulator is read-only, with no ledger."""
    monkeypatch.setattr(server, "_drone", None)
    monkeypatch.delenv("SAFEMAV_CONFIG", raising=False)
    monkeypatch.setenv("MAVLINK_MCP_BACKEND", "sim")
    monkeypatch.setenv("MAVLINK_MCP_READONLY", "false")  # Not an authorization switch.
    drone = server.get_drone()
    assert drone.backend_info["gateway_configured"] is False
    assert drone.guardrails.readonly is True
    for tool in (server.arm, server.disarm, server.land, server.hold,
                 server.return_to_launch):
        result = await tool()
        assert result["error"] == ReasonCode.READONLY.value
        assert result["guardrail"] is True
    assert (await server.takeoff(altitude_m=5.0))["error"] == ReasonCode.READONLY.value
    assert (await server.get_telemetry())["flight_mode"] is not None


async def test_sim_readonly_is_still_default(monkeypatch):
    monkeypatch.setattr(server, "_drone", None)
    monkeypatch.delenv("SAFEMAV_CONFIG", raising=False)
    monkeypatch.setenv("MAVLINK_MCP_BACKEND", "sim")
    monkeypatch.delenv("MAVLINK_MCP_READONLY", raising=False)
    result = await server.arm(confirm=True)
    assert result["guardrail"] is True
    assert result["error"] == ReasonCode.READONLY.value
