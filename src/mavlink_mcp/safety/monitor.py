"""Pure, fail-closed observation quality checks; never authorization or I/O."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math

from mavlink_mcp.capabilities import (
    Attitude, Battery, GpsInfo, Position, TelemetryField, TelemetryMetadata,
    TelemetrySnapshot,
)


class MonitorState(str, Enum):
    DISCONNECTED = "DISCONNECTED"
    INITIALIZING = "INITIALIZING"
    READY = "READY"
    DEGRADED = "DEGRADED"
    FAULT = "FAULT"


@dataclass(frozen=True, slots=True)
class HealthSnapshot:
    state: MonitorState
    connection_epoch: int
    home_epoch: int
    heartbeat_monotonic: float | None
    stale_fields: tuple[str, ...] = ()
    missing_fields: tuple[str, ...] = ()
    invalid_fields: tuple[str, ...] = ()
    max_age_s: float | None = None
    max_skew_s: float | None = None
    reason: str = ""

    @property
    def healthy(self) -> bool:
        return self.state is MonitorState.READY and not (
            self.stale_fields or self.missing_fields or self.invalid_fields
        )

    @property
    def dangerous_actions_allowed(self) -> bool:
        """Compatibility alias for health ONLY; does not authorize any action."""
        return self.healthy


def _finite(value: object) -> bool:
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _range(value: object, low: float, high: float) -> bool:
    return _finite(value) and low <= value <= high


def valid_observation_value(name: str, value: object) -> bool:
    """Validate actual values even when a producer marks metadata valid.

    These are representation/range checks, not flight policy (battery reserve,
    GPS fix quality, geofence, armed state, etc. are separately enforced).
    Home itself is outside TelemetrySnapshot; its adapter validates it before
    setting the home receipt's valid bit, and the gateway checks the anchor.
    """
    if name in {"position", "home"}:
        return isinstance(value, Position) and all((
            _range(value.latitude_deg, -90, 90),
            _range(value.longitude_deg, -180, 180),
            _range(value.absolute_altitude_m, -1_000_000, 1_000_000),
            _range(value.relative_altitude_m, -1_000_000, 1_000_000),
        ))
    if name == "attitude":
        return isinstance(value, Attitude) and all((
            _range(value.roll_deg, -180, 180),
            _range(value.pitch_deg, -90, 90),
            _range(value.yaw_deg, -360, 360),
        ))
    if name == "battery":
        return isinstance(value, Battery) and (
            _finite(value.voltage_v) and value.voltage_v > 0
            and _range(value.remaining_percent, 0, 100)
        )
    if name == "gps":
        return isinstance(value, GpsInfo) and (
            type(value.num_satellites) is int and 0 <= value.num_satellites <= 255
            and type(value.fix_type) is str and value.fix_type in {
                "NO_GPS", "NO_FIX", "FIX_2D", "FIX_3D", "FIX_DGPS",
                "DGPS", "RTK_FLOAT", "RTK_FIXED",
            }
        )
    if name in {"armed", "in_air"}:
        return type(value) is bool
    if name == "flight_mode":
        return (type(value) is str and bool(value.strip())
                and len(value) <= 64 and value.upper() != "UNKNOWN")
    return False


@dataclass(frozen=True, slots=True)
class MonitorConfig:
    max_age_s: float = 1.0
    max_skew_s: float = 0.5
    required_fields: tuple[str, ...] = ("position", "flight_mode", "armed", "in_air", "home")

    def __post_init__(self) -> None:
        if not _finite(self.max_age_s) or self.max_age_s <= 0:
            raise ValueError("max_age_s must be finite and positive")
        if not _finite(self.max_skew_s) or self.max_skew_s < 0:
            raise ValueError("max_skew_s must be finite and non-negative")
        fields = tuple(self.required_fields)
        if not fields or any(name not in {
            "position", "attitude", "battery", "gps", "flight_mode", "armed", "in_air", "home",
        } for name in fields):
            raise ValueError("required_fields must name supported observations")
        object.__setattr__(self, "required_fields", fields)


def assess_snapshot(
    snapshot: TelemetrySnapshot | None,
    *,
    now_monotonic: float,
    connection_epoch: int,
    home_epoch: int,
    connected: bool = True,
    config: MonitorConfig = MonitorConfig(),
    monitor_healthy: bool = True,
    expected_source: str | None = None,
) -> HealthSnapshot:
    """Check cached receipts using the adapter's monotonic clock.

    No timestamp is refreshed or future receipt clamped. All supplied values
    and metadata are checked, including optional sensors; required_fields
    controls which missing fields fail closed. Home validity/freshness is
    checked through its receipt; the caller must also validate the home anchor.
    """
    if connected is False:
        return HealthSnapshot(MonitorState.DISCONNECTED, connection_epoch, home_epoch, None, reason="not connected")
    if (connected is not True or monitor_healthy is not True
            or not _finite(now_monotonic)
            or type(connection_epoch) is not int or connection_epoch < 0
            or type(home_epoch) is not int or home_epoch < 0):
        return HealthSnapshot(MonitorState.FAULT, connection_epoch, home_epoch, None,
                              invalid_fields=("monitor",), reason="monitor or clock is unhealthy")
    if snapshot is not None and not isinstance(snapshot, TelemetrySnapshot):
        return HealthSnapshot(MonitorState.FAULT, connection_epoch, home_epoch, None,
                              invalid_fields=("snapshot",), reason="invalid observation snapshot")
    if snapshot is None or snapshot.metadata is None:
        return HealthSnapshot(MonitorState.INITIALIZING, connection_epoch, home_epoch, None,
                              missing_fields=config.required_fields, reason="no observation envelope")
    if not isinstance(snapshot.metadata, TelemetryMetadata):
        return HealthSnapshot(MonitorState.FAULT, connection_epoch, home_epoch, None,
                              invalid_fields=("metadata",), reason="invalid observation envelope")

    metadata = snapshot.metadata
    missing: set[str] = set()
    stale: set[str] = set()
    invalid: set[str] = set()
    ages: list[float] = []
    receipts: list[float] = []
    captured_valid = _finite(metadata.captured_monotonic) and metadata.captured_monotonic <= now_monotonic
    if not captured_valid:
        invalid.add("captured_monotonic")
    if type(metadata.connection_epoch) is not int or metadata.connection_epoch != connection_epoch:
        stale.add("connection_epoch")
    if type(metadata.home_epoch) is not int or metadata.home_epoch != home_epoch:
        stale.add("home_epoch")
    if (metadata.source not in ("MAVSDK", "SYNTHETIC")
            or (expected_source is not None and metadata.source != expected_source)
            or type(metadata.synthetic) is not bool
            or metadata.synthetic != (metadata.source == "SYNTHETIC")):
        invalid.add("source")

    names = set(config.required_fields) | set(metadata.fields)
    names.update(name for name in ("position", "attitude", "battery", "gps", "flight_mode", "armed", "in_air")
                 if getattr(snapshot, name) is not None)
    for name in names:
        field = metadata.fields.get(name)
        value = getattr(snapshot, name, None)
        if field is None or (name != "home" and value is None):
            missing.add(name)
        if name != "home" and value is not None and not valid_observation_value(name, value):
            invalid.add(name)
        if field is None:
            continue
        if not isinstance(field, TelemetryField):
            invalid.add(name)
            continue
        if type(field.connection_epoch) is not int or field.connection_epoch != connection_epoch:
            stale.add(name)
        if field.home_epoch is not None and (type(field.home_epoch) is not int or field.home_epoch != home_epoch):
            stale.add(name)
        if name == "home" and field.home_epoch is None:
            invalid.add(name)
        if (field.valid is not True or field.source != metadata.source
                or (expected_source is not None and field.source != expected_source)):
            invalid.add(name)
        # Source timestamps may use a different clock; only test finiteness.
        if field.source_timestamp is not None and not _finite(field.source_timestamp):
            invalid.add(name)
        receipt = field.received_monotonic
        if (not _finite(receipt) or receipt > now_monotonic
                or (captured_valid and receipt > metadata.captured_monotonic)):
            invalid.add(name)
            continue
        age = now_monotonic - receipt
        ages.append(age)
        receipts.append(receipt)
        if age > config.max_age_s:
            stale.add(name)
    skew = max(receipts) - min(receipts) if receipts else None
    if skew is not None and skew > config.max_skew_s:
        stale.add("coherence")
    state = MonitorState.READY if not (missing or stale or invalid) else MonitorState.DEGRADED
    return HealthSnapshot(
        state, connection_epoch, home_epoch,
        metadata.captured_monotonic if captured_valid else None,
        tuple(sorted(stale)), tuple(sorted(missing)), tuple(sorted(invalid)),
        max(ages) if ages else None, skew,
        "healthy observation (not authorization)" if state is MonitorState.READY
        else "observation is missing, stale, invalid, epoch-mismatched, or incoherent",
    )
