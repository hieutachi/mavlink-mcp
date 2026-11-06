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
from dataclasses import dataclass, field
import math
import time
from types import MappingProxyType
from typing import Any, Callable, Mapping


@dataclass(slots=True)
class _LeaseState:
    next_sink: int = 0
    revoked: bool = False


def _immutable(value):
    if isinstance(value, Mapping):
        return MappingProxyType({key: _immutable(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_immutable(item) for item in value)
    return value

from mavlink_mcp.capabilities import (
    Capability,
    Position,
    TelemetrySnapshot,
    Waypoint,
)


class AdapterError(Exception):
    """Raised when the backend rejects or fails an operation."""


class LeaseError(AdapterError):
    """Raised when a protected sink is invoked without a valid lease."""


@dataclass(frozen=True, slots=True)
class ExecutionLease:
    """Opaque gateway-issued scope for one ordered dispatch attempt.

    Adapters validate the public contract, while the gateway remains the
    authority that creates and revokes leases. A lease never comes from MCP
    input and cannot be reused for another request or sink sequence. The
    mutable cursor prevents ordinary same-process reuse, but this is not a
    cryptographic capability: adversarial Python in the same process can
    mutate or bypass object state. Physical deployments must keep the gateway
    and protected adapter behind a trusted process boundary.
    """

    lease_id: str
    request_id: str
    action: str
    vehicle_id: str
    expires_at_ms: int
    parameters: Mapping[str, Any] = field(default_factory=dict)
    sinks: tuple[str, ...] = ()
    _next_sink: int = 0
    revoked: bool = False
    _state: _LeaseState | None = field(default=None, compare=False, repr=False)
    _issuer: object | None = field(default=None, compare=False, repr=False)
    _clock: Callable[[], int] = field(default=lambda: int(time.time() * 1000), compare=False, repr=False)
    _monotonic: Callable[[], float] = field(default=time.monotonic, compare=False, repr=False)
    _deadline: float = field(default=0.0, compare=False, repr=False)
    _recheck: Callable[[str], None] | None = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "parameters", _immutable(self.parameters))
        object.__setattr__(self, "sinks", tuple(self.sinks))
        if self._state is None:
            object.__setattr__(self, "_state", _LeaseState(self._next_sink, self.revoked))

    @property
    def consumed_sinks(self) -> int:
        return self._state.next_sink

    def for_sink(self, sink: str, parameters: Mapping[str, Any] | None = None,
                 *, issuer: object | None = None, vehicle_id: str | None = None) -> "ExecutionLease":
        state = self._state
        if self._issuer is None or issuer is not self._issuer or vehicle_id != self.vehicle_id:
            raise LeaseError("execution lease issuer or vehicle binding is invalid")
        now = self._monotonic()
        if not math.isfinite(now) or now >= self._deadline or self._clock() >= self.expires_at_ms:
            raise LeaseError("execution lease expired")
        if state.revoked or state.next_sink >= len(self.sinks) or self.sinks[state.next_sink] != sink:
            raise LeaseError(f"execution lease does not authorize sink {sink!r}")
        if parameters is None and state.next_sink == 0 and self.parameters:
            raise LeaseError("the first sink of a parameterized plan must present its values")
        if parameters is not None and _immutable(parameters) != self.parameters:
            raise LeaseError("execution lease parameters do not match the approved plan")
        if self._recheck is None:
            raise LeaseError("execution lease lacks a gateway recheck")
        self._recheck(sink)
        # No await between this gate and the backend call. Recheck on EVERY sink.
        if state.revoked or self._monotonic() >= self._deadline or self._clock() >= self.expires_at_ms:
            raise LeaseError("execution lease revoked or expired during recheck")
        state.next_sink += 1
        return self

    def revoke(self) -> "ExecutionLease":
        self._state.revoked = True
        return self


class DroneAdapter(ABC):
    """Abstract UAV backend. One instance controls one vehicle."""

    #: Short backend identifier, e.g. "mavsdk".
    name: str = "abstract"
    #: Protected adapters require a gateway lease at every physical sink.
    protected: bool = True

    def _bind_gateway(self, issuer: object, vehicle_id: str) -> None:
        """Trusted composition hook, not a client authorization API (G1 only)."""
        current = getattr(self, "_lease_issuer", None)
        if current is not None and current is not issuer:
            raise LeaseError("adapter already belongs to another gateway")
        self._lease_issuer = issuer
        self._lease_vehicle_id = vehicle_id

    def _consume_lease(self, lease: ExecutionLease | None, sink: str,
                       parameters: Mapping[str, Any] | None = None) -> ExecutionLease:
        if not isinstance(lease, ExecutionLease):
            raise LeaseError(f"protected sink {sink!r} requires a gateway execution lease")
        return lease.for_sink(sink, parameters, issuer=getattr(self, "_lease_issuer", None),
                              vehicle_id=getattr(self, "_lease_vehicle_id", None))

    def safety_snapshot(self):
        """No observation support means no protected writes, never fake freshness."""
        return None

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
    async def arm(self, *, lease: ExecutionLease | None = None) -> None:
        """Arm the vehicle under a gateway lease."""

    @abstractmethod
    async def disarm(self, *, lease: ExecutionLease | None = None) -> None:
        """Disarm the vehicle under a gateway lease."""

    @abstractmethod
    async def takeoff(self, altitude_m: float, *, lease: ExecutionLease | None = None) -> None:
        """Take off under a gateway lease."""

    @abstractmethod
    async def land(self, *, lease: ExecutionLease | None = None) -> None:
        """Land under a gateway lease."""

    @abstractmethod
    async def hold(self, *, lease: ExecutionLease | None = None) -> None:
        """Hold under a gateway lease."""

    @abstractmethod
    async def return_to_launch(self, *, lease: ExecutionLease | None = None) -> None:
        """RTL under a gateway lease."""

    @abstractmethod
    async def goto(self, waypoint: Waypoint, yaw_deg: float = 0.0, *, lease: ExecutionLease | None = None, absolute_altitude_m: float | None = None) -> None:
        """Goto under a gateway lease and resolved absolute target."""

    @abstractmethod
    async def fly_mission(self, waypoints: list[Waypoint], *, lease: ExecutionLease | None = None) -> None:
        """Upload/start a mission under a gateway lease."""
