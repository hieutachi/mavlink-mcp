"""Regression: a valid approval must not expire because the clock ticked.

The gateway tells the operator one issue instant. If the ledger re-read the
clock, it could store a later instant and then reject the signature that was
made against the published one. This surfaced intermittently over stdio and is
pinned here with a clock that advances on every read.
"""
import pytest

from mavlink_mcp.safety.models import PolicyDecision, ReasonCode


class TickingClock:
    """Advances on every read, like a real clock crossing a millisecond."""

    def __init__(self, start=1_000_000, step=1):
        self.value = start
        self.step = step
        self.reads = 0

    def __call__(self):
        self.reads += 1
        self.value += self.step
        return self.value


async def test_published_issue_instant_is_what_the_ledger_stores(rig_factory):
    clock = TickingClock()
    rig = rig_factory(clock=clock)
    pending = await rig.drone.arm()
    record = rig.store.get_approval_record(pending.request_id)
    stored = rig.store.get_request(pending.request_id)[0]
    assert pending.approval_request["issued_at_utc_ms"] == record.issued_at_utc_ms
    assert stored["created_at_utc_ms"] == record.issued_at_utc_ms
    assert record.expires_at_utc_ms == record.issued_at_utc_ms + rig.gateway.approval_ttl_ms


@pytest.mark.parametrize("step", [1, 7, 250])
async def test_signed_approval_survives_a_ticking_clock(rig_factory, step):
    clock = TickingClock(step=step)
    rig = rig_factory(clock=clock)
    result = await rig.run(rig.drone.arm)
    assert result.decision is PolicyDecision.ALLOW
    assert [sink for sink, _ in rig.adapter.sinks] == ["arm"]


async def test_expired_window_is_still_refused(rig_factory):
    """The fix must not weaken the deadline itself."""
    clock = TickingClock()
    rig = rig_factory(clock=clock)
    pending = await rig.drone.arm()
    token = rig.token(pending)
    clock.value += rig.gateway.approval_ttl_ms + 1_000
    result = await rig.drone.arm(request_id=pending.request_id, approval_token=token)
    assert result.reason_code is ReasonCode.APPROVAL_EXPIRED
    assert rig.adapter.sinks == []


async def test_ledger_rejects_an_invalid_issue_instant(rig):
    from mavlink_mcp.safety.models import request_from_dict
    from mavlink_mcp.safety.store import ClaimRejected
    pending = await rig.drone.arm()
    request = request_from_dict(rig.store.get_request(pending.request_id)[0])
    for bad in (-1, True, 1.5, None if False else "1000"):
        with pytest.raises(ClaimRejected):
            rig.store.create_approval(request, expires_at_utc_ms=2 ** 40,
                                      required_operator_role="human_operator",
                                      issued_at_utc_ms=bad)
