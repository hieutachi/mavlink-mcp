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
- **Authorization is part of the API, not an afterthought.** Readonly is the default. Every command that can move the vehicle needs an Ed25519 approval signed by a separate operator key, bound to that exact request, and re-checked immediately before each physical write. Altitude, geofence, battery and vehicle-state limits are enforced in the same path.

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

The **safety gateway** owns all authorization — signed approvals, replay protection, telemetry freshness, altitude and geofence limits, and the audit ledger. The **capability layer** (`Drone`) is a thin facade over it. The **adapter** (`MavsdkAdapter`) only translates approved operations to MAVLink and refuses any write without a valid execution lease; it is the only module that imports `mavsdk`. New backends implement the same `DroneAdapter` interface and inherit both surfaces (MCP tools + Python library) and every guardrail for free.

## Quick Start

For a local, network-free demo, set `MAVLINK_MCP_BACKEND=sim` and follow [Option 0 in the simulator guide](docs/simulator-quickstart.md#option-0--built-in-kinematic-simulator-windows-no-docker-or-ros). All 12 tools work against a moving synthetic vehicle without Docker, ROS or PX4. Telemetry is labelled synthetic; battery/GPS are null. This is a kinematic integration test, **not** a physics or energy simulator. Run `python examples/patrol_sim.py` for a bounded stdio flight test.

For the MAVSDK backend (the default), install and configure SITL separately:

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
| `arm` | Arm/Disarm | Spin up motors | **signed approval** |
| `disarm` | Arm/Disarm | Stop motors (on the ground) | **signed approval** |
| `takeoff` | Takeoff | Climb to a target altitude | **signed approval** + altitude limit |
| `land` | Land | Land at the current position | **signed approval** |
| `hold` | FlightMode | Pause: stop and loiter in place | **signed approval** |
| `return_to_launch` | ReturnToLaunch | Fly home and land | **signed approval** |
| `goto_location` | Goto | Fly to one waypoint and loiter | **signed approval** + geofence/altitude |
| `fly_mission` | Mission | Upload a waypoint mission and start it | **signed approval**, every waypoint checked |

## Example conversation

> **You:** Check the drone and, if it's healthy, do a short patrol at 20 m.
>
> **Agent:** *(calls `get_capabilities` — sees readonly is off, geofence 200 m; calls `get_telemetry` — GPS `FIX_3D`, 10 satellites, battery 100%)*
> The drone is healthy. I need your approval to arm and take off — confirm?
>
> **You:** Confirmed, go ahead.
>
> **Agent:** *(calls `arm` — gets back an approval request, not an action)*
> Request `a1b2c3` is waiting for your signature.
>
> **You:** *(runs `safemav-operator approve` with your key)*
>
> **Agent:** *(resubmits `arm` with the signed token, then `takeoff(altitude_m=20)` the same way, then `fly_mission` with 3 waypoints inside the geofence, monitors `get_telemetry`, finishes with `return_to_launch`)*
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

1. **Readonly by default, and no gateway means no writes.** Without a trusted
   `SAFEMAV_CONFIG` launch file the server exposes telemetry only. Setting
   `MAVLINK_MCP_READONLY=false` does not grant write access on its own.
2. **Signed human approval, not a boolean.** An agent's first call returns an
   approval request. A human signs that exact request with an Ed25519 key the
   server only knows the public half of (`safemav-operator approve`), and the
   agent resubmits it unchanged. `confirm=true` is deprecated and authorizes
   nothing. The approval is bound to the actor, vehicle, gateway, boot epoch,
   policy hash, connection epoch, home position and exact parameters, and is
   single-use.
3. **Re-checked at every physical write.** Commands like takeoff touch two
   MAVSDK sinks. Before each one the gateway re-verifies the approval deadline
   (UTC and monotonic), telemetry freshness, home and connection epochs, the
   policy hash, the operator key, and that the audit ledger is still writable.
   If anything changed between the first sink and the second, the second does
   not run and the request is recorded `UNKNOWN`.
4. **Deterministic limits.** Altitude ceiling, geofence radius, speed, battery
   reserve, GPS fix quality and vehicle state are checked on canonical integer
   units, with float32 rounding accounted for, before anything is sent.
5. **Durable audit.** Every request, approval, sink check and outcome is in a
   hash-chained SQLite ledger. An uncertain outcome latches the writer closed,
   across process restarts.
6. **`ACKNOWLEDGED` is not "done".** It means the autopilot accepted the
   command. Observe telemetry before concluding a flight finished.

Evidence for these claims, measured against a live headless PX4 SITL, is in
[docs/SITL_EVIDENCE.md](docs/SITL_EVIDENCE.md).

**Scope limit, stated plainly:** enforcement covers the MCP tools and the
supported Python API in this process. It is not an OS sandbox. Code already
running inside the same interpreter can read the gateway's private issuer
object and forge a lease; that attempt is enumerated and tested in
[docs/THREAT_MODEL.md](docs/THREAT_MODEL.md) section 8b. These checks are also
**not** a substitute for the autopilot's own failsafes, a real geofence
configured in PX4/ArduPilot, network isolation, or a human with an RC transmitter. Read [SECURITY.md](SECURITY.md) before considering real hardware, and treat real-world flights as requiring registration/permits under your local aviation law (e.g. Vietnam's UAV Decree 288/2025 requires registration and flight permits).

## Python library

The same capability layer is importable for scripts and notebooks. Reads need
nothing special; writes go through the same gateway as the MCP tools, so a
bare `Drone` is read-only by design:

```python
from mavlink_mcp import Drone
from mavlink_mcp.adapters.mavsdk_adapter import MavsdkAdapter

drone = Drone(MavsdkAdapter())            # no gateway -> telemetry only
snapshot = await drone.get_telemetry()

result = await drone.arm()                # REQUIRE_APPROVAL, nothing was sent
print(result.request_id)                  # hand this to the operator to sign
```

To actually fly, compose the trusted runtime (ledger, policy, operator trust
store) and resubmit the signed token:

```python
from mavlink_mcp.safety.runtime import create_drone   # reads SAFEMAV_CONFIG

drone = create_drone()
pending = await drone.takeoff(20.0)
# ... operator signs pending.approval_request out of band ...
result = await drone.takeoff(20.0, request_id=pending.request_id,
                             approval_token=token)
assert result.execution_status == "ACKNOWLEDGED"   # accepted, not completed
```

A complete runnable version, including an ephemeral operator key and the
refusal cases, is [examples/patrol_sim.py](examples/patrol_sim.py).

## Documentation

| Document | What it covers |
| --- | --- |
| [INSTALLATION.md](docs/INSTALLATION.md) | Tiered setup: tests, kinematic demo, PX4 SITL |
| [DEMO_GUIDE.md](docs/DEMO_GUIDE.md) | Three scenarios with real transcripts |
| [SECURITY_ANALYSIS.md](docs/SECURITY_ANALYSIS.md) | Each claim mapped to a test, and what is not claimed |
| [THREAT_MODEL.md](docs/THREAT_MODEL.md) | Adversaries, trust boundaries, attack tree |
| [ADVERSARIAL_TESTS.md](docs/ADVERSARIAL_TESTS.md) | T01-T25 mapped to running tests |
| [BENCHMARK_RESULTS.md](docs/BENCHMARK_RESULTS.md) | 2,340 trials against the pre-gateway guardrails |
| [SITL_EVIDENCE.md](docs/SITL_EVIDENCE.md) | Recorded run against headless PX4 |
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | Component design |

For the competition submission, see [submission/](submission/).

## Privacy & legal

**No telemetry, no data collection.** The only network connection this package opens is the MAVLink endpoint you configure (`MAVLINK_MCP_URL`). Vehicle data returned by tools goes exclusively to your MCP client.

**License compliance.** The core deliberately depends on **MAVSDK-Python (BSD-3-Clause)** and *not* on pymavlink (LGPL-3), keeping the dependency tree permissive under this project's MIT license. Direct dependencies: `mavsdk` (BSD-3-Clause), `fastmcp` (Apache-2.0). All code in this repository is original work written from public, open specifications (MAVLink protocol docs, MAVSDK docs) — no proprietary SDKs, no reverse engineering, no vendor EULAs accepted.

## FAQ

**Do I need a drone?**
No. MVP1 is simulation-first and verified against PX4 SITL running headless (no Gazebo or GPU needed) — see [docs/SITL_EVIDENCE.md](docs/SITL_EVIDENCE.md) and [docs/simulator-quickstart.md](docs/simulator-quickstart.md). No physical aircraft has been flown with this code.

**Does it work with ArduPilot?**
The capability layer targets both PX4 and ArduPilot through MAVSDK. PX4 SITL is the tested target: see [docs/SITL_EVIDENCE.md](docs/SITL_EVIDENCE.md) for a recorded run against headless PX4 (arm, takeoff, goto, RTL, plus ten refusal cases). ArduPilot has **not** been tested at all; compatibility notes in the quickstart are design intent, and validating it is a roadmap item.

**Why not just use MAVSDK directly?**
If you're writing Python by hand, do! mavlink-mcp adds the layer MAVSDK doesn't have: an MCP tool surface for AI agents, a capability model with runtime discovery, and a policy-enforced safety gateway (signed operator approval, per-sink re-validation, hash-chained audit) above the protocol.

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
- **An toàn mặc định:** chỉ đọc nếu chưa cấu hình `SAFEMAV_CONFIG`. Mọi lệnh di chuyển phương tiện cần approval Ed25519 do người vận hành ký, ràng buộc vào đúng request đó, dùng một lần, và được kiểm tra lại ngay trước từng lệnh gửi xuống xe. `confirm=true` không còn cấp quyền. Geofence mềm + trần độ cao cấu hình được.
- **Simulation-first:** chạy với PX4 SITL (1 lệnh Docker) — xem [docs/simulator-quickstart.md](docs/simulator-quickstart.md). Dự án dành cho mô phỏng/nghiên cứu; bay thật hoàn toàn do bạn tự chịu trách nhiệm, bao gồm đăng ký thiết bị và xin phép bay theo Luật Phòng không nhân dân 49/2024 và Nghị định 288/2025.
- Tên "mavlink-mcp" là tên tạm — sẽ rà soát trademark policy của Dronecode trước khi công bố.
