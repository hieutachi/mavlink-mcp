import pytest
from mavlink_mcp.safety.authorization import Principal, Role, TrustedContext, authorize
from mavlink_mcp.safety.models import Action, PolicyDecision, ReasonCode

def context(role, readonly=True):
    return TrustedContext(Principal('agent', role, 'vehicle', readonly), 'mock', 'gateway', 'boot')

@pytest.mark.parametrize('role', list(Role))
def test_roles_do_not_self_grant_mutation(role):
    result = authorize(context(role), Action.ARM)
    if role is Role.FLIGHT_AGENT:
        assert result.reason_code is ReasonCode.READONLY
    else:
        assert result.reason_code is ReasonCode.UNAUTHORIZED

def test_writable_flight_agent_requires_approval():
    result = authorize(context(Role.FLIGHT_AGENT, False), Action.TAKEOFF)
    assert result.decision is PolicyDecision.REQUIRE_APPROVAL
