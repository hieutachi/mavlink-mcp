# No drone? Fly mavlink-mcp against a simulator

Everything in this project works without hardware. Two ways to get a MAVLink vehicle on your machine, from lightest to most complete — pick one. Both end with a vehicle sending MAVLink to UDP port **14540**, which is `mavlink-mcp`'s default endpoint.

## Option A — PX4 SITL in Docker (recommended, ~2 minutes)

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
3. *"Take off to 15 meters."* — the agent must ask you to confirm, then calls `arm(confirm=true)` and `takeoff(altitude_m=15, confirm=true)`.
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
| `takeoff` rejected with "requires explicit confirmation" | Working as intended — call again with `confirm=true` after the operator approves. |
| `goto_location` rejected with a geofence error | The target is farther from home than `MAVLINK_MCP_GEOFENCE_RADIUS_M`. Pick a closer point or raise the limit consciously. |
| Old MAVSDK (2.x) installed | The default endpoint automatically falls back to the older `udp://:14540` syntax; if you set `MAVLINK_MCP_URL` yourself, use the syntax matching your MAVSDK major version. |
