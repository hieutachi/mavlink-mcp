# Changelog

## Unreleased — SafeMAV safety gateway

**Breaking change.** `confirm=true` no longer authorizes anything. Mutating
methods return a `GatewayResult` instead of raising, and without a trusted
`SAFEMAV_CONFIG` launch file the server is read-only. Migration: an agent's
first call returns an approval request; a human signs it with
`safemav-operator approve`; the agent resubmits the same call with
`request_id` and `approval_token`.

Added:

- Policy-enforced safety gateway. Every command that can move the vehicle needs
  an Ed25519 approval signed by an operator key the server holds only the public
  half of, bound to actor, vehicle, gateway, boot epoch, policy hash, connection
  and home epochs, and canonical parameters. Single use.
- Per-sink revalidation. A takeoff is two MAVSDK calls; the approval deadline,
  telemetry freshness, home and connection epochs, policy hash, operator key
  scope and ledger writability are all re-checked immediately before each one.
  If anything lapses in between, the second call does not happen and the request
  is recorded `UNKNOWN` with partial effects, latching the writer closed across
  restarts.
- Single-owner SQLite ledger with one-use nonces and a hash-chained audit trail.
- `safemav-operator` CLI for out-of-band review and signing at a terminal.
- Canonical integer units (degrees ×10⁷, millimetres, millidegrees) with
  float32 wire-rounding checks pinned against the installed MAVSDK protobufs.
- Per-field telemetry freshness with measured thresholds; connection lifecycle
  with generation-guarded subscriptions.
- Network-free kinematic simulator backend (`MAVLINK_MCP_BACKEND=sim`).
- Quantitative benchmark against the pre-gateway guardrails: unauthorized
  execution 82.6% → 4.3%, unauthorized takeoff 66.7% → 0%, replay and parameter
  tampering 100% → 0%, false rejection 0% on both arms. Raw data committed.
- Verified against headless PX4 SITL through the real MAVSDK adapter: four
  actions executed and observed, ten refusals with nothing reaching the
  autopilot.
- 744 tests. Documentation: threat model, security analysis, adversarial test
  map, installation, demo guide, benchmark results, SITL evidence.

Known gap, asserted by a passing test rather than only described: enforcement
is an in-process boundary. Same-process code can read the gateway's private
issuer and forge a lease. Seven of eight bypass attempts are blocked; that one
is not, and it accounts for the entire residual 4.3%.

`ACKNOWLEDGED` means the autopilot accepted a command, never that a flight
completed. No physical aircraft has flown with this code; ArduPilot is untested.

## v0.1.0 (unreleased) — MVP1

Initial release: vendor-neutral UAV capability layer + MCP server.

- Capability model with 8 capabilities: telemetry, flight_mode, arm_disarm, takeoff, land, goto, mission, return_to_launch.
- MAVSDK-Python (BSD-3) adapter behind an explicit `DroneAdapter` interface — the only module that touches the protocol.
- MCP server (FastMCP, stdio) with 12 tools; same capabilities available as a typed async Python library (`mavlink_mcp.Drone`).
- Safety guardrails: readonly mode **on by default** (`MAVLINK_MCP_READONLY`), explicit `confirm=true` required for `arm` and `takeoff`, configurable altitude ceiling (`MAVLINK_MCP_MAX_ALTITUDE_M`) and soft geofence around home (`MAVLINK_MCP_GEOFENCE_RADIUS_M`); `land`/`hold`/`return_to_launch` stay confirmation-free.
- Test suite against a mock adapter (no SITL needed) + MAVSDK adapter smoke tests.
- Docs: README, PX4/ArduPilot simulator quickstart, SITL patrol example, roadmap, security policy.
