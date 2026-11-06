"""Queued, in-process MAVSDK contract tests. Never instantiate a real System."""
from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
import inspect
from types import SimpleNamespace as NS

import pytest

from mavlink_mcp.adapter import AdapterError, ExecutionLease, LeaseError
from mavlink_mcp.capabilities import Position, Waypoint
from mavlink_mcp.safety.monitor import MonitorState, assess_snapshot

sdk = pytest.importorskip("mavlink_mcp.adapters.mavsdk_adapter")

TELEMETRY = ("position", "attitude_euler", "battery", "gps_info", "flight_mode", "armed", "in_air", "home")
STREAMS = ("connection_state", *TELEMETRY, "mission_progress")
END = object()


async def until(predicate):
    """Yield scheduler turns, not elapsed time, with a bounded failure."""
    for _ in range(500):
        if predicate():
            return
        turn = asyncio.get_running_loop().create_future()
        asyncio.get_running_loop().call_soon(turn.set_result, None)
        await turn
    pytest.fail("queued fake did not reach the expected state")


class Clock:
    def __init__(self):
        self.now = 10.0
        self.reads = 0

    def __call__(self):
        self.reads += 1
        return self.now


class QueueStream:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.task = None
        self.delivered = 0
        self.closed = False
        self.resist_cancel = False
        self.cancelled = asyncio.Event()

    def send(self, value):
        self.queue.put_nowait(value)

    def __aiter__(self):
        return self

    async def __anext__(self):
        self.task = asyncio.current_task()
        while True:
            try:
                value = await self.queue.get()
                break
            except asyncio.CancelledError:
                self.cancelled.set()
                if not self.resist_cancel:
                    raise
        if value is END:
            raise StopAsyncIteration
        if isinstance(value, BaseException):
            raise value
        self.delivered += 1
        return value

    async def aclose(self):
        self.closed = True


class FakeSystem:
    def __init__(self, *, auto_discover=True):
        self.streams = {name: QueueStream() for name in STREAMS}
        self.opened = {name: [] for name in STREAMS}
        self.core = NS(connection_state=lambda: self.open("connection_state"))
        self.telemetry = NS(**{name: (lambda n=name: self.open(n)) for name in TELEMETRY})
        self.writes = []
        self.before_write = None
        self.after_write = None
        self.action = NS(**{name: self.writer(name) for name in (
            "arm", "disarm", "set_takeoff_altitude", "takeoff", "land", "hold",
            "return_to_launch", "goto_location",
        )})
        self.finished_calls = 0
        self.finished_started = asyncio.Event()
        self.finished_answers = asyncio.Queue()
        self.finished_block = False
        self.finished_resist_cancel = False
        self.finished_cancelled = asyncio.Event()
        self.finished_task = None
        self.mission = NS(mission_progress=lambda: self.open("mission_progress"),
                          is_mission_finished=self.is_mission_finished,
                          upload_mission=self.writer("upload_mission"),
                          start_mission=self.writer("start_mission"))
        self.auto_discover = auto_discover
        self.connect_calls = []
        self.connect_started = asyncio.Event()
        self.connect_gate = asyncio.Event()
        self.connect_gate.set()
        self.connect_resist_cancel = False
        self.connect_cancelled = asyncio.Event()
        self.close_started = asyncio.Event()
        self.close_gate = asyncio.Event()
        self.close_gate.set()
        self.close_resist_cancel = False
        self.close_cancelled = asyncio.Event()
        self.closed = False
        self.close_error = None

    def open(self, name):
        if self.opened[name]:
            self.streams[name] = QueueStream()
        stream = self.streams[name]
        self.opened[name].append(stream)
        return stream

    def writer(self, name):
        async def write(*args):
            if self.before_write:
                self.before_write(name)
            self.writes.append((name, args))
            if self.after_write:
                await self.after_write(name)
        return write

    async def is_mission_finished(self):
        self.finished_calls += 1
        self.finished_task = asyncio.current_task()
        self.finished_started.set()
        if not self.finished_block:
            return False
        while True:
            try:
                answer = await self.finished_answers.get()
                if isinstance(answer, Exception):
                    raise answer
                return answer
            except asyncio.CancelledError:
                self.finished_cancelled.set()
                if not self.finished_resist_cancel:
                    raise

    async def connect(self, *, system_address):
        self.connect_calls.append(system_address)
        self.connect_started.set()
        while True:
            try:
                await self.connect_gate.wait()
                break
            except asyncio.CancelledError:
                self.connect_cancelled.set()
                if not self.connect_resist_cancel:
                    raise
        if self.auto_discover:
            self.streams["connection_state"].send(NS(is_connected=True))

    async def close(self):
        self.close_started.set()
        while True:
            try:
                await self.close_gate.wait()
                break
            except asyncio.CancelledError:
                self.close_cancelled.set()
                if not self.close_resist_cancel:
                    raise
        self.closed = True
        if self.close_error:
            raise self.close_error


