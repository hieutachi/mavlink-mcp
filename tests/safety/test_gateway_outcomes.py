"""Timeouts, mid-dispatch failures and uncooperative cancellation use sink spies."""
import asyncio
from dataclasses import replace
import pytest
from mavlink_mcp.safety.models import PolicyDecision, ReasonCode
from mavlink_mcp.safety.store import StoreUnavailable, SafetyStore


class Clock:
    """Independent clocks so each deadline can expire on its own."""
    def __init__(self):
        self.now = 100.0
        self.utc_now = 100_000
    def mono(self): return self.now
    def utc(self): return self.utc_now


@pytest.mark.parametrize("operation", ["takeoff", "fly_mission"])
@pytest.mark.parametrize("failure", ["expiry", "monotonic", "stale", "home", "disconnect", "audit", "policy", "revoke"])
async def test_second_sink_rechecks_all_authority(rig_factory, operation, failure):
    clock = Clock()
    rig = rig_factory(clock=clock.utc, monotonic=clock.mono)
    rig.adapter.clock = clock.mono
    rig.adapter.telemetry = replace(rig.adapter.telemetry, armed=True, in_air=operation == "fly_mission")
    args = (10,) if operation == "takeoff" else ([{"latitude_deg": 47.397742, "longitude_deg": 8.545594, "relative_altitude_m": 10}],)
    method = getattr(rig.drone, operation)
    pending = await method(*args)
    token = rig.token(pending)
    async def after(sink):
        if failure == "expiry":
            clock.utc_now += 61_000  # Signed UTC expiry only.
        elif failure == "monotonic":
            clock.now += 61  # Monotonic deadline only; UTC unchanged.
            rig.adapter.observe()  # Keep telemetry fresh so only the deadline fails.
        elif failure == "stale":
            clock.now += 2
        elif failure == "home":
            rig.adapter.home = replace(rig.adapter.home, absolute_altitude_m=489)
            rig.adapter.home_epoch += 1
            rig.adapter.observe()
        elif failure == "disconnect":
            rig.adapter.connected = False
            rig.adapter.observe()
        elif failure == "audit":
            rig.store.check_writable = lambda *a, **k: (_ for _ in ()).throw(StoreUnavailable("disk failed"))
        elif failure == "policy":
            from mavlink_mcp.safety.config import load_policy
            rig.gateway.policy = load_policy({"readonly": False, "max_altitude_m": 60})
        else:
            key = rig.gateway.approval_store._keys["test-key"]
            rig.gateway.approval_store._keys["test-key"] = replace(key, revoked=True)
    rig.adapter.after_sink = after
    result = await method(*args, request_id=pending.request_id, approval_token=token)
    assert result.execution_status == "UNKNOWN"
    assert result.partial_effects is True
    assert len(rig.adapter.sinks) == 1
    assert rig.store.get_request(pending.request_id)[2] == "UNKNOWN"
    assert (await rig.drone.hold()).reason_code is ReasonCode.EXECUTION_UNKNOWN


async def test_backend_exception_is_unknown_and_survives_restart(rig):
    pending = await rig.drone.arm()
    token = rig.token(pending)
    async def uncertain(sink): raise RuntimeError("transport lost after send")
    rig.adapter.after_sink = uncertain
    result = await rig.drone.arm(request_id=pending.request_id, approval_token=token)
    assert result.execution_status == "UNKNOWN" and result.partial_effects
    assert rig.adapter.telemetry.armed is False  # no invented successful state
    assert len(rig.adapter.sinks) == 1
    path = rig.store.path
    rig.store.close()
    with SafetyStore(path) as reopened:
        assert reopened.has_uncertain_execution("test-vehicle")
        assert reopened.get_request(pending.request_id)[2] == "UNKNOWN"
        # A restarted gateway stays latched; it does not retry or auto-recover.
        from mavlink_mcp.drone import Drone
        from mavlink_mcp.safety.gateway import SafetyGateway
        from tests.conftest import MockAdapter
        fresh = MockAdapter()
        gateway = SafetyGateway(store=reopened, policy=rig.policy, approval_store=rig.gateway.approval_store,
                                adapter=fresh, context=replace(rig.context, boot_epoch=reopened.boot_epoch))
        assert (await Drone(fresh, safety_gateway=gateway).land()).reason_code is ReasonCode.EXECUTION_UNKNOWN
        assert fresh.sinks == []


@pytest.mark.parametrize("caller_cancel", [False, True])
async def test_cancellation_resistant_dispatch_cannot_reach_later_sink(rig_factory, caller_cancel):
    rig = rig_factory(dispatch_timeout=0.03)
    rig.adapter.telemetry = replace(rig.adapter.telemetry, armed=True)
    pending = await rig.drone.takeoff(10)
    token = rig.token(pending)
    reached, release = asyncio.Event(), asyncio.Event()
    async def resist(sink):
        if sink == "set_takeoff_altitude":
            reached.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
    rig.adapter.after_sink = resist
    task = asyncio.create_task(rig.drone.takeoff(10, request_id=pending.request_id, approval_token=token))
    await asyncio.wait_for(reached.wait(), 1)
    if caller_cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
    else:
        result = await asyncio.wait_for(task, 1)
        assert result.execution_status == "UNKNOWN"
    # Denial applies while the stubborn task still exists, no retry or disarm.
    assert (await rig.drone.hold()).decision is PolicyDecision.DENY
    assert rig.gateway._dispatch_tasks
    unfinished = tuple(rig.gateway._dispatch_tasks)
    release.set()
    await asyncio.wait_for(asyncio.gather(*unfinished, return_exceptions=True), 1)
    assert [s[0] for s in rig.adapter.sinks] == ["set_takeoff_altitude"]
    assert rig.store.get_request(pending.request_id)[2] == "UNKNOWN"


async def test_concurrent_token_reuse_dispatches_once(rig):
    pending = await rig.drone.arm()
    token = rig.token(pending)
    async def yield_once(sink): await asyncio.sleep(0)
    rig.adapter.after_sink = yield_once
    results = await asyncio.gather(*[
        rig.drone.arm(request_id=pending.request_id, approval_token=token) for _ in range(4)])
    assert sum(r.execution_status == "ACKNOWLEDGED" for r in results) == 1
    assert [s[0] for s in rig.adapter.sinks] == ["arm"]
