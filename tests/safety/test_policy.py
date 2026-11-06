import pytest
from mavlink_mcp.safety.authorization import Principal, Role, TrustedContext
from mavlink_mcp.safety.config import load_policy
from mavlink_mcp.safety.models import Action, PolicyDecision, ReasonCode, normalize_parameters
from mavlink_mcp.safety.policy import evaluate


def ctx(environment='mock', readonly=False, role=Role.FLIGHT_AGENT):
    return TrustedContext(Principal('agent', role, 'v', readonly), environment, 'g', 'b')


WRITABLE = {'environment': 'mock', 'readonly': False, 'max_altitude_m': 50}


def point(**changes):
    return dict(normalize_parameters(Action.GOTO, {'latitude_deg': 0, 'longitude_deg': 0,
                                                   'relative_altitude_m': 10, **changes}))


def test_policy_is_pure_and_checks_environment_battery_and_limits():
    policy = load_policy(WRITABLE)
    takeoff = lambda mm, **kw: evaluate(ctx(), policy, Action.TAKEOFF, {'altitude_mm': mm}, **kw)
    assert takeoff(10_000, battery_percent=100).decision is PolicyDecision.REQUIRE_APPROVAL
    # The ceiling keeps a numerical margin; 50 m exactly is not admitted.
    assert takeoff(49_950).decision is PolicyDecision.REQUIRE_APPROVAL
    assert takeoff(49_951).reason_code is ReasonCode.ALTITUDE_LIMIT
    assert takeoff(51_000).reason_code is ReasonCode.ALTITUDE_LIMIT
    assert takeoff(10_000, battery_percent=10).reason_code is ReasonCode.UNSAFE_STATE
    for bad in (True, float('nan'), -1, 101):
        assert takeoff(10_000, battery_percent=bad).reason_code is ReasonCode.INVALID_ARGUMENT
    assert evaluate(ctx('px4_sitl'), policy, Action.TAKEOFF, {'altitude_mm': 10_000}).reason_code is ReasonCode.POLICY_CHANGED
    # Recovery actions stay available on low battery.
    assert evaluate(ctx(), policy, Action.LAND, {}, battery_percent=5).decision is PolicyDecision.REQUIRE_APPROVAL


def test_readonly_is_default_and_principal_or_config_readonly_denies():
    for action in Action:
        assert evaluate(ctx(), load_policy({'environment': 'mock'}), action, {}).reason_code is ReasonCode.READONLY
    assert evaluate(ctx(readonly=True), load_policy(WRITABLE), Action.LAND, {}).reason_code is ReasonCode.READONLY
    assert evaluate(ctx(role=Role.OBSERVER), load_policy(WRITABLE), Action.LAND, {}).reason_code is ReasonCode.UNAUTHORIZED


def test_policy_accepts_only_canonical_integer_units():
    policy = load_policy(WRITABLE)
    raw = {'waypoints': [{'latitude_deg': 0, 'longitude_deg': 0, 'relative_altitude_m': 10}]}
    assert evaluate(ctx(), policy, Action.FLY_MISSION, raw).reason_code is ReasonCode.INVALID_ARGUMENT
    assert evaluate(ctx(), policy, Action.TAKEOFF, {'altitude_mm': True}).reason_code is ReasonCode.INVALID_ARGUMENT
    assert evaluate(ctx(), policy, Action.TAKEOFF, {'altitude_mm': 10.0}).reason_code is ReasonCode.INVALID_ARGUMENT
    assert evaluate(ctx(), policy, Action.LAND, {'altitude_mm': 1}).reason_code is ReasonCode.INVALID_ARGUMENT
    assert evaluate(ctx(), policy, Action.GOTO, point()).decision is PolicyDecision.REQUIRE_APPROVAL


def test_policy_rejects_invalid_waypoint():
    policy = load_policy(WRITABLE)
    high = {'waypoints': [point(relative_altitude_m=100)]}
    assert evaluate(ctx(), policy, Action.FLY_MISSION, high).reason_code is ReasonCode.ALTITUDE_LIMIT
    fast = dict(normalize_parameters(Action.FLY_MISSION, {'waypoints': [
        {'latitude_deg': 0, 'longitude_deg': 0, 'relative_altitude_m': 10, 'speed_m_s': 6}]}))
    assert evaluate(ctx(), policy, Action.FLY_MISSION, fast).reason_code is ReasonCode.INVALID_ARGUMENT
    extra = {'waypoints': [{**point(), 'extra': 1}]}
    assert evaluate(ctx(), policy, Action.FLY_MISSION, extra).reason_code is ReasonCode.INVALID_ARGUMENT
    assert evaluate(ctx(), policy, Action.FLY_MISSION, {'waypoints': []}).reason_code is ReasonCode.INVALID_ARGUMENT


@pytest.mark.parametrize('params', [
    {'latitude_deg': 0, 'longitude_deg': 0, 'relative_altitude_m': 10, 'speed_m_s': 2},
    {'latitude_deg': 0.00000001, 'longitude_deg': 0, 'relative_altitude_m': 10},
    {'latitude_deg': 0, 'longitude_deg': 0, 'relative_altitude_m': 10, 'yaw_deg': 360},
])
def test_public_goto_normalization_rejects_unsupported_or_imprecise_input(params):
    from mavlink_mcp.safety.models import ContractError
    with pytest.raises(ContractError):
        normalize_parameters(Action.GOTO, params)
