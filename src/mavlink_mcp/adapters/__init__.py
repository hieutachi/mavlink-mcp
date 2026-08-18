"""Concrete UAV backends implementing :class:`mavlink_mcp.adapter.DroneAdapter`.

MVP1 ships one adapter: MAVSDK (MAVLink). The package layout keeps the
boundary explicit so future adapters (ROS 2, additional protocols) live
side by side without touching the capability layer.
"""
