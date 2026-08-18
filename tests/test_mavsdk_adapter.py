"""Smoke tests for the MAVSDK adapter that do NOT need a vehicle.

Anything requiring a live SITL belongs in a separate (future) integration
suite — these tests only cover construction, configuration, and the
declared capability set.
"""

from __future__ import annotations

import pytest

from mavlink_mcp.adapter import AdapterError
from mavlink_mcp.capabilities import Capability

mavsdk_adapter = pytest.importorskip(
    "mavlink_mcp.adapters.mavsdk_adapter",
    reason="mavsdk not installed",
)


def test_declares_all_mvp1_capabilities():
    adapter = mavsdk_adapter.MavsdkAdapter()
    assert adapter.capabilities() == frozenset(Capability)


def test_default_system_address_matches_installed_mavsdk():
    address = mavsdk_adapter.default_system_address()
    assert address in ("udpin://0.0.0.0:14540", "udp://:14540")


def test_env_var_overrides_address(monkeypatch):
    monkeypatch.setenv("MAVLINK_MCP_URL", "udpin://0.0.0.0:14550")
    adapter = mavsdk_adapter.MavsdkAdapter()
    assert adapter.system_address == "udpin://0.0.0.0:14550"


def test_constructor_argument_beats_env(monkeypatch):
    monkeypatch.setenv("MAVLINK_MCP_URL", "udpin://0.0.0.0:14550")
    adapter = mavsdk_adapter.MavsdkAdapter(system_address="tcpin://:5760")
    assert adapter.system_address == "tcpin://:5760"


def test_not_connected_initially():
    adapter = mavsdk_adapter.MavsdkAdapter()
    assert adapter.is_connected() is False


@pytest.mark.asyncio
async def test_operations_require_connection():
    adapter = mavsdk_adapter.MavsdkAdapter()
    with pytest.raises(AdapterError, match="Not connected"):
        await adapter.get_telemetry()
    with pytest.raises(AdapterError, match="Not connected"):
        await adapter.arm()


def test_mission_item_builder_works_with_installed_mavsdk():
    from mavlink_mcp.capabilities import Waypoint

    item = mavsdk_adapter._mission_item(Waypoint(47.4, 8.5, 20.0))
    assert item.latitude_deg == 47.4
    assert item.relative_altitude_m == 20.0
