# Security Policy

## Reporting a vulnerability

Please report security issues **privately** via GitHub's *Security → Report a vulnerability* (private vulnerability reporting) on this repository, or by opening a minimal issue asking for a private contact channel — do not post exploit details publicly. You can expect an acknowledgement within 7 days. Please include reproduction steps and the affected version.

Supported versions: only the latest released version receives security fixes.

## Deployment security model

Understand what this software does and does not protect:

- **MAVLink links have no authentication by default.** A vehicle listening for MAVLink on UDP accepts commands from anyone who can reach that port — with or without this project. Never expose a MAVLink endpoint to the public internet. Keep vehicles on an isolated/trusted network (lab VLAN, VPN such as WireGuard/Tailscale) and firewall the ports to known hosts. MAVLink 2 message signing exists but is not configured by this project.
- **This server is a MAVLink peer**, connecting only to the `MAVLINK_MCP_URL` you configure. Its guardrails — readonly mode (default **on**), `confirm=true` for arm/takeoff, the altitude ceiling, and the soft geofence — are a policy filter inside this process, **not** a substitute for network security or for the autopilot's own failsafes. A hostile actor on the network can still talk MAVLink to the vehicle directly, and the soft geofence is not a flight-controller geofence: configure a real geofence and failsafes in PX4/ArduPilot too.
- **The MCP client (LLM) decides which tools to call.** Treat write access (`MAVLINK_MCP_READONLY=false`) like sudo: enable it only for sessions that need it, only against a simulator unless you have accepted the risks of real flight, and keep an RC transmitter with manual override / kill switch within reach when real hardware is involved.
- **Telemetry is untrusted input to the LLM.** A malicious or compromised MAVLink endpoint could embed prompt-injection payloads in string fields this server returns, potentially steering the connected LLM into unintended tool calls — only connect to vehicles and simulators you trust.

## Disclaimer of liability

This software is provided **"AS IS"**, without warranty of any kind, under the [MIT License](LICENSE). It is built for **simulation and research**. Operating a real aircraft through this software is done entirely at the operator's own risk, and the operator is solely responsible for compliance with applicable aviation law (registration, flight permits, pilot licensing, no-fly zones — for example, Vietnam's Law on People's Air Defence 49/2024 and Decree 288/2025 require UAV registration and per-flight permits). The authors and contributors accept no responsibility for damage to aircraft, property, or persons resulting from commands issued through this software — the person who disables readonly mode and connects a vehicle is the responsible operator.
