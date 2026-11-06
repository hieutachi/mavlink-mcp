"""Signed gateway guardrails and legacy numerical/environment configuration."""

from __future__ import annotations

from dataclasses import replace

import pytest

from mavlink_mcp.capabilities import Waypoint
from mavlink_mcp.drone import Drone
from mavlink_mcp.guardrails import GuardrailConfig, haversine_m
from mavlink_mcp.safety.config import load_policy
from mavlink_mcp.safety.models import ContractError, PolicyDecision, ReasonCode
from tests.conftest import MockAdapter


def assert_denied(result, reason):
    assert result.decision is PolicyDecision.DENY, result.to_dict()
    assert result.reason_code is reason, result.to_dict()
    assert result.execution_status == "NOT_STARTED"


async def assert_all_writes_readonly(drone):
    for action in (
        lambda: drone.arm(confirm=True),
        drone.disarm,
        lambda: drone.takeoff(10.0, confirm=True),
        drone.land,
        drone.hold,
        drone.return_to_launch,
        lambda: drone.goto(47.398, 8.546, 20.0),
        lambda: drone.fly_mission([Waypoint(47.398, 8.546, 20.0)]),
    ):
        assert_denied(await action(), ReasonCode.READONLY)


# --------------------------- readonly mode --------------------------- #


async def test_readonly_rejects_all_mutating_ops(readonly_drone, adapter):
    await assert_all_writes_readonly(readonly_drone)
    assert adapter.sinks == []


async def test_signed_policy_readonly_rejects_all_mutating_ops(rig_factory):
    rig = rig_factory(policy={"readonly": True})
    await assert_all_writes_readonly(rig.drone)
    assert rig.adapter.sinks == []


async def test_readonly_allows_telemetry(readonly_drone, adapter):
    snapshot = await readonly_drone.get_telemetry()
    assert snapshot.flight_mode == "HOLD"
    assert await readonly_drone.get_flight_mode() == "HOLD"


async def test_readonly_is_the_default():
    assert GuardrailConfig().readonly is True


# ------------------------- signed approvals -------------------------- #


@pytest.mark.parametrize("kwargs", [{}, {"confirm": False}, {"confirm": True}])
async def test_arm_requires_signed_approval_not_confirmation(drone, adapter, kwargs):
    pending = await drone.arm(**kwargs)
    assert pending.decision is PolicyDecision.REQUIRE_APPROVAL, pending.to_dict()
    assert pending.reason_code is ReasonCode.APPROVAL_REQUIRED
    assert adapter.sinks == []


@pytest.mark.parametrize("kwargs", [{}, {"confirm": False}, {"confirm": True}])
async def test_takeoff_requires_signed_approval_not_confirmation(drone, adapter, kwargs):
    adapter.telemetry = replace(adapter.telemetry, armed=True, in_air=False)
    pending = await drone.takeoff(10.0, **kwargs)
    assert pending.decision is PolicyDecision.REQUIRE_APPROVAL, pending.to_dict()
    assert pending.reason_code is ReasonCode.APPROVAL_REQUIRED
    assert adapter.sinks == []


async def test_confirm_cannot_authorize_changed_takeoff(rig, drone, adapter):
    adapter.telemetry = replace(adapter.telemetry, armed=True, in_air=False)
    pending = await drone.takeoff(10.0)
    result = await drone.takeoff(
        20.0, confirm=True, request_id=pending.request_id, approval_token=rig.token(pending),
    )
    assert_denied(result, ReasonCode.PARAMETER_MISMATCH)
    assert adapter.sinks == []


async def test_land_rtl_hold_require_and_accept_signed_approval(rig, drone, adapter):
    for method in (drone.land, drone.return_to_launch, drone.hold):
        before = list(adapter.sinks)
        pending = await method()
        assert pending.decision is PolicyDecision.REQUIRE_APPROVAL, pending.to_dict()
        assert adapter.sinks == before
        result = await method(request_id=pending.request_id, approval_token=rig.token(pending))
        assert result.execution_status == "ACKNOWLEDGED", result.to_dict()
    assert adapter.sinks == [("land", None), ("return_to_launch", None), ("hold", None)]


