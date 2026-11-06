# No drone? Fly mavlink-mcp against a simulator

Choose the network-free kinematic demo below for MCP integration, or a separately installed SITL for autopilot testing. These are different fidelity levels: a passing kinematic demo is not evidence that PX4, aerodynamics, or a real vehicle will work.

## Option 0 — Built-in kinematic simulator (Windows, no Docker or ROS)

Use the existing package environment and this MCP configuration. Replace `python` with that environment's absolute Python executable when configuring a desktop client:

```json
{
  "mcpServers": {
    "mavlink-sim": {
      "command": "python",
      "args": ["-I", "-B", "-u", "-m", "mavlink_mcp"],
      "env": {
        "MAVLINK_MCP_BACKEND": "sim",
        "MAVLINK_MCP_READONLY": "false",
        "MAVLINK_MCP_MAX_ALTITUDE_M": "50",
        "MAVLINK_MCP_GEOFENCE_RADIUS_M": "200"
      }
    }
  }
}
```

`sim` does not import MAVSDK, open UDP ports, or use `MAVLINK_MCP_URL`. No additional runtime dependencies are introduced. The default backend remains `mavsdk` for compatibility; an unknown backend fails instead of falling back. Selection is fixed until process restart.

The same 12 tools control a synthetic UAV: arm, takeoff, goto, ordered waypoint missions, hold, land, and RTL. Speeds default to 5 m/s horizontally and 2 m/s vertically. State advances from monotonic elapsed time whenever observed or commanded, not by teleporting on command acknowledgement. Mission speeds may override horizontal speed. Missions contain at most 1000 waypoints. Hold cancels the remaining plan; there is no resume operation. RTL returns at the current altitude, then lands; this assumes a flat, obstacle-free world. Landing auto-disarms. Disconnect cancels motion and freezes state; restarting the process creates a new disarmed vehicle.

Check `get_capabilities` for `adapter: "sim"`, `simulation.synthetic: true` and `network_enabled: false` before authorizing motion. After takeoff, poll telemetry for HOLD and positive altitude. `get_telemetry` and `get_connection_status` include local ENU position, synthetic home and mission progress. `goto_started`/`mission_started` mean accepted, **not completed**.

**Safety:** without a trusted `SAFEMAV_CONFIG` launch file the server is read-only and blocks **all** mutations, including land/hold/RTL/disarm. `MAVLINK_MCP_READONLY=false` alone grants nothing. With a gateway configured, every mutating tool — recovery actions included — needs an Ed25519 approval signed by the operator key and bound to that exact request. `confirm=true` is deprecated and authorizes nothing. Disarm while airborne is refused. Do not try to build an emergency controller by removing the gateway.

**Data limitations:** battery and GPS are null (not modelled), coordinates and attitude are synthetic. No physics, collision, terrain/DEM, wind, camera, disease detection, Wh, GSD or recall model is provided. Do not use these outputs as thesis validation or flight-safety evidence.

A bounded, self-contained stdio test exercises all 12 tools and verifies mission completion and landing:

```bash
python examples/patrol_sim.py
python -m pytest -q
```

The example always launches `BACKEND=sim`, even when the parent environment has a MAVLink URL configured.

## Option A — PX4 SITL in Docker (separate installation)

