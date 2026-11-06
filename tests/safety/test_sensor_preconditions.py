"""T17, T23, T25: sensor quality, monitor health and airborne emergency limits.

These are gateway-level preconditions measured on the observation cache, not
autopilot failsafes. Passing them does not make a flight safe; failing them
must stop a command before it reaches the vehicle.
"""
from dataclasses import replace

import pytest

from mavlink_mcp.capabilities import Battery, GpsInfo
from mavlink_mcp.safety.models import PolicyDecision, ReasonCode

GOOD_FIXES = ("FIX_3D", "FIX_DGPS", "RTK_FLOAT", "RTK_FIXED")
BAD_FIXES = ("NO_GPS", "NO_FIX", "FIX_2D", "DGPS")


@pytest.fixture
def physical(rig_factory):
    """GPS and battery are only required in the physical (px4_sitl) profile."""
    from tests.conftest import MockAdapter

    adapter = MockAdapter()
    adapter.name = "mavsdk"
    rig = rig_factory(adapter)
    adapter.telemetry = replace(adapter.telemetry, armed=False, in_air=False)
    return rig


@pytest.mark.parametrize("fix", BAD_FIXES)
async def test_degraded_gps_fix_blocks_arming(physical, fix):
    """T17: a weak fix is refused before any sink runs."""
    physical.adapter.telemetry = replace(
        physical.adapter.telemetry, gps=GpsInfo(14, fix))
    result = await physical.drone.arm()
    assert result.decision is PolicyDecision.DENY
    assert result.reason_code is ReasonCode.UNSAFE_STATE
    assert physical.adapter.sinks == []


@pytest.mark.parametrize("satellites", [0, 4, 9])
async def test_too_few_satellites_blocks_arming(physical, satellites):
    """T17: the configured minimum (10) is enforced, not merely reported."""
    physical.adapter.telemetry = replace(
        physical.adapter.telemetry, gps=GpsInfo(satellites, "FIX_3D"))
    result = await physical.drone.arm()
    assert result.reason_code is ReasonCode.UNSAFE_STATE
    assert physical.adapter.sinks == []


@pytest.mark.parametrize("fix", GOOD_FIXES)
async def test_acceptable_gps_fix_allows_a_signed_arm(physical, fix):
    """Positive control: good GPS must not be rejected."""
    physical.adapter.telemetry = replace(
        physical.adapter.telemetry, gps=GpsInfo(12, fix))
    await physical.run(physical.drone.arm)
    assert [sink for sink, _ in physical.adapter.sinks] == ["arm"]


async def test_missing_gps_or_battery_blocks_a_physical_vehicle(physical):
    """A physical profile requires both sensors; absent is not 'probably fine'."""
    physical.adapter.telemetry = replace(physical.adapter.telemetry, gps=None)
    first = await physical.drone.arm()
    assert first.reason_code is ReasonCode.TELEMETRY_INCOMPLETE
    physical.adapter.telemetry = replace(
        physical.adapter.telemetry, gps=GpsInfo(12, "FIX_3D"), battery=None)
    second = await physical.drone.arm()
    assert second.reason_code is ReasonCode.TELEMETRY_INCOMPLETE
    assert physical.adapter.sinks == []


@pytest.mark.parametrize("percent", [0.0, 5.0, 29.0])
async def test_low_battery_blocks_energy_consuming_actions(physical, percent):
    """Arm, takeoff, goto and mission are refused below the reserve."""
    physical.adapter.telemetry = replace(
        physical.adapter.telemetry, battery=Battery(11.1, percent))
    result = await physical.drone.arm()
    assert result.reason_code is ReasonCode.UNSAFE_STATE
    assert physical.adapter.sinks == []


async def test_low_battery_still_permits_recovery(physical):
    """Recovery must stay available precisely when the battery is low."""
    physical.adapter.telemetry = replace(
        physical.adapter.telemetry, battery=Battery(10.8, 7.0),
        armed=True, in_air=True)
    await physical.run(physical.drone.land)
    assert [sink for sink, _ in physical.adapter.sinks] == ["land"]


@pytest.mark.parametrize("voltage", [0.0, -1.0, float("nan"), float("inf")])
async def test_impossible_battery_voltage_is_not_treated_as_healthy(physical, voltage):
    """T23: an invalid reading fails closed instead of passing a range check."""
    physical.adapter.telemetry = replace(
        physical.adapter.telemetry, battery=Battery(voltage, 100.0))
    result = await physical.drone.arm()
    assert result.decision is PolicyDecision.DENY
    assert physical.adapter.sinks == []


async def test_unhealthy_monitor_blocks_every_action(rig):
    """T23: when the monitor cannot vouch for observations, nothing executes."""
    rig.adapter.telemetry = replace(rig.adapter.telemetry, armed=True, in_air=True)
    rig.adapter.monitor_healthy = False
    rig.adapter.observe()
    for method in (rig.drone.arm, rig.drone.disarm, rig.drone.land, rig.drone.hold,
                   rig.drone.return_to_launch):
        result = await method()
        assert result.decision is PolicyDecision.DENY
        assert result.reason_code is ReasonCode.MONITOR_UNHEALTHY
    assert (await rig.drone.takeoff(10)).reason_code is ReasonCode.MONITOR_UNHEALTHY
    assert rig.adapter.sinks == []


async def test_disconnected_vehicle_blocks_every_action(rig):
    """A link that is down is not a reason to retry blind."""
    rig.adapter.telemetry = replace(rig.adapter.telemetry, armed=True, in_air=True)
    rig.adapter.observe()
    rig.adapter.connected = False
    rig.adapter.refresh = False  # Do not let the gateway's observe() reconnect.
    result = await rig.drone.land()
    assert result.decision is PolicyDecision.DENY
    assert rig.adapter.sinks == []


async def test_airborne_disarm_is_refused_but_land_and_rtl_are_not(rig):
    """T25: cutting motors in flight is not an emergency response.

    Disarming an airborne multirotor drops it. The gateway refuses it and
    leaves the two responses that a vehicle can actually survive.
    """
    rig.adapter.telemetry = replace(rig.adapter.telemetry, armed=True, in_air=True)
    rig.adapter.observe()
    refused = await rig.drone.disarm()
    assert refused.decision is PolicyDecision.DENY
    assert refused.reason_code is ReasonCode.UNSAFE_STATE
    assert rig.adapter.sinks == []
    # Both survivable responses remain available under signed approval.
    await rig.run(rig.drone.hold)
    await rig.run(rig.drone.return_to_launch)
    assert [sink for sink, _ in rig.adapter.sinks] == ["hold", "return_to_launch"]


async def test_airborne_disarm_stays_refused_even_with_a_signed_approval(rig):
    """A human cannot sign away flight dynamics."""
    rig.adapter.telemetry = replace(rig.adapter.telemetry, armed=True, in_air=False)
    rig.adapter.observe()
    pending = await rig.drone.disarm()
    token = rig.token(pending)
    # The vehicle lifts off between approval and execution.
    rig.adapter.telemetry = replace(rig.adapter.telemetry, in_air=True)
    rig.adapter.observe()
    result = await rig.drone.disarm(request_id=pending.request_id, approval_token=token)
    assert result.decision is PolicyDecision.DENY
    assert result.reason_code is ReasonCode.UNSAFE_STATE
    assert rig.adapter.sinks == []
