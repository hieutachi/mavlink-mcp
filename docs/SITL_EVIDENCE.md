# PX4 SITL evidence

Machine-readable results: [SITL_EVIDENCE.json](SITL_EVIDENCE.json). Everything
below was measured by running the code; nothing is estimated.

## What was run

| Item | Value |
| --- | --- |
| Autopilot | PX4 SITL, PX4's built-in SIH dynamics, headless (no Gazebo, no GPU) |
| Airframe | `sihsim_quadx` (`PX4_SYS_AUTOSTART=10040`) |
| Backend | `MavsdkAdapter`, the same adapter the MCP server uses |
| MAVSDK | 3.17.2 (the version this project pins) |
| Host | WSL2, Linux 6.18.33.1-microsoft-standard-WSL2, x86_64 |
| Endpoint | `udpin://0.0.0.0:14540` (PX4's onboard MAVLink stream) |
| Physical hardware | none contacted |

Reproduce with:

```bash
bash tools/run_sitl.sh          # starts headless PX4, prints its MAVLink ports
python tools/measure_rates.py   # per-field telemetry age, read-only
python tools/sitl_evidence.py   # signed-approval flight + deny matrix
```

`tools/run_sitl.sh` deletes PX4's persisted parameters first, because PX4 keeps
the simulated vehicle armed and airborne between runs otherwise, which silently
invalidates a preflight-state test.

## Measured telemetry rates

This is why the freshness thresholds are what they are. Per-field receipt age
over 30 s, ~297 samples per field:

| Field | median | p95 | max |
| --- | --- | --- | --- |
| `attitude` | 0.002 s | 0.007 s | 0.012 s |
| `position` | 0.014 s | 0.016 s | 0.020 s |
| `gps` | 0.071 s | 0.124 s | 0.128 s |
| `armed` | 0.460 s | 0.961 s | 0.965 s |
| `flight_mode` | 0.460 s | 0.961 s | 0.965 s |
| `in_air` | 0.459 s | 0.960 s | 0.965 s |
| `battery` | 0.963 s | 1.870 s | 1.974 s |
| `home` | 0.962 s | 1.870 s | 1.974 s |

PX4 streams position and attitude at roughly 50 Hz but battery and home at
1 Hz. Because one snapshot mixes both, inter-field skew is as large as the
slowest stream's period. The architecture draft proposed `max_age_s = 1.0` and
`max_skew_s = 0.5`; against this build those values reject healthy telemetry.
An earlier evidence run proved it: RTL was refused with `TELEMETRY_STALE` at
`max_skew_ms = 1500`. The run of record uses 3000 ms for both bounds.

These numbers describe this PX4 build on this host. They are a starting point
for a controlled simulator, not validated safety thresholds for any aircraft.

## Signed flight, observed outcomes

Each step was proposed by the agent, signed by a separate operator key, then
resubmitted. `ACKNOWLEDGED` means PX4 accepted the command; the observed column
is what telemetry then showed.

| Action | Result | Observed |
| --- | --- | --- |
| `arm` | ACKNOWLEDGED | `armed = true` |
| `takeoff` 10 m | ACKNOWLEDGED | climbed to 8.23 m before the check |
| `goto` | ACKNOWLEDGED | reached latitude 47.397804 |
| `return_to_launch` | ACKNOWLEDGED | `in_air = false` |

Home anchor quantized from the measured home position:
`{latitude_e7: 473977428, longitude_e7: 85455941, absolute_altitude_amsl_mm: 489412, vertical_datum: "AMSL"}`.

## Refusals (10/10 as expected)

A refusal counts only if no sink reached PX4.

| Case | Reason code |
| --- | --- |
| No approval, `confirm=true` only | `APPROVAL_REQUIRED` |
| Signature altered by six characters | `APPROVAL_INVALID` |
| Valid token without its `request_id` | `APPROVAL_INVALID` |
| Takeoff to 500 m against a 30 m ceiling | `ALTITUDE_LIMIT` |
| `goto` while still on the ground | `UNSAFE_STATE` |
| Takeoff altitude `NaN` | `INVALID_ARGUMENT` |
| Altitude `10.00000001` (sub-millimetre) | `INVALID_ARGUMENT` |
| In flight: target ~12 km away, 200 m fence | `OUTSIDE_GEOFENCE` |
| In flight: `goto` 100 m against a 30 m ceiling | `ALTITUDE_LIMIT` |
| In flight: mission whose second waypoint is outside the fence | `OUTSIDE_GEOFENCE` |

The grounded `goto` returns `UNSAFE_STATE` rather than `OUTSIDE_GEOFENCE`
because the preflight state check runs first. The geofence is exercised
separately in flight.

## Audit ledger

19 events, hash chain verified. Every accepted action produced
`APPROVAL_REQUESTED` → `EXECUTION_INTENT` → `SINK_CHECK` (one per physical
sink) → `REQUEST_ACKNOWLEDGED`. Refused requests produced an
`APPROVAL_REQUESTED` and nothing further.

## Two real defects this run exposed

Both were found by running the code, not by reading it, and both are now fixed
with regression tests.

**1. A valid approval could expire because the clock ticked.** The gateway read
the wall clock once to build the request it published to the operator, and the
ledger read it again when storing the approval window. When a millisecond
boundary fell between the two reads, the stored `issued_at` was 1 ms later than
the one the operator signed, and verification rejected a perfectly valid
approval with `APPROVAL_EXPIRED`. It only showed up over stdio, at a different
step each run, which is what made it look like a flaky test rather than a bug.
The ledger now stores exactly the instant the gateway published. Pinned by
`tests/safety/test_clock_window.py` with a clock that advances on every read.

**2. Needing approval was reported as an error.** `REQUIRE_APPROVAL` is the
normal first step of every dangerous action, but `to_dict()` tagged it with
`error` and `guardrail: true`. That teaches an agent to treat "ask the human"
as a fault condition to work around. Only `DENY` carries `error` now.

## What this does not establish

- No physical aircraft was involved. SITL exercises the MAVLink and MAVSDK
  path, not airworthiness, radio links, or real sensor failure modes.
- `ACKNOWLEDGED` is PX4 accepting a command. Full completion arbitration over
  observed flight state is not implemented.
- ArduPilot was not tested at all.
- The gateway is enforced for the Python API and MCP tools in this process. It
  is not an OS-level sandbox: code already running inside the process can read
  the gateway's private issuer object and forge a lease. See
  [THREAT_MODEL.md](THREAT_MODEL.md).
