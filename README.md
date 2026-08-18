# mavlink-mcp

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)

**mavlink-mcp** is a **vendor-neutral UAV capability layer + [Model Context Protocol](https://modelcontextprotocol.io) server built on open standards**. It connects AI agents (Claude Desktop, Cursor, VS Code, and any other MCP client) to drones that speak **MAVLink** — PX4 SITL, ArduPilot SITL, and Pixhawk-class autopilots — through [MAVSDK](https://mavsdk.mavlink.io/) (BSD-3-Clause). Instead of wrapping protocol messages, it exposes **8 flight capabilities** (telemetry, flight mode, arm/disarm, takeoff, land, goto, mission, return-to-launch) behind one clean API with safety guardrails on by default.

It is the sister project of [rosbridge-mcp](https://github.com/hieutachi/rosbridge-mcp) (AI agents ↔ ROS 2 robots) and shares its philosophy: open protocols only, readonly-by-default guardrails, simulation-first, zero telemetry, MIT licensed.

> **Note on the name:** "mavlink-mcp" is a provisional working name. "MAVLink" is a trademark of the Dronecode Foundation; the project name may be adjusted pending a review of their trademark policy before any public release.

> **Safety disclaimer:** this project is built for **simulation and research**. Flying real aircraft with it is entirely at your own risk and responsibility, including compliance with your local aviation law (registration, flight permits, pilot licensing). See [SECURITY.md](SECURITY.md).

## Why a capability layer, not another SDK?

- **For AI agents, capabilities beat 400 SDK functions.** An agent asks `get_capabilities` ("what can this drone do?"), gets back a small vocabulary of physical actions, and plans with it — no MAVLink knowledge needed on the model side.
- **Vendor-neutral by construction.** Capabilities are defined in physical quantities (degrees, meters, volts) from open specs — not copied from any proprietary SDK surface. The MAVSDK adapter is one implementation; a future ROS 2 adapter (reusing rosbridge-mcp) implements the same interface.
- **Guardrails are part of the API, not an afterthought.** Readonly mode is the default, arming and takeoff require explicit operator-approved confirmation, and every commanded location is checked against an altitude ceiling and a soft geofence — before anything reaches the autopilot.

## Architecture

```text
+--------------------+  stdio (MCP)  +----------------------------------+  MAVLink (UDP)  +------------------+
|  AI client         | <-----------> | mavlink-mcp                      | <-------------> | PX4 / ArduPilot  |
|  (Claude, Cursor,  |               |  MCP server                      |     via         |  SITL or real FC |
|   VS Code, ...)    |               |   └─ capability layer + policy   |    MAVSDK       |  (Pixhawk-class) |
+--------------------+               |       └─ MAVSDK adapter          |    (BSD-3)      +------------------+
                                     +----------------------------------+
Python scripts use the same capability layer directly (mavlink_mcp.Drone).
```

The **capability layer** (`Drone`) owns all policy — readonly, confirmation, altitude, geofence. The **adapter** (`MavsdkAdapter`) only translates approved operations to MAVLink; it is the only module that imports `mavsdk`. New backends implement the same `DroneAdapter` interface and inherit both surfaces (MCP tools + Python library) and every guardrail for free.

## Quick Start (60 seconds)

```bash
pip install git+https://github.com/hieutachi/mavlink-mcp.git
```

Start a PX4 SITL (see [docs/simulator-quickstart.md](docs/simulator-quickstart.md) — one Docker command), then add to your MCP client config:

```json
{
  "mcpServers": {
    "mavlink": {
      "command": "mavlink-mcp",
      "env": { "MAVLINK_MCP_READONLY": "true" }
    }
  }
}
```

Then ask your agent: *"What can this drone do, and where is it right now?"*

When you're ready to fly (in the simulator!), set `MAVLINK_MCP_READONLY` to `"false"`.

## Capabilities & tools

12 tools covering 8 capabilities. All tools return JSON; errors come back as `{"error": "...", "guardrail": true}` instead of raising, so the agent can read and react.

| Tool | Capability | What it does | Mutating? |
| --- | --- | --- | --- |
| `get_capabilities` | — | What the vehicle supports + active guardrails | no |
| `get_telemetry` | Telemetry | Position, attitude, battery, GPS, mode, armed, in-air | no |
| `get_flight_mode` | FlightMode | Current flight mode (HOLD, MISSION, RTL, ...) | no |
| `get_connection_status` | — | Endpoint, connection state, guardrail config | no |
| `arm` | Arm/Disarm | Spin up motors | **yes** — requires `confirm=true` |
| `disarm` | Arm/Disarm | Stop motors (on the ground) | **yes** |
| `takeoff` | Takeoff | Climb to a target altitude | **yes** — requires `confirm=true` |
| `land` | Land | Land at the current position | **yes** |
| `hold` | FlightMode | Pause: stop and loiter in place | **yes** |
| `return_to_launch` | ReturnToLaunch | Fly home and land | **yes** |
| `goto_location` | Goto | Fly to one waypoint and loiter | **yes** — geofence + altitude checked |
| `fly_mission` | Mission | Upload a waypoint mission and start it | **yes** — every waypoint checked |

## Example conversation

> **You:** Check the drone and, if it's healthy, do a short patrol at 20 m.
>
> **Agent:** *(calls `get_capabilities` — sees readonly is off, geofence 200 m; calls `get_telemetry` — GPS `FIX_3D`, 10 satellites, battery 100%)*
> The drone is healthy. I need your approval to arm and take off — confirm?
>
> **You:** Confirmed, go ahead.
>
> **Agent:** *(calls `arm(confirm=true)`, `takeoff(altitude_m=20, confirm=true)`, then `fly_mission` with 3 waypoints inside the geofence, monitors `get_telemetry`, finishes with `return_to_launch`)*
> Patrol complete — the drone is back at the launch point and disarmed.

## Configuration

| Environment variable | Default | Description |
| --- | --- | --- |
| `MAVLINK_MCP_URL` | `udpin://0.0.0.0:14540` | MAVLink endpoint (PX4 SITL's offboard port). With MAVSDK 2.x the older syntax `udp://:14540` is used automatically. |
| `MAVLINK_MCP_READONLY` | **`true`** | Reject every tool that can move the vehicle (see Safety) |
| `MAVLINK_MCP_MAX_ALTITUDE_M` | `50` | Ceiling for takeoff/goto/mission altitudes, meters above launch. `0` disables. |
| `MAVLINK_MCP_GEOFENCE_RADIUS_M` | `200` | Soft geofence radius around the home position, meters. `0` disables. |

## Safety

Letting a language model command an aircraft is a real risk, so the guardrails are stricter than a typical SDK:

1. **Readonly by default.** Unlike most tools, you must explicitly opt in to flight with `MAVLINK_MCP_READONLY=false`. In readonly mode all telemetry tools work; every mutating tool is rejected with a clear explanation.
2. **Two-step confirmation for the dangerous transitions.** `arm` and `takeoff` require `confirm=true`, and the tool descriptions instruct the agent to obtain human approval first — an agent cannot legitimately take off in a single autonomous step.
3. **Soft geofence + altitude ceiling.** Every commanded location (goto and each mission waypoint) is validated against `MAVLINK_MCP_GEOFENCE_RADIUS_M` around home and `MAVLINK_MCP_MAX_ALTITUDE_M` before anything is sent to the autopilot.
4. **Safety actions stay friction-free.** `land`, `hold`, and `return_to_launch` never require confirmation — de-escalation must always be cheap.

These checks are policy inside this process — **not** a substitute for the autopilot's own failsafes, a real geofence configured in PX4/ArduPilot, network isolation, or a human with an RC transmitter. Read [SECURITY.md](SECURITY.md) before considering real hardware, and treat real-world flights as requiring registration/permits under your local aviation law (e.g. Vietnam's UAV Decree 288/2025 requires registration and flight permits).

## Python library

The same capability layer is importable for scripts and notebooks — see [examples/patrol_sitl.py](examples/patrol_sitl.py) for a full takeoff → waypoint → land run against SITL:

```python
from mavlink_mcp import Drone, GuardrailConfig
from mavlink_mcp.adapters.mavsdk_adapter import MavsdkAdapter

drone = Drone(MavsdkAdapter(), guardrails=GuardrailConfig(readonly=False))
snapshot = await drone.get_telemetry()
await drone.arm(confirm=True)
await drone.takeoff(20.0, confirm=True)
```

## Privacy & legal

**No telemetry, no data collection.** The only network connection this package opens is the MAVLink endpoint you configure (`MAVLINK_MCP_URL`). Vehicle data returned by tools goes exclusively to your MCP client.

**License compliance.** The core deliberately depends on **MAVSDK-Python (BSD-3-Clause)** and *not* on pymavlink (LGPL-3), keeping the dependency tree permissive under this project's MIT license. Direct dependencies: `mavsdk` (BSD-3-Clause), `fastmcp` (Apache-2.0). All code in this repository is original work written from public, open specifications (MAVLink protocol docs, MAVSDK docs) — no proprietary SDKs, no reverse engineering, no vendor EULAs accepted.

## FAQ

**Do I need a drone?**
No. MVP1 is simulation-first: everything works against PX4 SITL (one Docker command) and is designed to also work against ArduPilot SITL. See [docs/simulator-quickstart.md](docs/simulator-quickstart.md).

**Does it work with ArduPilot?**
The capability layer targets both PX4 and ArduPilot through MAVSDK. PX4 SITL is the primary tested target in MVP1; ArduPilot SITL compatibility notes are in the quickstart, and validating it in CI is a roadmap item.

**Why not just use MAVSDK directly?**
If you're writing Python by hand, do! mavlink-mcp adds the layer MAVSDK doesn't have: an MCP tool surface for AI agents, a capability model with runtime discovery, and production guardrails (readonly, confirmation, geofence) enforced above the protocol.

**The agent says no vehicle was discovered.**
Check the SITL is running and sending MAVLink to the endpoint in `MAVLINK_MCP_URL` (PX4 SITL sends to UDP 14540 by default). The quickstart has a troubleshooting table.

**Is my data sent anywhere?**
Only to your MCP client, which forwards it to whatever LLM you use — treat position data accordingly.

## Roadmap

Staged plan in [ROADMAP.md](ROADMAP.md): MVP1 (this — capability layer + MCP server on SITL), MVP2 (real Pixhawk-class hardware, ROS 2 adapter reusing rosbridge-mcp, plugin/conformance system), MVP3 (community adapters, multi-vehicle, open-core services).

## Contributing

Contributions are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md). Please sign off your commits (DCO). Note the clean-contribution rule: PRs must be based on public specs and documentation only.

## License

MIT — see [LICENSE](LICENSE). Dependency licenses are permissive and compatible: `mavsdk` (BSD-3-Clause), `fastmcp` (Apache-2.0). No GPL/LGPL/AGPL dependencies in the core.

---

## Tóm tắt tiếng Việt

**mavlink-mcp** là lớp capability trung lập (vendor-neutral) cho UAV kèm MCP server, xây hoàn toàn trên chuẩn mở: kết nối AI agent (Claude Desktop, Cursor, VS Code...) với drone nói MAVLink (PX4/ArduPilot) qua thư viện MAVSDK (BSD-3). Đây là dự án chị em của rosbridge-mcp.

- **8 capability:** telemetry (vị trí/tư thế/pin/GPS), flight mode, arm/disarm, takeoff, land, goto, mission, return-to-launch — 12 tool MCP.
- **An toàn mặc định:** chế độ readonly bật sẵn (`MAVLINK_MCP_READONLY` mặc định `true`); arm và takeoff cần `confirm=true` sau khi người vận hành đồng ý; geofence mềm + trần độ cao cấu hình được.
- **Simulation-first:** chạy với PX4 SITL (1 lệnh Docker) — xem [docs/simulator-quickstart.md](docs/simulator-quickstart.md). Dự án dành cho mô phỏng/nghiên cứu; bay thật hoàn toàn do bạn tự chịu trách nhiệm, bao gồm đăng ký thiết bị và xin phép bay theo Luật Phòng không nhân dân 49/2024 và Nghị định 288/2025.
- Tên "mavlink-mcp" là tên tạm — sẽ rà soát trademark policy của Dronecode trước khi công bố.