Requires [Docker](https://docs.docker.com/get-docker/). The community image runs PX4 SITL with a headless Gazebo world and streams MAVLink out of the container:

```bash
docker run --rm -it jonasvautherin/px4-gazebo-headless:1.14.3
```

Wait for the PX4 boot log to settle (you'll see `INFO [commander] Ready for takeoff!`). The simulator sends MAVLink to UDP 14540 on the host — exactly where `mavlink-mcp` listens by default.

> **Windows note:** run this from WSL2 (with the MCP client also on Windows, set `MAVLINK_MCP_URL` accordingly, or run the whole stack inside WSL2). UDP forwarding from Linux containers is most reliable on a Linux/WSL2 host. Alternatively pass your host IP: `docker run --rm -it jonasvautherin/px4-gazebo-headless:1.14.3 <HOST_IP>`.

If you build PX4 from source instead, `make px4_sitl gz_x500` gives the same result (see the [PX4 docs](https://docs.px4.io/main/en/sim_gazebo_gz/)).

## Option B — ArduPilot SITL

Requires an ArduPilot development environment ([official guide](https://ardupilot.org/dev/docs/sitl-simulator-software-in-the-loop.html)). Start a copter and point one output at mavlink-mcp's port:

```bash
sim_vehicle.py -v ArduCopter --out udp:127.0.0.1:14540
```

ArduPilot speaks the same MAVLink protocol through MAVSDK, with a different flight-mode dialect (GUIDED instead of PX4's OFFBOARD/HOLD naming). The capability tools work the same way; PX4 SITL is the primary tested target in MVP1 and ArduPilot validation is a tracked roadmap item — please report anything that misbehaves.

## Connect your MCP client

Add to your MCP client config (Claude Desktop `claude_desktop_config.json`, Cursor `mcp.json`, etc.):

```json
{
  "mcpServers": {
    "mavlink": {
      "command": "mavlink-mcp",
      "env": {
        "MAVLINK_MCP_URL": "udpin://0.0.0.0:14540",
        "MAVLINK_MCP_READONLY": "false",
        "MAVLINK_MCP_MAX_ALTITUDE_M": "50",
        "MAVLINK_MCP_GEOFENCE_RADIUS_M": "200"
      }
    }
  }
}
```

`MAVLINK_MCP_READONLY=false` is safe here because the "vehicle" is a simulator. For anything real, start with `"true"`.

## A first flight, guided by prompts

1. *"What can this drone do?"* — the agent calls `get_capabilities` and sees the 8 capabilities plus the active guardrails.
2. *"Where is it, and is it healthy?"* — `get_telemetry`: position, battery, GPS fix, flight mode.
3. *"Take off to 15 meters."* — the agent calls `arm`, gets an approval request back, and waits. You sign it with `safemav-operator approve`; the agent resubmits with `request_id` and `approval_token`, then repeats for `takeoff(altitude_m=15)`.
4. *"Fly 100 m north, then come home."* — `goto_location` (geofence-checked), then `return_to_launch`.
5. Try to break the guardrails: ask for takeoff to 500 m or a waypoint 5 km away — the tools refuse and explain which limit was hit.

Or run the scripted version: [examples/patrol_sitl.py](../examples/patrol_sitl.py).

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `No vehicle discovered on udpin://0.0.0.0:14540` | The SITL isn't running, hasn't finished booting, or isn't sending to port 14540. For Docker, wait for `Ready for takeoff!`; for ArduPilot, check the `--out udp:...:14540` argument. |
| Same error on Windows with Docker | UDP from the container may not reach the Windows host. Run the stack in WSL2, or pass your host IP to the container (see Option A note). |
| Port 14540 already in use | Another GCS/SDK is bound to it. Close it, or use a different output port and set `MAVLINK_MCP_URL` to match. |
| `arm` fails with a preflight error | The simulated vehicle isn't ready (no GPS lock yet, EKF still initializing). Wait a few seconds after boot and check `get_telemetry` shows a 3D fix. |
| `takeoff` returns `REQUIRE_APPROVAL` | Working as intended — sign the returned `approval_request` and resubmit with `request_id` plus `approval_token`. |
| `takeoff` returns `READONLY` | No `SAFEMAV_CONFIG` is set, so no gateway exists and writes are refused. |
| `goto_location` rejected with a geofence error | The target is farther from home than `MAVLINK_MCP_GEOFENCE_RADIUS_M`. Pick a closer point or raise the limit consciously. |
| Old MAVSDK (2.x) installed | The default endpoint automatically falls back to the older `udp://:14540` syntax; if you set `MAVLINK_MCP_URL` yourself, use the syntax matching your MAVSDK major version. |
