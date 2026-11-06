"""Pin the G1/G2 boundary with executable tests, including the known gap.

A bypass counts as prevented only when no physical sink ran. The last test
documents what this design does NOT stop: same-process code that reads the
gateway's private issuer. That is a deployment (G2) concern, not G1.
"""
import pytest

from mavlink_mcp.adapter import ExecutionLease, LeaseError
from mavlink_mcp.drone import Drone
from mavlink_mcp.safety.models import PolicyDecision, ReasonCode
from tests.conftest import MockAdapter


async def test_second_facade_without_gateway_cannot_write(rig):
    """Wrapping a bound adapter in a bare Drone grants nothing."""
    result = await Drone(rig.adapter).arm(confirm=True)
    assert result.decision is PolicyDecision.DENY
    assert result.reason_code is ReasonCode.READONLY
    assert rig.adapter.sinks == []


async def test_direct_adapter_call_without_lease_is_refused(rig):
    with pytest.raises(LeaseError):
        await rig.adapter.arm()
    assert rig.adapter.sinks == []


async def test_self_minted_lease_is_refused(rig):
    """A lease with no issuer binding cannot authorize a sink."""
    forged = ExecutionLease("l", "r", "arm", "test-vehicle", 2 ** 62, {}, ("arm",))
    with pytest.raises(LeaseError):
        await rig.adapter.arm(lease=forged)
    assert rig.adapter.sinks == []


async def test_adapter_cannot_be_claimed_by_a_second_gateway(rig, rig_factory):
    with pytest.raises(LeaseError):
        rig_factory(rig.adapter)
    assert rig.adapter.sinks == []


async def test_lease_bound_to_another_vehicle_is_refused(rig):
    lease = ExecutionLease("l", "r", "arm", "other-vehicle", 2 ** 62, {}, ("arm",),
                           _issuer=rig.gateway._issuer, _recheck=lambda sink: None,
                           _deadline=float("inf"))
    with pytest.raises(LeaseError):
        await rig.adapter.arm(lease=lease)
    assert rig.adapter.sinks == []


async def test_lease_without_gateway_recheck_is_refused(rig):
    """Even a correctly bound lease must carry the synchronous recheck hook."""
    lease = ExecutionLease("l", "r", "arm", "test-vehicle", 2 ** 62, {}, ("arm",),
                           _issuer=rig.gateway._issuer, _deadline=float("inf"))
    with pytest.raises(LeaseError):
        await rig.adapter.arm(lease=lease)
    assert rig.adapter.sinks == []


async def test_known_gap_same_process_issuer_theft_is_not_prevented(rig):
    """Documented G1/G2 limit: in-process code can forge a lease.

    This asserts the real behavior so the limitation cannot be quietly lost.
    Preventing it requires OS-level isolation (G2), which is not implemented;
    see docs/THREAT_MODEL.md section 8b.
    """
    stolen = ExecutionLease("l", "r", "arm", "test-vehicle", 2 ** 62, {}, ("arm",),
                            _issuer=rig.gateway._issuer, _recheck=lambda sink: None,
                            _deadline=float("inf"))
    await rig.adapter.arm(lease=stolen)
    assert [sink for sink, _ in rig.adapter.sinks] == ["arm"]
    # No ledger entry exists, so the forgery is at least auditable after the fact.
    assert rig.store.export_audit() == []


async def test_readonly_principal_denies_every_action(rig_factory):
    from dataclasses import replace
    rig = rig_factory()
    rig.gateway.context = replace(rig.context, principal=replace(
        rig.context.principal, readonly=True))
    for method in (rig.drone.arm, rig.drone.disarm, rig.drone.land, rig.drone.hold,
                   rig.drone.return_to_launch):
        assert (await method()).reason_code is ReasonCode.READONLY
    assert (await rig.drone.takeoff(10)).reason_code is ReasonCode.READONLY
    assert rig.adapter.sinks == []
