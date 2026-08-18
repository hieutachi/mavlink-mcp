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

from dataclasses import dataclass
from enum import Enum


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


@dataclass(frozen=True)
class Position:
    """Global position (WGS84)."""

    latitude_deg: float
    longitude_deg: float
    absolute_altitude_m: float
    relative_altitude_m: float


@dataclass(frozen=True)
class Attitude:
    """Vehicle attitude as Euler angles."""

    roll_deg: float
    pitch_deg: float
    yaw_deg: float


@dataclass(frozen=True)
class Battery:
    """Battery state."""

    voltage_v: float
    remaining_percent: float


@dataclass(frozen=True)
class GpsInfo:
    """GPS receiver state."""

    num_satellites: int
    fix_type: str


@dataclass(frozen=True)
class TelemetrySnapshot:
    """One consistent snapshot of the vehicle's telemetry.

    Fields that the vehicle did not report within the collection window
    are ``None`` rather than stale or invented values.
    """

    position: Position | None
    attitude: Attitude | None
    battery: Battery | None
    gps: GpsInfo | None
    flight_mode: str | None
    armed: bool | None
    in_air: bool | None


@dataclass(frozen=True)
class Waypoint:
    """A single target location, altitude relative to the launch point."""

    latitude_deg: float
    longitude_deg: float
    relative_altitude_m: float
    speed_m_s: float | None = None
