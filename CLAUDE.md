# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this is

**mavlink-mcp** is a vendor-neutral UAV capability layer plus an MCP server. AI agents talk to MAVLink drones (PX4/ArduPilot SITL or Pixhawk-class hardware) through MAVSDK (BSD-3). It is the sister of [rosbridge-mcp](https://github.com/hieutachi/rosbridge-mcp).

Do not rename the Python package (`mavlink_mcp`) or the console script (`mavlink-mcp`).

## Architecture

```
AI client (MCP stdio)
  -> mavlink_mcp.server (FastMCP tools)
    -> mavlink_mcp.Drone          # owns all policy
      -> DroneAdapter             # protocol-agnostic interface
        -> MavsdkAdapter          # only module that imports mavsdk
          -> MAVLink UDP (default udpin://0.0.0.0:14540)
```

- `src/mavlink_mcp/capabilities.py` — physical-quantity types (degrees, meters, volts); no protocol messages.
- `src/mavlink_mcp/drone.py` — capability checks + guardrails.
- `src/mavlink_mcp/guardrails.py` — readonly (default **on**), altitude ceiling, soft geofence.
- `src/mavlink_mcp/adapters/mavsdk_adapter.py` — MAVSDK translation only.
- `src/mavlink_mcp/server.py` — 12 MCP tools over 8 capabilities.

Python scripts use `Drone` directly; MCP clients use the `mavlink-mcp` console script. Same policy either way.

## Commands

```bash
pip install -e ".[dev]"
pytest                          # mock adapter; no SITL required
mavlink-mcp                     # stdio MCP server
```

SITL (optional, see `docs/simulator-quickstart.md`):

```bash
docker run --rm -it jonasvautherin/px4-gazebo-headless:1.14.3
```

## Environment

| Variable | Default | Meaning |
| --- | --- | --- |
| `MAVLINK_MCP_URL` | `udpin://0.0.0.0:14540` | MAVLink endpoint |
| `MAVLINK_MCP_READONLY` | `true` | Rejects every tool that can move the vehicle |
| `MAVLINK_MCP_MAX_ALTITUDE_M` | `50` | Relative altitude ceiling; `0` disables |
| `MAVLINK_MCP_GEOFENCE_RADIUS_M` | `200` | Soft geofence around home; `0` disables |

Arm and takeoff also require `confirm=true` even when readonly is off.

## Conventions

- Runtime deps must stay permissively licensed (MIT/BSD/Apache). Do not add pymavlink (LGPL-3); the MAVLink backend is MAVSDK-Python.
- Mutating MCP tools return `{"error": "...", "guardrail": true}` instead of raising, so agents can recover.
- Tests live in `tests/` and use a mock adapter (`tests/conftest.py`). Keep that path hardware-free.
- Cite public specs (MAVLink, MAVSDK, PX4/ArduPilot docs) in PRs. No NDA, firmware dumps, or proprietary SDK material — see `CONTRIBUTING.md`.
- Sign off commits (`git commit -s`) for DCO.
- Simulation and research first. Do not treat this as a certified autopilot safety system.
