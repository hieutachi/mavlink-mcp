"""Admission models float32 rounding; pin that to the installed MAVSDK protobufs."""
import struct
from dataclasses import replace

import pytest
from google.protobuf.descriptor import FieldDescriptor as F

from mavlink_mcp.safety.admission import AdmissionError, check_targets
from mavlink_mcp.safety.config import PolicyConfig
from mavlink_mcp.safety.models import Action, Environment, FrozenRequest, ReasonCode, normalize_parameters

FLOAT, DOUBLE = F.TYPE_FLOAT, F.TYPE_DOUBLE


@pytest.mark.parametrize("message,expected", [
    ("action_pb2.SetTakeoffAltitudeRequest", {"altitude": FLOAT}),
    ("action_pb2.GotoLocationRequest", {"latitude_deg": DOUBLE, "longitude_deg": DOUBLE,
                                        "absolute_altitude_m": FLOAT, "yaw_deg": FLOAT}),
    ("mission_pb2.MissionItem", {"latitude_deg": DOUBLE, "longitude_deg": DOUBLE,
                                 "relative_altitude_m": FLOAT, "speed_m_s": FLOAT}),
])
def test_installed_mavsdk_wire_types_match_admission_model(message, expected):
    import importlib
    module, name = message.split(".")
    cls = getattr(importlib.import_module(f"mavsdk.{module}"), name)
    actual = {f.name: f.type for f in cls.DESCRIPTOR.fields}
    for field, kind in expected.items():
        assert actual[field] == kind, f"{message}.{field} changed; revisit check_targets"


def _request(action, params, home_mm=488_000):
    return FrozenRequest(1, "r", "a", "v", "g", "b", Environment.PX4_SITL, "1", "1", "p", action,
                         normalize_parameters(action, params),
                         home_anchor={"latitude_e7": 473977420, "longitude_e7": 85455940,
                                      "absolute_altitude_amsl_mm": home_mm, "vertical_datum": "AMSL",
                                      "home_epoch": "1"})


def test_goto_altitude_is_checked_after_float32_rounding_of_absolute_altitude():
    cfg = PolicyConfig(readonly=False, max_altitude_m=50)
    point = {"latitude_deg": 47.397742, "longitude_deg": 8.545594, "yaw_deg": 0}
    check_targets(_request(Action.GOTO, {**point, "relative_altitude_m": 49.9}), cfg)
    # The margin, not exact equality, decides the boundary.
    with pytest.raises(AdmissionError) as exc:
        check_targets(_request(Action.GOTO, {**point, "relative_altitude_m": 49.96}), cfg)
    assert exc.value.reason is ReasonCode.ALTITUDE_LIMIT
    # Within the anchor's +/-1e6 m range float32 spacing is <= 0.0625 m, so the
    # worst rounding error (0.03125 m) stays below the 50 mm default margin.
    f32 = lambda v: struct.unpack("!f", struct.pack("!f", v))[0]
    base = 999_999.0
    for rel in (1.0, 10.001, 33.333, 49.9):
        assert abs(f32(base + rel) - base - rel) <= 0.03125
    check_targets(_request(Action.GOTO, {**point, "relative_altitude_m": 10.001}, home_mm=999_999_000), cfg)
    assert abs(f32(488.0 + 49.9) - 488.0 - 49.9) < 0.0001


def test_mission_speed_is_checked_after_float32_rounding():
    cfg = PolicyConfig(readonly=False, max_speed_mm_s=5000)
    wp = {"latitude_deg": 47.397742, "longitude_deg": 8.545594, "relative_altitude_m": 10}
    check_targets(_request(Action.FLY_MISSION, {"waypoints": [{**wp, "speed_m_s": 5}]}), cfg)
    with pytest.raises(AdmissionError):
        check_targets(_request(Action.FLY_MISSION, {"waypoints": [{**wp, "speed_m_s": 5.001}]}), cfg)
