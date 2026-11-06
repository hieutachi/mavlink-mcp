"""Deterministic kinematic integration tests; no sleeping or hardware.

Every write goes through the real SafetyGateway with a signed Ed25519
approval minted on the same injected clock the simulator flies on, so
approval freshness and synthetic motion never drift apart.
"""

import math

import pytest

from mavlink_mcp.adapter import AdapterError
from mavlink_mcp.adapters.sim_adapter import SimAdapter
from mavlink_mcp.capabilities import Waypoint
from mavlink_mcp.drone import Drone
from mavlink_mcp.guardrails import GuardrailConfig
from mavlink_mcp.safety.models import PolicyDecision, ReasonCode


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def sim(rig_factory):
    """One simulator, one ledger, one clock for motion and approval deadlines."""
    clock = Clock()
    adapter = SimAdapter(clock=clock)
    rig = rig_factory(
        adapter, monotonic=clock, clock=lambda: int(1_000_000 + clock() * 1000)
    )
    return rig, clock


async def airborne(sim, altitude=10.0):
    rig, clock = sim
    await rig.run(rig.drone.arm)
    await rig.run(rig.drone.takeoff, altitude)
    clock.advance(altitude / 2)
    assert (await rig.drone.get_telemetry()).flight_mode == "HOLD"
    return rig, clock


def wp_at(adapter, east=0.0, north=0.0, up=10.0, speed=None):
    """Synthetic ENU helper; public coordinates carry at most 7 decimals."""
    home = adapter.simulation_info()["home"]
    return Waypoint(
        round(home["latitude_deg"] + math.degrees(north / 6_371_000), 7),
        round(
            home["longitude_deg"]
            + math.degrees(
                east / (6_371_000 * math.cos(math.radians(home["latitude_deg"])))
            ),
            7,
        ),
        up,
        speed,
    )


async def test_initial_telemetry_is_explicitly_synthetic(sim):
    rig, clock = sim
    adapter = rig.adapter
    assert not adapter.is_connected()
    state = await rig.drone.get_telemetry()
    assert state.position.relative_altitude_m == 0
    assert state.battery is None and state.gps is None
    assert state.armed is False and state.in_air is False
    info = adapter.simulation_info()
    assert info["synthetic"] is True and info["network_enabled"] is False
    assert info["energy_model"] is None
    clock.advance(100)
    assert (await rig.drone.get_telemetry()).position == state.position


async def test_takeoff_moves_over_time_not_on_ack(sim):
    rig, clock = sim
    drone, adapter = rig.drone, rig.adapter
    grounded = await drone.takeoff(10, confirm=True)
    assert grounded.decision is PolicyDecision.DENY
    assert grounded.reason_code is ReasonCode.UNSAFE_STATE
    # The simulator keeps its own physics rule, and nothing was dispatched.
    with pytest.raises(AdapterError, match="arm before"):
        await adapter.takeoff(10)
    assert adapter.simulation_info()["local_position_enu_m"] == [0, 0, 0]

    await rig.run(drone.arm)
    # confirm is not authorization: it only ever yields an approval request.
    pending = await drone.takeoff(10, confirm=True)
    assert pending.decision is PolicyDecision.REQUIRE_APPROVAL
    assert pending.execution_status == "NOT_STARTED"
    assert (await drone.get_telemetry()).flight_mode == "READY"

    await rig.run(drone.takeoff, 10)
    state = await drone.get_telemetry()
    assert state.position.relative_altitude_m == 0
    assert state.flight_mode == "TAKEOFF"
    clock.advance(2)
    assert (await drone.get_telemetry()).position.relative_altitude_m == 4

    assert (await drone.disarm()).reason_code is ReasonCode.UNSAFE_STATE
    with pytest.raises(AdapterError, match="disarm in air"):
        await adapter.disarm()
    assert (await drone.takeoff(12)).reason_code is ReasonCode.UNSAFE_STATE
    with pytest.raises(AdapterError, match="already airborne"):
        await adapter.takeoff(12)
    unchanged = await drone.get_telemetry()
    assert unchanged.position.relative_altitude_m == 4 and unchanged.armed is True

    clock.advance(3)
    assert (await drone.get_telemetry()).position.relative_altitude_m == 10
    assert await drone.get_flight_mode() == "HOLD"


