# Roadmap

Where mavlink-mcp is going, stage by stage — following the same discipline as its sister project [rosbridge-mcp](https://github.com/hieutachi/rosbridge-mcp): one protocol first, guardrails before features, nothing shipped that isn't tested. Contributions toward any stage are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md).

Status today: 8 capabilities behind a vendor-neutral capability layer, 12 MCP tools, MAVSDK (BSD-3) adapter, and a policy-enforced safety gateway — signed single-use operator approval bound to exact parameters, re-verified before every physical write, with a hash-chained audit ledger. 744 tests, verified against headless PX4 SITL. Zero telemetry, MIT licensed. See [docs/SECURITY_ANALYSIS.md](docs/SECURITY_ANALYSIS.md) for what is and is not enforced.

## MVP1 (months 1–3) — UAV capability layer, simulation-first, MCP-native

**Goal:** a working, safe, documented capability layer over MAVLink that an AI agent can fly in SITL.

| Deliverable | Status |
| --- | --- |
| Capability model v0: 8 capabilities (telemetry, flight mode, arm/disarm, takeoff, land, goto, mission, RTL) defined in physical quantities | ✅ Done in v0.1.0 |
| MAVSDK-Python adapter (BSD-3 — deliberately not pymavlink/LGPL) behind an explicit adapter interface | ✅ Done in v0.1.0 |
| Two surfaces over one core: MCP server (12 tools) + typed async Python library (`Drone`) | ✅ Done in v0.1.0 |
| Guardrails: readonly **by default**, altitude ceiling, soft geofence | ✅ Done in v0.1.0 |
| Safety gateway: signed single-use operator approval bound to exact parameters, re-verified per physical write, hash-chained audit ledger (supersedes `confirm=true`) | ✅ Done |
| Unit tests against a mock adapter — no SITL required in CI | ✅ Done in v0.1.0 |
| Docs: README, simulator quickstart (PX4 SITL via Docker), example patrol script | ✅ Done in v0.1.0 |
| Camera capability | ✂️ Cut from MVP1 on purpose — SITL camera support is inconsistent, and we don't ship empty capabilities |
| ArduPilot SITL validated as a second target (same protocol, different dialect — first real test of the capability model) | ⏳ Remaining |
| CI: license-check + mock tests + PX4 SITL smoke test in Docker | ⏳ Remaining |
| Name/trademark check ("MAVLink" is a Dronecode trademark), then PyPI + MCP registry publication | ⏳ Remaining |
| Demo: "AI agent flies a patrol in Gazebo" video + Show HN / r/diydrones / ArduPilot Discuss launch | ⏳ Remaining |

## MVP2 (months 4–6) — Real hardware + second adapter + plugin system

**Goal:** prove "two protocols, one capability API" and fly a real Pixhawk-class vehicle.

| Deliverable | Notes |
| --- | --- |
| Validate on one real Pixhawk-class flight controller (~$50–200, self-owned hardware, open firmware only) | Requires compliance with local aviation law for any outdoor flight (registration + flight permits — e.g. Vietnam Decree 288/2025); a "flying legally" docs page ships with this stage |
| ROS 2 adapter implementing the same `DroneAdapter` interface, reusing the rosbridge client from rosbridge-mcp | One codebase, two projects benefit; brings mobile robots into the same capability model |
| Formal plugin system: adapters as external packages registered via entry points, plus a conformance test suite | The ros2_control/pluginlib model; community adapters must pass conformance to be listed |
| Standardized telemetry stream/logging (considering MCAP, Apache-2.0) for debug and replay | |

**Resources needed:** part-time dev weeks; one Pixhawk-class FC purchased through official import channels (with customs paperwork, so it can be registered); a safe, legal flight site or a partner lab with existing flight permits.

## MVP3 (after month 6 — only if MVP1/2 have traction) — Ecosystem & sustainability

**Goal:** from "a tool" to "a small ecosystem", without betraying the MIT core.

| Deliverable | Notes |
| --- | --- |
| Community adapters (Cyphal/DroneCAN, IP cameras; any proprietary-SDK wrapper only after legal review) | GREEN-zone methods only, per project policy |
| Multi-vehicle / fleet support | Synchronized with rosbridge-mcp's v0.4 fleet roadmap |
| First open-core paid tier: hosted telemetry / fleet dashboard | Core stays fully MIT; public commitment on what remains open forever |

**Decision milestone (month 6):** ≥100 GitHub stars or ≥3 real external users → continue as a standalone project. Otherwise → merge the UAV capabilities into rosbridge-mcp rather than maintaining two dying projects.

## How to help

- **Developers:** the ArduPilot SITL validation and the SITL smoke-test CI job are the best first issues — claimable now.
- **Labs / companies:** simulator time, a legal flight site, or a registered test vehicle directly unblock MVP2.
- **Users:** run the quickstart and report your PX4/ArduPilot + MAVSDK versions — compatibility reports are the cheapest way to make this robust.
