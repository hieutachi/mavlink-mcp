from dataclasses import replace

import pytest

from mavlink_mcp.capabilities import (
    Attitude, Battery, GpsInfo, Position, TelemetryField, TelemetryMetadata,
    TelemetrySnapshot,
)
from mavlink_mcp.safety.monitor import MonitorConfig, MonitorState, assess_snapshot

FIELDS = ("position", "attitude", "battery", "gps", "flight_mode", "armed", "in_air", "home")


def snapshot(received=10.0, epoch=1):
    fields = {
        name: TelemetryField(received, "SYNTHETIC", epoch, home_epoch=1 if name == "home" else None)
        for name in FIELDS
    }
    return TelemetrySnapshot(
        Position(47.0, 8.0, 500.0, 10.0), Attitude(0.0, 0.0, 0.0),
        Battery(12.0, 80.0), GpsInfo(12, "FIX_3D"), "HOLD", True, True,
        TelemetryMetadata(received, "SYNTHETIC", epoch, 1, fields, synthetic=True),
    )


def assess(current=None, **kwargs):
    options = dict(now_monotonic=10.5, connection_epoch=1, home_epoch=1,
                   expected_source="SYNTHETIC")
    options.update(kwargs)
    return assess_snapshot(snapshot() if current is None else current, **options)


def field_change(name, **changes):
    current = snapshot()
    fields = dict(current.metadata.fields)
    fields[name] = replace(fields[name], **changes)
    return replace(current, metadata=replace(current.metadata, fields=fields))


def test_freshness_uses_receipt_time_and_epoch():
    assert assess().state is MonitorState.READY
    assert assess().healthy
    assert assess().dangerous_actions_allowed  # Compatibility health alias only.
    stale = assess(now_monotonic=12.0)
    assert stale.state is MonitorState.DEGRADED
    assert "position" in stale.stale_fields
    changed = assess(connection_epoch=2)
    assert changed.state is MonitorState.DEGRADED
    assert "position" in changed.stale_fields


@pytest.mark.parametrize("current", [None, replace(snapshot(), metadata=None)])
def test_missing_envelope_fails_closed_without_refreshing_anything(current):
    result = assess_snapshot(current, now_monotonic=100.0, connection_epoch=1, home_epoch=1)
    assert result.state is MonitorState.INITIALIZING
    assert not result.healthy


@pytest.mark.parametrize("name", FIELDS)
def test_each_stale_field(name):
    result = assess(field_change(name, received_monotonic=8.0))
    assert name in result.stale_fields
    assert not result.healthy


@pytest.mark.parametrize("name", FIELDS)
@pytest.mark.parametrize("receipt", [float("nan"), float("inf"), -float("inf"), 11.0, True, "10"])
def test_each_nonfinite_future_or_mistyped_receipt(name, receipt):
    result = assess(field_change(name, received_monotonic=receipt))
    assert name in result.invalid_fields
    assert not result.healthy


@pytest.mark.parametrize("name", FIELDS)
def test_each_field_connection_generation_is_checked(name):
    assert name in assess(field_change(name, connection_epoch=0)).stale_fields


@pytest.mark.parametrize("name,value", [
    ("position", Position(float("nan"), 0, 0, 0)),
    ("position", Position(0, float("inf"), 0, 0)),
    ("position", Position(0, 0, float("nan"), 0)),
    ("position", Position(0, 0, 0, float("inf"))),
    ("position", Position(91, 0, 0, 0)),
    ("position", Position(0, 181, 0, 0)),
    ("position", Position(True, 0, 0, 0)),
    ("position", "position"),
    ("attitude", Attitude(float("nan"), 0, 0)),
    ("attitude", Attitude(0, float("inf"), 0)),
    ("attitude", Attitude(0, 0, float("nan"))),
    ("battery", Battery(float("inf"), 50)),
    ("battery", Battery(12, float("nan"))),
    ("battery", Battery(-1, 50)),
    ("battery", Battery(12, 101)),
    ("gps", GpsInfo(-1, "FIX_3D")),
    ("gps", GpsInfo(True, "FIX_3D")),
    ("gps", GpsInfo(float("nan"), "FIX_3D")),
    ("gps", GpsInfo(10, "made up")),
    ("flight_mode", float("nan")), ("flight_mode", ""), ("flight_mode", "UNKNOWN"),
    ("armed", 1), ("armed", float("nan")), ("in_air", "yes"), ("in_air", 0),
])
def test_valid_bit_does_not_override_bad_actual_value(name, value):
    result = assess(replace(snapshot(), **{name: value}))
    assert name in result.invalid_fields
    assert not result.healthy


