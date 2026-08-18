"""Adapter boundary: the interface every UAV backend must implement.

The capability layer (:mod:`mavlink_mcp.drone`) only ever talks to this
interface. The MVP1 backend is MAVSDK/MAVLink
(:mod:`mavlink_mcp.adapters.mavsdk_adapter`); future backends (e.g. a ROS 2
adapter reusing rosbridge-mcp) implement the same class and automatically
inherit the guardrails and the MCP tool surface.

Adapters translate; they do not decide. All policy (readonly mode,
confirmation, geofence, altitude limits) lives above this boundary in the
capability layer — an adapter must never be the place where safety checks
happen.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from mavlink_mcp.capabilities import (
    Capability,
    Position,
    TelemetrySnapshot,
    Waypoint,
)


class AdapterError(Exception):
    """Raised when the backend rejects or fails an operation.

    The message is intended to be shown to the calling agent as-is, so
    adapters should include the backend's own failure reason.
    """


class DroneAdapter(ABC):
    """Abstract UAV backend. One instance controls one vehicle."""

    #: Short backend identifier, e.g. "mavsdk".
    name: str = "abstract"

    @abstractmethod
    def capabilities(self) -> frozenset[Capability]:
        """The set of capabilities this adapter implements."""

    @abstractmethod
    def is_connected(self) -> bool:
        """Whether a vehicle is currently connected."""

    @abstractmethod
    async def connect(self) -> None:
        """Connect to the vehicle. Raise AdapterError on failure/timeout."""

    @abstractmethod
    async def disconnect(self) -> None:
        """Release the connection and any backend resources."""

    # --- Telemetry -------------------------------------------------- #

    @abstractmethod
    async def get_telemetry(self) -> TelemetrySnapshot:
        """Collect one telemetry snapshot (position, attitude, battery, GPS,
        flight mode, armed, in-air)."""

    @abstractmethod
    async def get_flight_mode(self) -> str:
        """Current flight mode as a short string, e.g. "HOLD"."""

    @abstractmethod
    async def get_home(self) -> Position | None:
        """Home/launch position, or None if not yet known."""

    # --- Actions ----------------------------------------------------- #

    @abstractmethod
    async def arm(self) -> None:
        """Arm the vehicle."""

    @abstractmethod
    async def disarm(self) -> None:
        """Disarm the vehicle (autopilots reject this in the air)."""

    @abstractmethod
    async def takeoff(self, altitude_m: float) -> None:
        """Take off to ``altitude_m`` above the launch point."""

    @abstractmethod
    async def land(self) -> None:
        """Land at the current position."""

    @abstractmethod
    async def hold(self) -> None:
        """Pause: stop and loiter at the current position."""

    @abstractmethod
    async def return_to_launch(self) -> None:
        """Fly back to the launch point and land."""

    @abstractmethod
    async def goto(self, waypoint: Waypoint, yaw_deg: float = 0.0) -> None:
        """Fly to a single waypoint and loiter there."""

    @abstractmethod
    async def fly_mission(self, waypoints: list[Waypoint]) -> None:
        """Upload a waypoint mission and start it."""
