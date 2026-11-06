"""Caller-owned mission inputs are frozen before the first gateway await."""

from dataclasses import replace

from mavlink_mcp.capabilities import Waypoint
from mavlink_mcp.safety.models import PolicyDecision, request_from_dict
from tests.conftest import MockAdapter


class MutatingConnectAdapter(MockAdapter):
    def __init__(self, source):
        super().__init__()
        self.source = source
        self.telemetry = replace(self.telemetry, armed=True, in_air=True)

    async def connect(self):
        self.source[0] = Waypoint(47.397742, 8.545594, 999.0)
        await super().connect()


async def test_fly_mission_dispatches_pre_await_waypoint_snapshot(rig_factory):
    waypoint = Waypoint(47.397742, 8.545594, 20.0)
    original = [waypoint]
    adapter = MutatingConnectAdapter(original)
    rig = rig_factory(adapter)

    pending = await rig.drone.fly_mission(original)
    assert pending.decision is PolicyDecision.REQUIRE_APPROVAL, pending.to_dict()
    assert original[0].relative_altitude_m == 999.0
    assert adapter.sinks == []
    request = request_from_dict(rig.store.get_request(pending.request_id)[0])
    assert request.parameters["waypoints"][0]["relative_altitude_mm"] == 20_000
    assert pending.approval_request["parameters"]["waypoints"][0]["relative_altitude_mm"] == 20_000

    # Resubmit exactly the proposed arguments with a real test-key signature.
    original[0] = waypoint
    token = rig.token(pending)
    result = await rig.drone.fly_mission(
        original, request_id=pending.request_id, approval_token=token,
    )
    assert result.execution_status == "ACKNOWLEDGED", result.to_dict()
    dispatched = next(call for call in adapter.calls if call[0] == "fly_mission")[1]
    assert dispatched == (replace(waypoint, speed_m_s=5.0),)
    assert dispatched[0].relative_altitude_m == 20.0
    assert all(wp.relative_altitude_m != 999.0 for wp in dispatched)
    assert adapter.sinks == [
        ("upload_mission", {"waypoints": dispatched}), ("start_mission", None),
    ]
