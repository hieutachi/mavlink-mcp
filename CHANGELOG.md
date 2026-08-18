# Changelog

## v0.1.0 (unreleased) — MVP1

Initial release: vendor-neutral UAV capability layer + MCP server.

- Capability model with 8 capabilities: telemetry, flight_mode, arm_disarm, takeoff, land, goto, mission, return_to_launch.
- MAVSDK-Python (BSD-3) adapter behind an explicit `DroneAdapter` interface — the only module that touches the protocol.
- MCP server (FastMCP, stdio) with 12 tools; same capabilities available as a typed async Python library (`mavlink_mcp.Drone`).
- Safety guardrails: readonly mode **on by default** (`MAVLINK_MCP_READONLY`), explicit `confirm=true` required for `arm` and `takeoff`, configurable altitude ceiling (`MAVLINK_MCP_MAX_ALTITUDE_M`) and soft geofence around home (`MAVLINK_MCP_GEOFENCE_RADIUS_M`); `land`/`hold`/`return_to_launch` stay confirmation-free.
- Test suite against a mock adapter (no SITL needed) + MAVSDK adapter smoke tests.
- Docs: README, PX4/ArduPilot simulator quickstart, SITL patrol example, roadmap, security policy.