@pytest.mark.parametrize("value", [float("nan"), float("inf"), 11.0, True, "10"])
def test_capture_time_is_validated(value):
    current = snapshot()
    assert not assess(replace(current, metadata=replace(current.metadata, captured_monotonic=value))).healthy


@pytest.mark.parametrize("value", [float("nan"), float("inf"), True, "10"])
def test_invalid_monitor_clock_fails_closed(value):
    assert assess(now_monotonic=value).state is MonitorState.FAULT


def test_skew_is_reported_even_when_each_field_is_recent():
    result = assess(field_change("in_air", received_monotonic=9.4), now_monotonic=10.1,
                    config=MonitorConfig(max_skew_s=0.5))
    assert result.state is MonitorState.DEGRADED
    assert "coherence" in result.stale_fields


def test_home_epoch_checked_in_envelope_and_home_receipt():
    current = snapshot()
    result = assess(replace(current, metadata=replace(current.metadata, home_epoch=2)))
    assert "home_epoch" in result.stale_fields
    assert "home" in assess(field_change("home", home_epoch=2)).stale_fields
    assert "home" in assess(field_change("home", home_epoch=None)).invalid_fields
    assert "home" in assess(field_change("home", valid=False)).invalid_fields


def test_envelope_source_and_each_field_source_must_match_expected_backend():
    assert "source" in assess(expected_source="MAVSDK").invalid_fields
    assert "position" in assess(field_change("position", source="MAVSDK")).invalid_fields
    current = snapshot()
    assert "source" in assess(replace(current, metadata=replace(current.metadata, synthetic=False))).invalid_fields


def test_stream_health_is_separate_from_fresh_receipts():
    assert assess(monitor_healthy=False).state is MonitorState.FAULT
    assert assess(connected=False).state is MonitorState.DISCONNECTED


def test_profiles_do_not_invent_unmodelled_sensors():
    current = snapshot()
    fields = {k: v for k, v in current.metadata.fields.items() if k not in {"battery", "gps"}}
    synthetic = replace(current, battery=None, gps=None, metadata=replace(current.metadata, fields=fields))
    assert assess(synthetic).healthy
    physical = assess(synthetic, config=MonitorConfig(required_fields=FIELDS))
    assert set(physical.missing_fields) == {"battery", "gps"}


def test_reading_health_does_not_mutate_receipts():
    current = snapshot()
    before = dict(current.metadata.fields)
    assess(current, now_monotonic=100)
    assert dict(current.metadata.fields) == before
    assert current.metadata.age("position", now=9.0) == -1.0


@pytest.mark.parametrize("name", FIELDS)
def test_receipts_after_capture_are_invalid_even_if_not_in_future_now(name):
    result = assess(field_change(name, received_monotonic=10.25))
    assert name in result.invalid_fields
    assert not result.healthy


@pytest.mark.parametrize("value", [True, 1, "snapshot", object()])
def test_invalid_snapshot_type_fails_closed(value):
    result = assess_snapshot(value, now_monotonic=10, connection_epoch=1, home_epoch=1)
    assert result.state is MonitorState.FAULT
    assert "snapshot" in result.invalid_fields


def test_envelope_defensively_copies_field_mapping():
    fields = dict(snapshot().metadata.fields)
    metadata = TelemetryMetadata(10, "SYNTHETIC", 1, 1, fields, synthetic=True)
    original = metadata.fields["position"]
    fields["position"] = replace(original, received_monotonic=20)
    assert metadata.fields["position"] is original
    with pytest.raises(TypeError):
        metadata.fields["position"] = fields["position"]


@pytest.mark.parametrize("changes", [
    {"max_age_s": 0}, {"max_age_s": float("nan")}, {"max_age_s": True},
    {"max_skew_s": -1}, {"max_skew_s": float("inf")},
    {"required_fields": ()}, {"required_fields": ("invented",)},
])
def test_invalid_monitor_configuration_is_rejected(changes):
    with pytest.raises(ValueError):
        MonitorConfig(**changes)
