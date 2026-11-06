import pytest
from mavlink_mcp.safety.config import load_policy
from mavlink_mcp.safety.models import ContractError

def test_policy_snapshot_is_hashed_and_changes_with_values():
    a = load_policy({'environment': 'mock'})
    b = load_policy({'environment': 'mock', 'max_altitude_m': 60_000})
    assert a.policy_hash != b.policy_hash

def test_invalid_policy_is_rejected_without_fallback():
    with pytest.raises(ContractError): load_policy({'environment': 'unknown'})
    with pytest.raises(ContractError): load_policy({'max_altitude_m': 0})
    with pytest.raises(ContractError): load_policy({'unexpected': 1})
