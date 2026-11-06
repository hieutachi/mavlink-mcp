# Security Policy

## Reporting a vulnerability

Please report security issues **privately** via GitHub's *Security → Report a vulnerability* (private vulnerability reporting) on this repository, or by opening a minimal issue asking for a private contact channel — do not post exploit details publicly. You can expect an acknowledgement within 7 days. Please include reproduction steps and the affected version.

Supported versions: only the latest released version receives security fixes.

## Deployment security model

Understand what this software does and does not protect:

- **MAVLink links have no authentication by default.** A vehicle listening for MAVLink on UDP accepts commands from anyone who can reach that port — with or without this project. Never expose a MAVLink endpoint to the public internet. Keep vehicles on an isolated/trusted network (lab VLAN, VPN such as WireGuard/Tailscale) and firewall the ports to known hosts. MAVLink 2 message signing exists but is not configured by this project.
- **This server is a MAVLink peer**, connecting only to the `MAVLINK_MCP_URL` you configure. Every command that can move the vehicle requires an Ed25519 approval signed by a separate operator key, bound to that exact request, single-use, and re-verified immediately before each physical write. `confirm=true` is deprecated and authorizes nothing. See [docs/SECURITY_ANALYSIS.md](docs/SECURITY_ANALYSIS.md).
- **Enforcement is an in-process boundary, not an OS sandbox.** Code already running inside the interpreter can read the gateway's private issuer object and forge an execution lease. Seven of eight bypass attempts are blocked and tested; that one is not, and `tests/safety/test_bypass_attempts.py` asserts it succeeds so the gap cannot be lost. Closing it requires running the gateway in its own process with sole access to the vehicle network.
- **None of this replaces network security or the autopilot's failsafes.** A hostile actor on the network can still talk MAVLink to the vehicle directly, and the soft geofence is not a flight-controller geofence: configure a real geofence and failsafes in PX4/ArduPilot too.
- **The MCP client (LLM) decides which tools to *call*; it cannot decide what executes.** Write access needs a trusted `SAFEMAV_CONFIG` launch file naming operator public keys. `MAVLINK_MCP_READONLY=false` alone grants nothing. Keep operator private keys off the server host, and keep an RC transmitter with manual override within reach when real hardware is involved.
- **No aircraft has flown with this code.** Verification is against PX4 SITL ([docs/SITL_EVIDENCE.md](docs/SITL_EVIDENCE.md)); ArduPilot is untested.
- **Telemetry is untrusted input to the LLM.** A malicious or compromised MAVLink endpoint could embed prompt-injection payloads in string fields this server returns, potentially steering the connected LLM into unintended tool calls — only connect to vehicles and simulators you trust.

## Disclaimer of liability

This software is provided **"AS IS"**, without warranty of any kind, under the [MIT License](LICENSE). It is built for **simulation and research**. Operating a real aircraft through this software is done entirely at the operator's own risk, and the operator is solely responsible for compliance with applicable aviation law (registration, flight permits, pilot licensing, no-fly zones — for example, Vietnam's Law on People's Air Defence 49/2024 and Decree 288/2025 require UAV registration and per-flight permits). The authors and contributors accept no responsibility for damage to aircraft, property, or persons resulting from commands issued through this software — the person who disables readonly mode and connects a vehicle is the responsible operator.