async def test_navigation_requires_airborne(sim):
    rig, _ = sim
    drone, adapter = rig.drone, rig.adapter
    target = wp_at(adapter)
    grounded = await drone.goto(target.latitude_deg, target.longitude_deg, 10)
    assert grounded.reason_code is ReasonCode.UNSAFE_STATE
    await rig.run(drone.arm)
    assert (await drone.fly_mission([target])).reason_code is ReasonCode.UNSAFE_STATE
    with pytest.raises(AdapterError, match="take off"):
        await adapter.fly_mission([target])
    assert adapter.simulation_info()["mission"]["state"] == "idle"
    assert (await drone.get_telemetry()).in_air is False


async def test_goto_hold_and_land(sim):
    rig, clock = await airborne(sim)
    drone, adapter = rig.drone, rig.adapter
    target = wp_at(adapter, east=50)
    wrapped_yaw = await drone.goto(
        target.latitude_deg, target.longitude_deg, 10, yaw_deg=450
    )
    assert wrapped_yaw.reason_code is ReasonCode.INVALID_ARGUMENT
    await rig.run(drone.goto, target.latitude_deg, target.longitude_deg, 10, yaw_deg=90)
    clock.advance(2)
    before = await drone.get_telemetry()
    assert before.attitude.yaw_deg == 90
    assert adapter.simulation_info()["local_position_enu_m"][0] == pytest.approx(
        10, abs=0.05
    )
    await rig.run(drone.hold)
    clock.advance(100)
    assert (await drone.get_telemetry()).position == before.position
    await rig.run(drone.land)
    clock.advance(2)
    assert (await drone.get_telemetry()).position.relative_altitude_m == 6
    clock.advance(3)
    landed = await drone.get_telemetry()
    assert not landed.in_air and not landed.armed
    await rig.run(drone.disarm)
    assert await drone.get_flight_mode() == "READY"


async def test_mission_progress_speed_and_return_home(sim):
    rig, clock = await airborne(sim)
    drone, adapter = rig.drone, rig.adapter
    await rig.run(drone.fly_mission, [
        wp_at(adapter, east=20, speed=2),
        wp_at(adapter, east=20, north=10, speed=5),
    ])
    clock.advance(5)
    assert adapter.simulation_info()["local_position_enu_m"][0] == pytest.approx(
        10, abs=0.05
    )
    assert adapter.simulation_info()["mission"]["completed_waypoints"] == 0
    clock.advance(5.01)
    assert adapter.simulation_info()["mission"]["completed_waypoints"] == 1
    clock.advance(2)
    assert (await drone.get_telemetry()).flight_mode == "HOLD"
    assert adapter.simulation_info()["mission"] == {
        "state": "completed", "completed_waypoints": 2, "total_waypoints": 2,
    }
    await rig.run(drone.return_to_launch)
    assert (await drone.get_telemetry()).flight_mode == "RTL"
    clock.advance(100)
    state = await drone.get_telemetry()
    assert state.position.relative_altitude_m == 0
    assert state.position.latitude_deg == adapter.simulation_info()["home"]["latitude_deg"]
    assert state.position.longitude_deg == adapter.simulation_info()["home"]["longitude_deg"]
    assert state.in_air is False and state.armed is False


@pytest.mark.parametrize("operation", ["hold", "land", "return_to_launch"])
async def test_recovery_cancels_mission_under_signed_approval(sim, operation):
    rig, clock = await airborne(sim)
    drone, adapter = rig.drone, rig.adapter
    await rig.run(drone.fly_mission, [
        wp_at(adapter, north=50), wp_at(adapter, east=50),
    ])
    clock.advance(1)
    # Recovery is not an authorization bypass; it is signed like any other write.
    await rig.run(getattr(drone, operation))
    assert adapter.simulation_info()["mission"]["state"] == "cancelled"
    clock.advance(100)
    assert (await drone.get_telemetry()).flight_mode != "MISSION"


async def test_no_gateway_keeps_all_mutation_blocked_even_in_sim(sim, monkeypatch):
    rig, clock = await airborne(sim)
    adapter = rig.adapter
    monkeypatch.delenv("MAVLINK_MCP_READONLY", raising=False)
    readonly = Drone(adapter)
    assert readonly.guardrails.readonly is True
    for operation in ("hold", "land", "return_to_launch", "disarm"):
        result = await getattr(readonly, operation)()
        assert result.decision is PolicyDecision.DENY
        assert result.reason_code is ReasonCode.READONLY
    assert (await readonly.takeoff(20, confirm=True)).reason_code is ReasonCode.READONLY
    # Nothing physical happened: still hovering at the same point, still armed.
    assert adapter.simulation_info()["local_position_enu_m"] == [0, 0, 10]
    state = await readonly.get_telemetry()
    assert state.armed is True and state.flight_mode == "HOLD"
    # The gateway-backed writer still recovers, with a real signed approval.
    await rig.run(rig.drone.land)
    clock.advance(5)
    assert (await rig.drone.get_telemetry()).in_air is False