# ------------------------- altitude ceiling -------------------------- #


async def test_takeoff_above_ceiling_rejected(drone, adapter):
    adapter.telemetry = replace(adapter.telemetry, armed=True, in_air=False)
    assert_denied(await drone.takeoff(101.0, confirm=True), ReasonCode.ALTITUDE_LIMIT)
    assert adapter.sinks == []


async def test_goto_above_ceiling_rejected(drone, adapter):
    adapter.telemetry = replace(adapter.telemetry, armed=True, in_air=True)
    assert_denied(await drone.goto(47.398, 8.546, 150.0), ReasonCode.ALTITUDE_LIMIT)
    assert adapter.sinks == []


async def test_mission_waypoint_above_ceiling_rejected(drone, adapter):
    adapter.telemetry = replace(adapter.telemetry, armed=True, in_air=True)
    waypoints = [Waypoint(47.398, 8.546, 20.0), Waypoint(47.398, 8.546, 150.0)]
    assert_denied(await drone.fly_mission(waypoints), ReasonCode.ALTITUDE_LIMIT)
    assert adapter.sinks == []


async def test_non_positive_altitude_rejected(drone, adapter):
    assert_denied(await drone.takeoff(0.0, confirm=True), ReasonCode.INVALID_ARGUMENT)
    assert_denied(await drone.goto(47.398, 8.546, -5.0), ReasonCode.INVALID_ARGUMENT)
    assert adapter.calls == []  # Invalid input is rejected before connecting.


async def test_altitude_ceiling_zero_cannot_bypass_missing_gateway(adapter):
    drone = Drone(
        adapter,
        guardrails=GuardrailConfig(
            readonly=False, max_altitude_m=0.0, geofence_radius_m=0.0,
        ),
    )
    assert_denied(await drone.takeoff(500.0, confirm=True), ReasonCode.GATEWAY_UNAVAILABLE)
    assert adapter.sinks == []


@pytest.mark.parametrize("limit", ["max_altitude_m", "max_radius_m", "max_speed_mm_s"])
@pytest.mark.parametrize("value", [0, -1])
def test_signed_policy_limits_must_be_strictly_positive(limit, value):
    with pytest.raises(ContractError, match=limit):
        load_policy({"readonly": False, limit: value})


# ----------------------------- geofence ------------------------------ #


async def test_goto_outside_geofence_rejected(drone, adapter):
    adapter.telemetry = replace(adapter.telemetry, armed=True, in_air=True)
    # ~1.1 km north of home; fence is 500 m.
    assert_denied(await drone.goto(47.4077, 8.5456, 20.0), ReasonCode.OUTSIDE_GEOFENCE)
    assert adapter.sinks == []


async def test_goto_inside_geofence_allowed(rig, drone, adapter):
    adapter.telemetry = replace(adapter.telemetry, armed=True, in_air=True)
    await rig.run(drone.goto, 47.3990, 8.5470, 20.0)  # ~150 m from home
    assert [sink for sink, _ in adapter.sinks] == ["goto_location"]


async def test_mission_with_far_waypoint_rejected(drone, adapter):
    adapter.telemetry = replace(adapter.telemetry, armed=True, in_air=True)
    waypoints = [
        Waypoint(47.3990, 8.5470, 20.0),  # inside
        Waypoint(47.4077, 8.5456, 20.0),  # ~1.1 km outside
    ]
    assert_denied(await drone.fly_mission(waypoints), ReasonCode.OUTSIDE_GEOFENCE)
    assert adapter.sinks == []


async def test_geofence_requires_home_position(rig_factory):
    adapter = MockAdapter(home=None)
    adapter.telemetry = replace(adapter.telemetry, armed=True, in_air=True)
    drone = rig_factory(adapter).drone
    assert_denied(await drone.goto(47.3990, 8.5470, 20.0), ReasonCode.TELEMETRY_INCOMPLETE)
    assert adapter.sinks == []


