import pytest

from mavlink_mcp.adapters.sim_adapter import SimAdapter
from mavlink_mcp.safety.monitor import MonitorState, assess_snapshot


class Clock:
    def __init__(self):
        self.now = 0.0
        self.reads = 0

    def __call__(self):
        self.reads += 1
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.mark.asyncio
async def test_sim_connection_epochs_change_only_on_new_generation():
    clock = Clock()
    adapter = SimAdapter(clock=clock)
    assert adapter.connection_epoch == 0
    await adapter.connect()
    first = adapter.connection_epoch
    home = adapter.home_epoch
    await adapter.connect()
    assert adapter.connection_epoch == first
    assert adapter.home_epoch == home
    await adapter.get_telemetry()
    await adapter.disconnect()
    assert not adapter.safety_snapshot().connected
    assert adapter.safety_snapshot().telemetry.position is None
    await adapter.connect()
    assert adapter.connection_epoch == first + 1
    assert adapter.home_epoch == home + 1
    assert adapter.safety_snapshot().home is None
    assert adapter.safety_snapshot().telemetry.position is None


@pytest.mark.asyncio
async def test_sim_new_stationary_observation_is_fresh_but_cache_query_is_not():
    clock = Clock()
    adapter = SimAdapter(clock=clock)
    await adapter.connect()
    first = await adapter.get_telemetry()
    clock.advance(10)
    reads = clock.reads
    observation = adapter.safety_snapshot()
    assert clock.reads == reads
    assert observation.telemetry is first
    assert assess_snapshot(first, now_monotonic=10.0, connection_epoch=1, home_epoch=1).state is MonitorState.DEGRADED
    second = await adapter.get_telemetry()
    assert second.metadata.captured_monotonic == 10
    assert second.position == first.position
    assert second.battery is second.gps is None
    assert second.metadata.fields["home"].received_monotonic == 10
    assert assess_snapshot(second, now_monotonic=10.0, connection_epoch=1, home_epoch=1).healthy
    third = await adapter.get_telemetry()
    assert third == second
    assert third.metadata.fields == second.metadata.fields


@pytest.mark.asyncio
async def test_home_and_mode_queries_are_cache_only():
    clock = Clock()
    adapter = SimAdapter(clock=clock)
    await adapter.connect()
    assert await adapter.get_home() is None
    await adapter.get_telemetry()
    before = adapter.safety_snapshot()
    clock.advance(10)
    reads = clock.reads
    assert await adapter.get_home() == before.home
    assert await adapter.get_flight_mode() == before.telemetry.flight_mode
    assert adapter.safety_snapshot() == before
    assert clock.reads == reads