async def test_invalid_mission_is_atomic_and_keeps_previous_plan(sim):
    rig, clock = await airborne(sim)
    drone, adapter = rig.drone, rig.adapter
    await rig.run(drone.fly_mission, [wp_at(adapter, north=20)])
    outside = [wp_at(adapter, east=10), wp_at(adapter, north=500)]
    # A running mission is not a launch state, so the loaded plan stays intact.
    mid_flight = await drone.fly_mission(outside)
    assert mid_flight.reason_code is ReasonCode.UNSAFE_STATE
    assert adapter.simulation_info()["mission"]["total_waypoints"] == 1
    clock.advance(5)  # The approved single-waypoint plan finishes, back to HOLD.
    denied = await drone.fly_mission(outside)
    assert denied.decision is PolicyDecision.DENY
    assert denied.reason_code is ReasonCode.OUTSIDE_GEOFENCE
    clock.advance(5)
    info = adapter.simulation_info()
    # The rejected plan's reachable first leg was never uploaded either.
    assert info["local_position_enu_m"][0] == 0
    assert info["local_position_enu_m"][1] == pytest.approx(20, abs=0.05)
    assert info["mission"] == {
        "state": "completed", "completed_waypoints": 1, "total_waypoints": 1,
    }


async def test_disconnect_freezes_and_reconnect_does_not_reset(sim):
    rig, clock = await airborne(sim)
    drone, adapter = rig.drone, rig.adapter
    await rig.run(drone.fly_mission, [wp_at(adapter, east=50)])
    clock.advance(1)
    await adapter.disconnect()
    before = adapter.simulation_info()["local_position_enu_m"]
    clock.advance(200)
    await adapter.connect()
    assert adapter.simulation_info()["local_position_enu_m"] == before
    assert adapter.simulation_info()["mission"]["state"] == "cancelled"
    assert (await drone.get_telemetry()).armed is True


async def test_identical_waypoints_and_large_time_jump_terminate(sim):
    rig, clock = await airborne(sim)
    target = wp_at(rig.adapter)
    await rig.run(rig.drone.fly_mission, [target] * 1000)
    clock.advance(100)
    assert rig.adapter.simulation_info()["mission"]["completed_waypoints"] == 1000
    assert (await rig.drone.get_telemetry()).flight_mode == "HOLD"


async def test_instances_do_not_share_state(sim):
    await airborne(sim)
    other = Drone(SimAdapter(), GuardrailConfig())
    assert not (await other.get_telemetry()).armed


async def test_unrepresentable_duration_preserves_existing_mission(sim):
    rig, clock = await airborne(sim)
    drone, adapter = rig.drone, rig.adapter
    await rig.run(drone.fly_mission, [wp_at(adapter, north=20)])
    # Canonical mm/s normalization rejects the subnormal speed before dispatch.
    subnormal = await drone.fly_mission([wp_at(adapter, east=20, speed=1e-309)])
    assert subnormal.decision is PolicyDecision.DENY
    assert subnormal.reason_code is ReasonCode.INVALID_ARGUMENT
    # The simulator independently refuses a duration it cannot represent.
    with pytest.raises(AdapterError, match="not representable"):
        await adapter.fly_mission([wp_at(adapter, east=20, speed=1e-309)])
    clock.advance(5)
    assert adapter.simulation_info()["local_position_enu_m"][1] == pytest.approx(
        20, abs=0.05
    )
    assert adapter.simulation_info()["mission"]["state"] == "completed"


async def test_telemetry_and_metadata_share_one_clock_instant(sim, monkeypatch):
    import mavlink_mcp.server as server

    rig, clock = await airborne(sim)
    drone, adapter = rig.drone, rig.adapter
    await rig.run(drone.land)
    monkeypatch.setattr(server, "_drone", drone)

    def ticking_clock():
        clock.advance(1)
        return clock.now

    adapter._clock = ticking_clock
    for _ in range(6):
        state = await server.get_telemetry()
        assert state["position"]["relative_altitude_m"] == (
            state["simulation"]["local_position_enu_m"][2]
        )
