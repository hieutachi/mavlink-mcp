# AGENTS.md

Conventions for anyone — human or automated assistant — working in this repository.

## What this is

**mavlink-mcp** is a vendor-neutral UAV capability layer plus an MCP server. AI agents talk to MAVLink drones (PX4/ArduPilot SITL or Pixhawk-class hardware) through MAVSDK (BSD-3). It is the sister of [rosbridge-mcp](https://github.com/hieutachi/rosbridge-mcp).

Do not rename the Python package (`mavlink_mcp`) or the console script (`mavlink-mcp`).

## Architecture

```
AI client (MCP stdio)
  -> mavlink_mcp.server (FastMCP tools)
    -> mavlink_mcp.Drone            # thin facade, no authority of its own
      -> safety.gateway             # policy, approvals, leases, audit
        -> DroneAdapter             # protocol-agnostic interface
          -> MavsdkAdapter          # only module that imports mavsdk
            -> MAVLink UDP (default udpin://0.0.0.0:14540)
```

All authorization lives in `safety/gateway.py`. `Drone` forwards to it and a
`Drone` built without a gateway cannot write at all.

- `src/mavlink_mcp/capabilities.py` — physical-quantity types (degrees, meters, volts); no protocol messages.
- `src/mavlink_mcp/drone.py` — capability checks; forwards writes to the gateway.
- `src/mavlink_mcp/guardrails.py` — legacy readonly flag, altitude ceiling, soft geofence.
- `src/mavlink_mcp/safety/` — gateway, approvals, store, policy, admission, state machine.
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
| `SAFEMAV_CONFIG` | unset | Trusted launch config. Unset means read-only with no gateway. |
| `MAVLINK_MCP_BACKEND` | `mavsdk` | `sim` or `mavsdk`. Unknown values fail closed. |
| `MAVLINK_MCP_READONLY` | `true` | Legacy flag. Can refuse; cannot authorize. |

`confirm=true` is deprecated and authorizes nothing. A write needs an Ed25519
approval from an operator key the server holds only the public half of. See
`docs/ARCHITECTURE.md` and `README.md`.

## Conventions

- Runtime deps must stay permissively licensed (MIT/BSD/Apache). Do not add pymavlink (LGPL-3); the MAVLink backend is MAVSDK-Python.
- Mutating MCP tools return a structured result instead of raising. Only `DENY` sets `error`; `REQUIRE_APPROVAL` is a normal step, not a fault.
- Tests live in `tests/` and use a mock adapter (`tests/conftest.py`). Keep that path hardware-free.
- Cite public specs (MAVLink, MAVSDK, PX4/ArduPilot docs) in PRs. No NDA, firmware dumps, or proprietary SDK material — see `CONTRIBUTING.md`.
- Sign off commits (`git commit -s`) for DCO.
- Simulation and research first. Do not treat this as a certified autopilot safety system.