@pytest.fixture
async def rig(monkeypatch):
    systems = []
    adapters = []
    clock = Clock()

    def create(**options):
        system = FakeSystem(**options)
        systems.append(system)
        monkeypatch.setattr(sdk, "System", lambda: system)
        adapter = sdk.MavsdkAdapter(clock=clock, connect_timeout_s=0.2)
        adapters.append(adapter)
        return adapter, system, clock

    yield create
    # Always unblock resistant fakes, including when a test assertion failed.
    for system in systems:
        system.connect_gate.set()
        system.close_gate.set()
        system.finished_resist_cancel = False
        system.finished_answers.put_nowait(False)
        for streams in system.opened.values():
            for stream in streams:
                stream.resist_cancel = False
                stream.send(END)
    for adapter in adapters:
        await adapter.disconnect()
        await until(lambda a=adapter: not a._retired_tasks)


async def connected(rig):
    adapter, system, clock = rig()
    await adapter.connect()
    await until(lambda: all(system.opened.values()))
    return adapter, system, clock


def position(**changes):
    values = dict(latitude_deg=47.4, longitude_deg=8.5, absolute_altitude_m=488.0, relative_altitude_m=0.0)
    values.update(changes)
    return NS(**values)


async def seed(adapter, system):
    samples = {
        "position": position(), "home": position(),
        "attitude_euler": NS(roll_deg=0.0, pitch_deg=0.0, yaw_deg=0.0),
        "battery": NS(voltage_v=12.0, remaining_percent=80.0),
        "gps_info": NS(num_satellites=12, fix_type=NS(name="FIX_3D")),
        "flight_mode": NS(name="HOLD"), "armed": False, "in_air": False,
    }
    for name, value in samples.items():
        system.streams[name].send(value)
    await until(lambda: len(adapter.safety_snapshot().telemetry.metadata.fields) == 8)
    return samples


def health(adapter, clock):
    observation = adapter.safety_snapshot()
    return assess_snapshot(observation.telemetry, now_monotonic=clock.now,
                           connection_epoch=adapter.connection_epoch, home_epoch=adapter.home_epoch,
                           connected=observation.connected, monitor_healthy=observation.monitor_healthy,
                           expected_source="MAVSDK")


async def test_concurrent_connect_creates_only_one_system_and_one_subscription_set(rig, monkeypatch):
    adapter, system, _ = rig()
    constructed = []

    def factory():
        constructed.append(system)
        return system

    monkeypatch.setattr(sdk, "System", factory)
    await asyncio.gather(*(adapter.connect() for _ in range(10)))
    await until(lambda: all(system.opened.values()))
    assert constructed == [system]
    assert len(system.connect_calls) == 1
    assert all(len(streams) == 1 for streams in system.opened.values())
    assert adapter.connection_epoch == 1
    await adapter.connect()
    assert len(constructed) == 1


async def test_queries_are_cache_only_and_individual_receipts_do_not_refresh(rig):
    adapter, system, clock = await connected(rig)
    assert (await adapter.get_telemetry()).position is None
    assert await adapter.get_home() is None
    with pytest.raises(AdapterError, match="not reported"):
        await adapter.get_flight_mode()
    await seed(adapter, system)
    first = adapter.safety_snapshot()
    assert health(adapter, clock).healthy
    clock.now = 20.0
    reads = clock.reads
    assert adapter.safety_snapshot() == first
    assert await adapter.get_telemetry() == first.telemetry
    assert await adapter.get_home() == first.home
    assert await adapter.get_flight_mode() == "HOLD"
    assert clock.reads == reads
    assert system.finished_calls == 0
    assert all(len(streams) == 1 for streams in system.opened.values())
    assert not health(adapter, clock).healthy
    system.streams["position"].send(position(relative_altitude_m=2.0))
    await until(lambda: adapter.safety_snapshot().telemetry.position.relative_altitude_m == 2.0)
    second = adapter.safety_snapshot()
    assert second.telemetry.metadata.fields["position"].received_monotonic == 20.0
    assert second.telemetry.metadata.fields["armed"].received_monotonic == 10.0
    assert second.telemetry.metadata.fields["home"].received_monotonic == 10.0
    assert "armed" in health(adapter, clock).stale_fields
    assert "coherence" in health(adapter, clock).stale_fields


