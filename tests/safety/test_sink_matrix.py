"""Eight actions / ten sinks: positive signed path, and no-authority denials."""
from dataclasses import replace
import pytest
from mavlink_mcp.adapter import LeaseError
from mavlink_mcp.capabilities import Waypoint
from mavlink_mcp.safety.models import PolicyDecision, ReasonCode

POINT = {"latitude_deg": 47.3980, "longitude_deg": 8.5460, "relative_altitude_m": 20}
CASES = {
    "arm": ((), dict(armed=False, in_air=False), ["arm"]),
    "disarm": ((), dict(armed=True, in_air=False), ["disarm"]),
    "takeoff": ((10,), dict(armed=True, in_air=False), ["set_takeoff_altitude", "takeoff"]),
    "land": ((), dict(armed=True, in_air=True), ["land"]),
    "hold": ((), dict(armed=True, in_air=True), ["hold"]),
    "return_to_launch": ((), dict(armed=True, in_air=True), ["return_to_launch"]),
    "goto": ((47.3980, 8.5460, 20, 90), dict(armed=True, in_air=True), ["goto_location"]),
    "fly_mission": (([POINT, {**POINT, "speed_m_s": 3}],), dict(armed=True, in_air=True), ["upload_mission", "start_mission"]),
}


@pytest.mark.parametrize("name", CASES)
async def test_signed_action_reaches_exact_sink_sequence(rig, name):
    args, state, sinks = CASES[name]
    rig.adapter.telemetry = replace(rig.adapter.telemetry, **state)
    result = await rig.run(getattr(rig.drone, name), *args)
    assert result.to_dict()["execution_status"] == "ACKNOWLEDGED"
    assert [s[0] for s in rig.adapter.sinks] == sinks
    if name == "goto":
        params = rig.adapter.sinks[0][1]
        assert params["waypoint"] == Waypoint(47.398, 8.546, 20.0)
        assert params["absolute_altitude_m"] == pytest.approx(508.0)
        assert params["yaw_deg"] == 90
    if name == "fly_mission":
        assert [w.speed_m_s for w in rig.adapter.sinks[0][1]["waypoints"]] == [5.0, 3.0]


@pytest.mark.parametrize("name", CASES)
async def test_no_gateway_and_unsigned_requests_never_reach_sink(rig, name):
    from mavlink_mcp.drone import Drone
    args, state, _ = CASES[name]
    rig.adapter.telemetry = replace(rig.adapter.telemetry, **state)
    bare = Drone(rig.adapter)
    assert (await getattr(bare, name)(*args)).decision is PolicyDecision.DENY
    pending = await getattr(rig.drone, name)(*args)
    assert pending.decision is PolicyDecision.REQUIRE_APPROVAL
    forged = await getattr(rig.drone, name)(*args, request_id=pending.request_id, approval_token="e30.AAAA")
    assert forged.reason_code is ReasonCode.APPROVAL_INVALID
    assert rig.adapter.sinks == []


@pytest.mark.parametrize("sink,call", [
    ("arm", lambda a, l: a.arm(lease=l)), ("disarm", lambda a, l: a.disarm(lease=l)),
    ("set_takeoff_altitude", lambda a, l: a.takeoff(10.0, lease=l)), ("land", lambda a, l: a.land(lease=l)),
    ("hold", lambda a, l: a.hold(lease=l)), ("return_to_launch", lambda a, l: a.return_to_launch(lease=l)),
    ("goto_location", lambda a, l: a.goto(Waypoint(47.398, 8.546, 20.0), lease=l, absolute_altitude_m=508.0)),
    ("upload_mission", lambda a, l: a.fly_mission([Waypoint(47.398, 8.546, 20.0)], lease=l)),
])
async def test_raw_adapter_sinks_reject_missing_lease(rig, sink, call):
    with pytest.raises(LeaseError):
        await call(rig.adapter, None)
    assert rig.adapter.sinks == []


async def test_unsafe_state_and_limits_never_reach_sink(rig):
    rig.adapter.telemetry = replace(rig.adapter.telemetry, armed=False, in_air=False)
    assert (await rig.drone.takeoff(10)).reason_code is ReasonCode.UNSAFE_STATE
    assert (await rig.drone.goto(*POINT.values())).reason_code is ReasonCode.UNSAFE_STATE
    rig.adapter.telemetry = replace(rig.adapter.telemetry, armed=True, in_air=True)
    assert (await rig.drone.goto(47.4077, 8.5456, 20)).reason_code is ReasonCode.OUTSIDE_GEOFENCE
    assert (await rig.drone.goto(47.398, 8.546, 101)).reason_code is ReasonCode.ALTITUDE_LIMIT
    rig.adapter.telemetry = replace(rig.adapter.telemetry, battery=replace(rig.adapter.telemetry.battery, remaining_percent=10))
    assert (await rig.drone.goto(*POINT.values())).reason_code is ReasonCode.UNSAFE_STATE
    rig.adapter.monitor_healthy = False
    assert (await rig.drone.land()).reason_code is ReasonCode.MONITOR_UNHEALTHY
    assert rig.adapter.sinks == []