async def test_geofence_zero_cannot_bypass_missing_gateway(adapter):
    drone = Drone(
        adapter,
        guardrails=GuardrailConfig(
            readonly=False, max_altitude_m=100.0, geofence_radius_m=0.0,
        ),
    )
    assert_denied(await drone.goto(48.0, 9.0, 20.0), ReasonCode.GATEWAY_UNAVAILABLE)
    assert adapter.sinks == []


async def test_empty_mission_rejected(drone, adapter):
    assert_denied(await drone.fly_mission([]), ReasonCode.INVALID_ARGUMENT)
    assert adapter.calls == []


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


async def test_env_guardrails_are_dynamic_but_cannot_authorize(adapter, monkeypatch):
    drone = Drone(adapter)
    monkeypatch.setenv("MAVLINK_MCP_READONLY", "true")
    assert drone.guardrails.readonly is True
    assert_denied(await drone.land(), ReasonCode.READONLY)
    monkeypatch.setenv("MAVLINK_MCP_READONLY", "false")
    assert drone.guardrails.readonly is False
    assert_denied(await drone.land(), ReasonCode.GATEWAY_UNAVAILABLE)
    assert adapter.sinks == []


# ----------------------------- haversine ----------------------------- #


def test_haversine_zero_distance():
    assert haversine_m(47.4, 8.5, 47.4, 8.5) == 0.0


def test_haversine_known_distance():
    # One degree of latitude is ~111.2 km.
    distance = haversine_m(47.0, 8.5, 48.0, 8.5)
    assert distance == pytest.approx(111_200, rel=0.01)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), 10**400])
async def test_nonfinite_inputs_never_reach_adapter(drone, adapter, value):
    for action in (
        lambda: drone.takeoff(value, confirm=True),
        lambda: drone.goto(value, 8.54, 10),
        lambda: drone.goto(47.4, value, 10),
        lambda: drone.goto(47.4, 8.54, value),
        lambda: drone.goto(47.4, 8.54, 10, yaw_deg=value),
        lambda: drone.fly_mission([Waypoint(47.4, 8.54, 10, value)]),
    ):
        assert_denied(await action(), ReasonCode.INVALID_ARGUMENT)
    assert adapter.calls == []


@pytest.mark.parametrize("speed", [0, -1])
async def test_invalid_speed_rejected(drone, adapter, speed):
    assert_denied(
        await drone.fly_mission([Waypoint(47.4, 8.54, 10, speed)]),
        ReasonCode.INVALID_ARGUMENT,
    )
    assert adapter.calls == []


@pytest.mark.parametrize("lat,lon", [(91, 0), (-91, 0), (0, 181), (0, -181)])
async def test_legacy_zero_fence_cannot_disable_coordinate_validation(rig, adapter, lat, lon):
    drone = Drone(
        adapter, GuardrailConfig(readonly=False, geofence_radius_m=0),
        safety_gateway=rig.gateway,
    )
    assert_denied(await drone.goto(lat, lon, 10), ReasonCode.INVALID_ARGUMENT)
    assert adapter.calls == []


@pytest.mark.parametrize("value", ["nan", "inf", "-inf", "-1", "1e-999", "-1e-999"])
def test_invalid_numeric_env_cannot_disable_limits(monkeypatch, value):
    monkeypatch.setenv("MAVLINK_MCP_MAX_ALTITUDE_M", value)
    monkeypatch.setenv("MAVLINK_MCP_GEOFENCE_RADIUS_M", value)
    config = GuardrailConfig.from_env()
    assert config.max_altitude_m == 50 and config.geofence_radius_m == 200


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1])
def test_invalid_fixed_limits_rejected(value):
    with pytest.raises(ValueError):
        GuardrailConfig(max_altitude_m=value)
    with pytest.raises(ValueError):
        GuardrailConfig(geofence_radius_m=value)


async def test_mission_size_is_bounded(drone, adapter):
    assert_denied(
        await drone.fly_mission([Waypoint(47.4, 8.54, 10)] * 1001),
        ReasonCode.INVALID_ARGUMENT,
    )
    assert adapter.calls == []