async def test_cached_sensor_values_and_home_are_immutable_defensive_copies(rig):
    adapter, system, _ = await connected(rig)
    samples = await seed(adapter, system)
    before = adapter.safety_snapshot()
    samples["position"].latitude_deg = -42.0
    samples["home"].absolute_altitude_m = 999.0
    samples["attitude_euler"].yaw_deg = 180.0
    samples["battery"].remaining_percent = 0.0
    samples["gps_info"].fix_type.name = "NO_FIX"
    assert adapter.safety_snapshot() == before
    for value, name in ((before.telemetry.position, "latitude_deg"),
                        (before.telemetry.attitude, "yaw_deg"),
                        (before.telemetry.battery, "voltage_v"),
                        (before.telemetry.gps, "fix_type")):
        with pytest.raises(FrozenInstanceError):
            setattr(value, name, 0)
        assert not hasattr(value, "__dict__")
    with pytest.raises(TypeError):
        before.home_anchor["home_epoch"] = "999"
    with pytest.raises(TypeError):
        before.telemetry.metadata.fields["position"] = None


@pytest.mark.parametrize("api,value,field", [
    ("position", position(latitude_deg=float("nan")), "position"),
    ("home", position(absolute_altitude_m=float("inf")), "home"),
    ("attitude_euler", NS(roll_deg=0, pitch_deg=91, yaw_deg=0), "attitude"),
    ("battery", NS(voltage_v=12, remaining_percent=101), "battery"),
    ("gps_info", NS(num_satellites=True, fix_type=NS(name="FIX_3D")), "gps"),
    ("flight_mode", float("nan"), "flight_mode"),
    ("flight_mode", True, "flight_mode"), ("armed", 1, "armed"), ("in_air", "true", "in_air"),
])
async def test_malformed_sensor_values_fail_closed(rig, api, value, field):
    adapter, system, clock = await connected(rig)
    await seed(adapter, system)
    clock.now = 10.1
    system.streams[api].send(value)
    await until(lambda: adapter.safety_snapshot().telemetry.metadata.fields[field].received_monotonic == 10.1)
    assert field in health(adapter, clock).invalid_fields
    assert not health(adapter, clock).healthy
    if field == "home":
        assert adapter.safety_snapshot().home_anchor is None


async def test_home_epoch_changes_only_for_canonical_anchor_or_connection_changes(rig):
    adapter, system, clock = await connected(rig)
    await seed(adapter, system)
    before = adapter.safety_snapshot()
    assert before.home_anchor["absolute_altitude_amsl_mm"] == 488000
    assert "absolute_altitude_mm" not in before.home_anchor
    epoch = adapter.home_epoch
    clock.now += 0.1
    system.streams["home"].send(position(latitude_deg=47.400000001, absolute_altitude_m=488.00001,
                                          relative_altitude_m=20.0))
    await until(lambda: adapter.safety_snapshot().home.relative_altitude_m == 20.0)
    assert adapter.home_epoch == epoch
    assert adapter.safety_snapshot().home_anchor == before.home_anchor
    system.streams["home"].send(position(absolute_altitude_m=488.002))
    await until(lambda: adapter.home_epoch > epoch)
    after = adapter.safety_snapshot()
    assert adapter.home_epoch == epoch + 1
    assert after.telemetry.metadata.fields["home"].home_epoch == adapter.home_epoch
    assert after.home_anchor["home_epoch"] == str(adapter.home_epoch)
    assert before.home_anchor["absolute_altitude_amsl_mm"] == 488000


