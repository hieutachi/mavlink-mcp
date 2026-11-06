"""Vendor-neutral UAV capability model.

This module defines *what* a drone can do, independent of the protocol or
library used to talk to it. Every type here is expressed in physical
quantities (degrees, meters, volts) and open-standard semantics — no
protocol messages, no vendor API shapes. Adapters (see
:mod:`mavlink_mcp.adapter`) translate these capabilities to a concrete
backend such as MAVSDK/MAVLink; future adapters (e.g. ROS 2) implement the
same interface.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_EVEN
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping
import math


class Capability(str, Enum):
    """The capabilities a UAV adapter may support (MVP1 set)."""

    TELEMETRY = "telemetry"
    FLIGHT_MODE = "flight_mode"
    ARM_DISARM = "arm_disarm"
    TAKEOFF = "takeoff"
    LAND = "land"
    GOTO = "goto"
    MISSION = "mission"
    RETURN_TO_LAUNCH = "return_to_launch"


class CapabilityNotSupported(Exception):
    """Raised when an operation needs a capability the adapter lacks."""


@dataclass(frozen=True, slots=True)
class Position:
    """Global position (WGS84)."""

    latitude_deg: float
    longitude_deg: float
    absolute_altitude_m: float
    relative_altitude_m: float


@dataclass(frozen=True, slots=True)
class Attitude:
    """Vehicle attitude as Euler angles."""

    roll_deg: float
    pitch_deg: float
    yaw_deg: float


@dataclass(frozen=True, slots=True)
class Battery:
    """Battery state."""

    voltage_v: float
    remaining_percent: float


@dataclass(frozen=True, slots=True)
class GpsInfo:
    """GPS receiver state."""

    num_satellites: int
    fix_type: str


@dataclass(frozen=True, slots=True)
class TelemetryField:
    """Receipt metadata for one cached observation.

    ``received_monotonic`` measures local receipt time only. It is not proof
    that the vehicle sensor produced a fresh value or that the value is
    authentic.
    """

    received_monotonic: float
    source: str
    connection_epoch: int
    valid: bool = True
    source_timestamp: float | None = None
    home_epoch: int | None = None


@dataclass(frozen=True, slots=True)
class TelemetryMetadata:
    """Immutable observation envelope accompanying a telemetry snapshot."""

    captured_monotonic: float
    source: str
    connection_epoch: int
    home_epoch: int
    fields: Mapping[str, TelemetryField] = field(default_factory=dict)
    synthetic: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "fields", MappingProxyType(dict(self.fields)))

    def age(self, field: str, *, now: float) -> float | None:
        observation = self.fields.get(field)
        # Do not hide future receipts by clamping negative ages to zero.
        return None if observation is None else now - observation.received_monotonic


@dataclass(frozen=True, slots=True)
class TelemetrySnapshot:
    """One consistent snapshot of the vehicle's telemetry.

    Fields that the vehicle did not report within the collection window
    are ``None`` rather than stale or invented values. ``metadata`` is
    optional for compatibility with adapters that have no observation cache.
    """

    position: Position | None
    attitude: Attitude | None
    battery: Battery | None
    gps: GpsInfo | None
    flight_mode: str | None
    armed: bool | None
    in_air: bool | None
    metadata: TelemetryMetadata | None = None


def canonical_home_anchor(home: Position, home_epoch: int) -> Mapping[str, Any]:
    """Quantize measured home, not command input, using decimal half-even.

    Sub-e7/mm sensor jitter and relative altitude do not change the anchor.
    This is an AMSL reference, not an ellipsoid/geoid conversion.
    """
    def quantize(value: float, scale: int, low: float, high: float) -> int:
        if (type(value) not in (int, float) or not low <= value <= high
                or not math.isfinite(value)):
            raise ValueError("invalid home measurement")
        return int((Decimal(str(value)) * scale).to_integral_value(rounding=ROUND_HALF_EVEN))

    if type(home_epoch) is not int or home_epoch < 0:
        raise ValueError("home_epoch must be a non-negative integer")
    return MappingProxyType({
        "latitude_e7": quantize(home.latitude_deg, 10_000_000, -90, 90),
        "longitude_e7": quantize(home.longitude_deg, 10_000_000, -180, 180),
        "absolute_altitude_amsl_mm": quantize(home.absolute_altitude_m, 1000, -1_000_000, 1_000_000),
        "vertical_datum": "AMSL",
        "home_epoch": str(home_epoch),
    })


def _freeze_observation(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_observation(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_observation(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze_observation(item) for item in value)
    if value is None or type(value) in (str, int, float, bool):
        return value
    raise TypeError("observation mappings must contain immutable scalar values")


@dataclass(frozen=True, slots=True)
class VehicleObservation:
    """One read-only adapter cache view. Reading it performs no I/O.

    Health is an observation-quality signal, never permission to actuate.
    Home reference validation against an approved plan belongs to the gateway.
    """

    telemetry: TelemetrySnapshot
    home: Position | None
    home_anchor: Mapping[str, Any] | None
    connected: bool
    monitor_healthy: bool

    def __post_init__(self) -> None:
        if self.home_anchor is not None:
            object.__setattr__(self, "home_anchor", _freeze_observation(self.home_anchor))


@dataclass(frozen=True, slots=True)
class MissionObservation:
    """Observed mission state, not proof a particular command completed.

    Progress and the finished RPC have independent receipt metadata. Callers
    must check freshness and bind the observation to their execution attempt.
    """

    current: int | None = None
    total: int | None = None
    finished: bool | None = None
    progress_metadata: TelemetryField | None = None
    finished_metadata: TelemetryField | None = None


@dataclass(frozen=True, slots=True)
class Waypoint:
    """A single target location, altitude relative to the launch point."""

    latitude_deg: float
    longitude_deg: float
    relative_altitude_m: float
    speed_m_s: float | None = None
