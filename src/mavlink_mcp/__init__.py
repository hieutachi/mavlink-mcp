"""mavlink-mcp: vendor-neutral UAV capability layer + MCP server.

Two surfaces over one core:

- **Python library** — ``Drone`` + an adapter, for scripts and notebooks::

    from mavlink_mcp import Drone
    from mavlink_mcp.adapters.mavsdk_adapter import MavsdkAdapter

    drone = Drone(MavsdkAdapter())
    snapshot = await drone.get_telemetry()

- **MCP server** — the ``mavlink-mcp`` console script exposes the same
  capabilities as tools for AI agents (Claude, Cursor, ...).
"""

from mavlink_mcp.adapter import AdapterError, DroneAdapter, LeaseError
from mavlink_mcp.capabilities import (
    Attitude,
    Battery,
    Capability,
    CapabilityNotSupported,
    GpsInfo,
    Position,
    TelemetrySnapshot,
    Waypoint,
)
from mavlink_mcp.drone import Drone
from mavlink_mcp.guardrails import GuardrailConfig, GuardrailViolation

__all__ = [
    "AdapterError",
    "Attitude",
    "Battery",
    "Capability",
    "CapabilityNotSupported",
    "Drone",
    "DroneAdapter",
    "LeaseError",
    "GpsInfo",
    "GuardrailConfig",
    "GuardrailViolation",
    "Position",
    "TelemetrySnapshot",
    "Waypoint",
]

__version__ = "0.1.0"