@pytest.mark.parametrize("api", (*TELEMETRY, "mission_progress"))
@pytest.mark.parametrize("failure", [END, RuntimeError("stream died"), "cancel"])
async def test_any_dead_or_ended_subscription_fails_closed_with_fresh_receipts(rig, api, failure):
    adapter, system, clock = await connected(rig)
    await seed(adapter, system)
    stream = system.streams[api]
    assert health(adapter, clock).healthy
    if failure == "cancel":
        stream.task.cancel()
        # Even before its cancellation handler gets a turn, fail closed.
        assert not adapter.safety_snapshot().monitor_healthy
    else:
        stream.send(failure)
    await until(lambda: not adapter.safety_snapshot().monitor_healthy)
    assert health(adapter, clock).state is MonitorState.FAULT
    assert adapter.is_connected()  # A live link alone is not a healthy monitor.
    assert system.writes == []


@pytest.mark.parametrize("failure", [END, RuntimeError("link died"), NS(is_connected=1), "cancel"])
async def test_dead_connection_watcher_clears_cache_and_connection(rig, failure):
    adapter, system, clock = await connected(rig)
    await seed(adapter, system)
    if failure == "cancel":
        system.streams["connection_state"].task.cancel()
    else:
        system.streams["connection_state"].send(failure)
    await until(lambda: not adapter.is_connected())
    assert adapter.safety_snapshot().home is None
    assert adapter.safety_snapshot().telemetry.position is None
    assert not health(adapter, clock).healthy


async def test_link_drop_clears_before_await_and_recovery_reuses_system_but_not_cache(rig, monkeypatch):
    adapter, system, _ = await connected(rig)
    await seed(adapter, system)
    monkeypatch.setattr(sdk, "_CLEANUP_TIMEOUT_S", 0.01)
    epoch, home_epoch = adapter.connection_epoch, adapter.home_epoch
    old_stream = system.streams["position"]
    old_stream.resist_cancel = True
    system.streams["connection_state"].send(NS(is_connected=False))
    await until(lambda: old_stream.cancelled.is_set())
    assert not adapter.is_connected()
    assert adapter.safety_snapshot().telemetry.position is None
    assert adapter.safety_snapshot().home_anchor is None
    waiter = asyncio.create_task(adapter.connect())
    # Receipt after cancellation must not resurrect the disconnected cache.
    old_stream.send(position(latitude_deg=-1))
    await until(lambda: old_stream.closed)
    assert adapter.safety_snapshot().telemetry.position is None
    system.streams["connection_state"].send(NS(is_connected=True))
    await waiter
    await until(lambda: len(system.opened["position"]) == 2)
    assert len(system.connect_calls) == 1
    assert adapter.connection_epoch == epoch + 1
    assert adapter.home_epoch == home_epoch + 1
    assert adapter.safety_snapshot().telemetry.position is None
    await seed(adapter, system)
    assert adapter.safety_snapshot().telemetry.position.latitude_deg == 47.4


async def test_explicit_reconnect_ignores_late_old_generation_callbacks(rig, monkeypatch):
    adapter, old_system, _ = await connected(rig)
    await seed(adapter, old_system)
    monkeypatch.setattr(sdk, "_CLEANUP_TIMEOUT_S", 0.01)
    old = old_system.streams["position"]
    old.resist_cancel = True
    await adapter.disconnect()
    assert old.task in adapter._retired_tasks
    assert adapter.safety_snapshot().telemetry.position is None
    _, new_system, _ = rig()
    await adapter.connect()
    await until(lambda: all(new_system.opened.values()))
    await seed(adapter, new_system)
    current = adapter.safety_snapshot()
    old.send(position(latitude_deg=-20))
    await until(lambda: old.closed and old.task not in adapter._retired_tasks)
    assert adapter.safety_snapshot() == current
    assert old_system.closed


async def test_cancelled_disconnect_keeps_cleanup_and_resistant_tasks_referenced(rig, monkeypatch):
    adapter, system, _ = await connected(rig)
    await seed(adapter, system)
    monkeypatch.setattr(sdk, "_CLEANUP_TIMEOUT_S", 0.01)
    old = system.streams["position"]
    old.resist_cancel = True
    system.close_gate.clear()
    system.close_resist_cancel = True
    disconnect = asyncio.create_task(adapter.disconnect())
    await until(lambda: old.cancelled.is_set())
    disconnect.cancel()
    with pytest.raises(asyncio.CancelledError):
        await disconnect
    assert not adapter.is_connected()
    assert adapter.safety_snapshot().telemetry.position is None
    assert old.task in adapter._retired_tasks
    await asyncio.wait_for(system.close_started.wait(), 0.5)
    await asyncio.wait_for(system.close_cancelled.wait(), 0.5)
    assert adapter._retired_tasks
    old.send(position())
    system.close_gate.set()
    await until(lambda: system.closed and not adapter._retired_tasks)
    assert adapter.safety_snapshot().telemetry.position is None


