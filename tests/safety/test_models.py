import pytest
from mavlink_mcp.safety.models import ContractError, Waypoint, Mission, canonical_bytes, mission_digest

def test_waypoint_normalizes_to_integer_units_and_is_immutable():
    wp = Waypoint.normalize({'latitude_deg': 1.25, 'longitude_deg': -2, 'relative_altitude_m': 10, 'speed_m_s': 2.5, 'yaw_deg': 90})
    assert wp.latitude_e7 == 12_500_000 and wp.relative_altitude_mm == 10_000 and wp.speed_mm_s == 2500
    with pytest.raises(Exception): wp.latitude_e7 = 1

def test_strict_numeric_rejections():
    for value in (True, float('nan'), float('inf'), '10'):
        with pytest.raises(ContractError): Waypoint.normalize({'latitude_deg': value, 'longitude_deg': 0, 'relative_altitude_m': 1})

def test_unknown_fields_precision_and_digest_order():
    with pytest.raises(ContractError): Waypoint.normalize({'latitude_deg': 0, 'longitude_deg': 0, 'relative_altitude_m': 1, 'extra': 1})
    with pytest.raises(ContractError): Waypoint.normalize({'latitude_deg': 0.00000001, 'longitude_deg': 0, 'relative_altitude_m': 1})
    a = [{'latitude_deg': 0, 'longitude_deg': 0, 'relative_altitude_m': 1}]
    b = [{'latitude_deg': 0, 'longitude_deg': 0, 'relative_altitude_m': 2}]
    assert mission_digest(a) != mission_digest(b)

def test_canonical_json_is_restricted_and_ascii_sorted():
    assert canonical_bytes({'z': 'é', 'a': 1}) == b'{"a":1,"z":"\\u00e9"}'
    with pytest.raises(ContractError): canonical_bytes({'x': 1.2})
