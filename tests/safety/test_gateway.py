"""Real signed approvals exercise the supported gateway, not dummy dispatchers."""
from dataclasses import replace
import pytest
from mavlink_mcp.adapter import ExecutionLease, LeaseError
from mavlink_mcp.safety.models import Action, PolicyDecision, ReasonCode, request_from_dict
from tests.conftest import HOME


async def test_submit_issues_live_bound_request_and_dispatches_once(rig):
    pending = await rig.drone.arm(confirm=True)
    assert pending.execution_status == "NOT_STARTED"
    assert rig.adapter.sinks == []
    assert pending.approval_record.expires_at_utc_ms > rig.store.clock()
    request = request_from_dict(rig.store.get_request(pending.request_id)[0])
    assert request.home_anchor["absolute_altitude_amsl_mm"] == 488000
    token = rig.token(pending)
    result = await rig.gateway.submit(request, rig.context, approval=token)
    assert result.execution_status == "ACKNOWLEDGED"
    assert [s[0] for s in rig.adapter.sinks] == ["arm"]
    assert rig.store.get_request(pending.request_id)[2] == "ACKNOWLEDGED"
    replay = await rig.drone.arm(request_id=pending.request_id, approval_token=token)
    assert replay.reason_code is ReasonCode.APPROVAL_REPLAY
    assert len(rig.adapter.sinks) == 1
    events = rig.store.export_audit()
    assert [e["kind"] for e in events] == ["APPROVAL_REQUESTED", "EXECUTION_INTENT", "SINK_CHECK", "REQUEST_ACKNOWLEDGED"]
    assert rig.store.verify_audit(events)


@pytest.mark.parametrize("change", ["home", "connection", "policy", "actor", "key"])
async def test_approval_invalidated_by_trusted_binding_changes(rig, change):
    pending = await rig.drone.arm()
    token = rig.token(pending)
    if change == "home":
        rig.adapter.home = replace(HOME, absolute_altitude_m=489)
        rig.adapter.home_epoch += 1
    elif change == "connection":
        rig.adapter.connection_epoch += 1
    elif change == "policy":
        from mavlink_mcp.safety.config import load_policy
        rig.gateway.policy = load_policy({"readonly": False, "max_altitude_m": 60})
    elif change == "actor":
        rig.gateway.context = replace(rig.context, principal=replace(rig.context.principal, actor_id="other"))
    else:
        key = rig.gateway.approval_store._keys["test-key"]
        rig.gateway.approval_store._keys["test-key"] = replace(key, revoked=True)
    result = await rig.drone.arm(request_id=pending.request_id, approval_token=token)
    assert result.decision is PolicyDecision.DENY
    assert rig.adapter.sinks == []


async def test_changed_parameters_and_action_never_reach_sink(rig):
    rig.adapter.telemetry = replace(rig.adapter.telemetry, armed=True)
    pending = await rig.drone.takeoff(10)
    token = rig.token(pending)
    for method, args in ((rig.drone.takeoff, (11,)), (rig.drone.land, ())):
        result = await method(*args, request_id=pending.request_id, approval_token=token)
        assert result.reason_code is ReasonCode.PARAMETER_MISMATCH
    assert rig.adapter.sinks == []


async def test_no_client_mintable_lease_or_public_lease_parameter(rig):
    forged = ExecutionLease("l", "r", "arm", "test-vehicle", 9999999999999, {}, ("arm",))
    with pytest.raises(TypeError):
        await rig.drone.arm(lease=forged)
    with pytest.raises(LeaseError):
        await rig.adapter.arm(lease=forged)
    with pytest.raises(LeaseError):
        await rig.adapter.arm()
    assert rig.adapter.sinks == []


@pytest.mark.parametrize("bad", [True, "10", float("nan"), float("inf"), 10.0001])
async def test_strict_input_is_rejected_before_connect(rig, bad):
    result = await rig.drone.takeoff(bad)
    assert result.reason_code is ReasonCode.INVALID_ARGUMENT
    assert rig.adapter.calls == []


async def test_closed_store_denies_without_sink(rig):
    pending = await rig.drone.arm()
    token = rig.token(pending)
    rig.store.close()
    result = await rig.drone.arm(request_id=pending.request_id, approval_token=token)
    assert result.reason_code is ReasonCode.AUDIT_UNAVAILABLE
    assert rig.adapter.sinks == []