async def test_old_delayed_cleanup_failure_does_not_poison_new_generation(rig, monkeypatch):
    adapter, old_system, _ = await connected(rig)
    monkeypatch.setattr(sdk, "_CLEANUP_TIMEOUT_S", 0.01)
    old_system.close_gate.clear()
    old_system.close_resist_cancel = True
    old_system.close_error = RuntimeError("late close failure")
    disconnect = asyncio.create_task(adapter.disconnect())
    await old_system.close_started.wait()
    disconnect.cancel()
    with pytest.raises(asyncio.CancelledError):
        await disconnect
    _, new_system, clock = rig()
    await adapter.connect()
    await until(lambda: all(new_system.opened.values()))
    await seed(adapter, new_system)
    old_system.close_gate.set()
    await until(lambda: old_system.closed and not adapter._retired_tasks)
    assert health(adapter, clock).healthy


@pytest.mark.parametrize("cancel", [False, True])
async def test_connection_initialization_timeout_or_cancellation_is_bounded_and_guarded(rig, cancel):
    adapter, system, _ = rig()
    adapter._connect_timeout_s = 0.01
    system.connect_gate.clear()
    system.connect_resist_cancel = True
    connect = asyncio.create_task(adapter.connect())
    await system.connect_started.wait()
    if cancel:
        connect.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else AdapterError):
        await asyncio.wait_for(connect, 0.5)
    assert not adapter.is_connected()
    assert adapter._system is None
    assert adapter._retired_tasks
    system.connect_gate.set()
    await until(lambda: not adapter._retired_tasks)
    assert not any(system.opened.values())
    assert system.closed
    assert adapter.safety_snapshot().telemetry.position is None


async def test_ended_connection_before_discovery_fails_without_waiting_full_timeout(rig):
    adapter, system, _ = rig(auto_discover=False)
    system.streams["connection_state"].send(END)
    with pytest.raises(AdapterError, match="stream ended"):
        await adapter.connect()
    assert adapter._system is None
    assert system.closed


async def test_mission_progress_stream_and_finished_rpc_have_independent_receipts(rig):
    adapter, system, clock = await connected(rig)
    await seed(adapter, system)
    system.finished_block = True
    progress = NS(current=1, total=2)
    system.streams["mission_progress"].send(progress)
    await system.finished_started.wait()
    pending = adapter.mission_snapshot()
    assert (pending.current, pending.total, pending.finished) == (1, 2, None)
    assert pending.progress_metadata.received_monotonic == 10
    assert pending.finished_metadata is None
    progress.current = 999  # Backend objects must not survive an await by reference.
    clock.now = 10.2
    system.finished_answers.put_nowait(True)
    await until(lambda: adapter.mission_snapshot().finished is True)
    result = adapter.mission_snapshot()
    assert (result.current, result.total) == (1, 2)
    assert result.progress_metadata.received_monotonic == 10
    assert result.finished_metadata.received_monotonic == 10.2
    reads = clock.reads
    clock.now = 100
    assert adapter.mission_snapshot() is result
    assert system.finished_calls == 1
    assert clock.reads == reads
    assert system.writes == []


@pytest.mark.parametrize("answer", ["true", RuntimeError("RPC failure")])
async def test_invalid_or_failed_finished_rpc_faults_monitor(rig, answer):
    adapter, system, _ = await connected(rig)
    system.finished_block = True
    system.finished_answers.put_nowait(answer)
    system.streams["mission_progress"].send(NS(current=0, total=1))
    await until(lambda: not adapter.safety_snapshot().monitor_healthy)
    assert adapter.mission_snapshot().finished is None


async def test_finished_rpc_timeout_retains_resistant_task_and_late_result_is_ignored(rig, monkeypatch):
    adapter, system, _ = await connected(rig)
    monkeypatch.setattr(sdk, "_MISSION_OBSERVATION_TIMEOUT_S", 0.01)
    system.finished_block = True
    system.finished_resist_cancel = True
    system.streams["mission_progress"].send(NS(current=0, total=1))
    await asyncio.wait_for(system.finished_cancelled.wait(), 0.5)
    assert system.finished_task in adapter._retired_tasks
    assert not adapter.safety_snapshot().monitor_healthy
    system.finished_answers.put_nowait(True)
    await until(lambda: system.finished_task not in adapter._retired_tasks)
    assert adapter.mission_snapshot().finished is None


def test_installed_mission_api_shapes_without_instantiating_system():
    from mavsdk.mission import Mission
    assert inspect.isasyncgenfunction(Mission.mission_progress)
    assert inspect.iscoroutinefunction(Mission.is_mission_finished)


def mint(adapter, sinks, parameters=None, *, recheck=None):
    """Test-only trusted issuer; no production authorization bypass."""
    issuer = getattr(adapter, "_lease_issuer", None) or object()
    adapter._bind_gateway(issuer, "test-vehicle")
    return ExecutionLease("test-lease", "test-request", "test-action", "test-vehicle", 1000,
                          parameters or {}, sinks, _issuer=issuer, _clock=lambda: 0,
                          _monotonic=lambda: 0, _deadline=1,
                          _recheck=recheck or (lambda sink: None))


WP = Waypoint(47.4, 8.5, 10.0)
OPERATIONS = [
    ("arm", (), {}, ("arm",), {}), ("disarm", (), {}, ("disarm",), {}),
    ("takeoff", (10.0,), {}, ("set_takeoff_altitude", "takeoff"), {"altitude_m": 10.0}),
    ("land", (), {}, ("land",), {}), ("hold", (), {}, ("hold",), {}),
    ("return_to_launch", (), {}, ("return_to_launch",), {}),
    ("goto", (WP,), {"absolute_altitude_m": 498.0}, ("goto_location",),
     {"waypoint": WP, "yaw_deg": 0.0, "absolute_altitude_m": 498.0}),
    ("fly_mission", ([WP],), {}, ("upload_mission", "start_mission"), {"waypoints": (WP,)}),
]


@pytest.mark.parametrize("name,args,kwargs,sinks,parameters", OPERATIONS)
async def test_all_ten_physical_sinks_consume_bound_lease_immediately_before_write(rig, name, args, kwargs, sinks, parameters):
    adapter, system, _ = await connected(rig)
    assert adapter.protected is True
    with pytest.raises(LeaseError):
        await getattr(adapter, name)(*args, **kwargs)
    assert system.writes == []
    events = []
    lease = mint(adapter, sinks, parameters, recheck=lambda sink: events.append(("check", sink)))
    system.before_write = lambda sink: events.append(("write", sink))
    await getattr(adapter, name)(*args, lease=lease, **kwargs)
    assert events == [event for sink in sinks for event in (("check", sink), ("write", sink))]
    assert lease.consumed_sinks == len(sinks)
    before = list(system.writes)
    with pytest.raises(LeaseError):
        await getattr(adapter, name)(*args, lease=lease, **kwargs)
    assert system.writes == before


@pytest.mark.parametrize("name,args,kwargs,sinks,parameters", [OPERATIONS[2], OPERATIONS[-1]])
@pytest.mark.parametrize("failure", ["revoke", "error", "cancel", "reconnect"])
async def test_two_stage_writes_never_dispatch_second_sink_after_failure(rig, name, args, kwargs, sinks, parameters, failure):
    adapter, system, _ = await connected(rig)
    lease = mint(adapter, sinks, parameters)

    async def after_write(sink):
        if sink == sinks[0]:
            if failure == "revoke":
                lease.revoke()
            elif failure == "error":
                raise RuntimeError("backend refused")
            elif failure == "cancel":
                raise asyncio.CancelledError
            else:
                await adapter.disconnect()
                rig()  # Patch the factory to a new fake System.
                await adapter.connect()

    system.after_write = after_write
    with pytest.raises(asyncio.CancelledError if failure == "cancel" else AdapterError):
        await getattr(adapter, name)(*args, lease=lease, **kwargs)
    assert [write[0] for write in system.writes] == [sinks[0]]
    assert lease.consumed_sinks == 1
