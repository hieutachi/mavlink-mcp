# Technical summary

For a reviewer who wants the mechanism, not the pitch. Companion to
[PROJECT_OVERVIEW.md](PROJECT_OVERVIEW.md).

## Trust boundaries

```
Agent / MCP client          untrusted: may send anything, may be compromised
        |  stdio
MCP tool surface            strict types; no field accepts natural language
        |
Drone facade                no gateway configured -> every write refused
        |
SafetyGateway  ............ TRUST BOUNDARY (in-process, G1)
        |
Durable ledger              single owner, one-use nonces, hash chain
        |
ExecutionLease              per-sink gate, no await before the call
        |
MAVSDK adapter              the only module that can command a vehicle
        |
Autopilot                   its own failsafes, independent of all the above
```

The boundary at `SafetyGateway` is a software invariant, not an OS boundary.
See [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md) §1.

## The ten physical sinks

Every vehicle command goes through exactly one of these MAVSDK calls:

| Action | Sinks, in order |
| --- | --- |
| `arm` | `arm` |
| `disarm` | `disarm` |
| `takeoff` | `set_takeoff_altitude`, `takeoff` |
| `land` | `land` |
| `hold` | `hold` |
| `return_to_launch` | `return_to_launch` |
| `goto` | `goto_location` |
| `fly_mission` | `upload_mission`, `start_mission` |

Each consumes a lease immediately before its call, with no `await` between the
gate and the call. Multi-sink actions are where per-sink revalidation matters.

## Request lifecycle

**1. Normalization, before any await.** Public input becomes canonical
integers: latitude/longitude ×10⁷, altitude and speed ×1000, yaw ×1000. Caller
containers are copied, so mutating a waypoint list after submission cannot
change what flies. Over-precise values are rejected rather than rounded —
`10.00000001 m` is `INVALID_ARGUMENT`, not silently `10.000 m`.

**2. Pure policy pre-check.** Deterministic evaluation on canonical units
before any connection: readonly, role, environment match, altitude ceiling less
a numerical margin, speed bound, battery reserve. Refusals here never issue an
approval request — a human is not asked to sign something policy forbids.

**3. Observation and admission.** Connect if needed, read the cache
synchronously, then check: adapter identity matches the declared environment,
connection and home epochs match, home anchor matches its re-derivation from
the measured position, every required field is present/valid/fresh/coherent,
GPS fix and satellite count for physical profiles, vehicle state preconditions
for the action, target inside the envelope after float32 conversion.

**4. Immutable request.** A `FrozenRequest` binds schema version, request ID,
actor, vehicle, gateway, boot epoch, environment, connection epoch, home epoch,
policy hash, action, canonical parameters, reference frame, home anchor, mission
digest and creation time. The SHA-256 digest is recomputed in `__post_init__`
and a supplied digest that disagrees is rejected.

**5. Approval request.** The request is persisted `AWAITING_APPROVAL` with a
single-use nonce and a 60-second window. The published issue instant is the one
stored — reading the clock twice here was a real defect.

**6. Out-of-band signing.** `safemav-operator review` prints the record;
`approve` requires a TTY, requires `--operator-id` explicitly, requires typing
the exact request ID, and timestamps after the human pause without ever
renewing the stored expiry. Private keys exist only on the operator's machine.

**7. Verification.** Token decode rejects non-canonical base64url and oversized
input. Claims are compared by canonical bytes, not `==`, so `True` and `1` are
distinguishable. The signature is Ed25519 over `b"SafeMAV-Approval-v1\0"` plus
canonical claim bytes. Then: key scope, stored request payload equality, nonce
state, and both time windows.

**8. Atomic claim.** `claim_and_intent` consumes the nonce and moves the
request to `EXECUTING` in one transaction, writing an `EXECUTION_INTENT` audit
event. Concurrent submissions of one approval produce exactly one dispatch.

**9. Lease and dispatch.** The lease carries the signed UTC expiry, a monotonic
deadline, the issuer identity, the vehicle, the exact sink sequence, the frozen
plan, and a recheck callback. Dispatch is bounded by `asyncio.wait` rather than
`wait_for`, so a task that ignores cancellation cannot hold the gateway open.

**10. Per-sink gate.** Before each call: issuer and vehicle binding, UTC expiry
and monotonic deadline, correct next sink in sequence, exact plan parameters,
full re-run of binding and admission, operator key still in scope, ledger still
writable. Then the deadline checks again, after the recheck.

## What each attack runs into

| Attack | Mechanism that stops it |
| --- | --- |
| `confirm=true` | Accepted and ignored; authorization needs a signature |
| Forged token | Ed25519 verification against the configured public key |
| Replayed token | Nonce is `CONSUMED`; request state is terminal |
| Altered altitude | Digest mismatch, then canonical byte comparison |
| Different vehicle | `vehicle_id` bound in claims and in the lease |
| Different gateway | `gateway_id` and boot epoch bound |
| Restart-then-replay | Boot epoch changes; pending approvals invalidated and audited |
| Stale telemetry | Per-field receipt age and inter-field skew |
| Moved home | Home epoch increments; anchor re-derived and compared |
| Reconnection | Connection epoch increments; cache cleared before any await |
| Injected text | No natural-language field exists in the authorization path |
| Hostile flight-mode string | Allow-listed; never copied into the ledger |
| Expiry mid-takeoff | Second sink's gate fails; request recorded `UNKNOWN` |
| Revoked key mid-takeoff | Key scope re-checked at every sink |
| Ledger failure mid-takeoff | `check_writable` per sink; writer latches closed |
| Concurrent duplicates | Atomic nonce claim plus dispatch lock |
| Uncooperative task | Bounded `asyncio.wait`; lease revoked; quarantine |
| Direct adapter call | No lease — `LeaseError` |
| Self-minted lease | Issuer identity mismatch |
| Airborne disarm | State precondition, enforced even with a valid signature |
| **Stolen in-process issuer** | **Nothing. See KNOWN_LIMITATIONS §1** |

## Design decisions worth defending

**Canonical integers.** Removes free text from authorization entirely, makes
digests byte-exact, and makes float32 wire rounding explicit. The last one
matters: a value can pass a float64 ceiling check and exceed it after
conversion to the protobuf `float` field. `tests/safety/test_wire_types.py`
pins the installed MAVSDK field types and verifies the effective altitude after
conversion.

**Monotonic deadline alongside UTC expiry.** Wall-clock time can jump. The
signed expiry is authoritative for the operator's window; the monotonic
deadline bounds the execution attempt independently.

**Uncertainty latches rather than recovers.** A timeout, cancellation or
backend exception records `UNKNOWN` with partial effects and refuses subsequent
commands — including after a restart, reconciled from the ledger. No retry, and
specifically no automatic disarm: cutting motors in flight drops the aircraft.

**Recovery is not gated on flight mode.** Land, hold and RTL remain available
in states where navigation is refused, and on a low battery. De-escalation must
stay cheap. They still require a signed approval.

**Acceptance is not completion.** `ACKNOWLEDGED` is RPC acceptance. Every demo
observes telemetry before concluding anything about the vehicle.

## Observation model

`TelemetryField` carries per-field receipt time, source, connection epoch,
validity and optional home epoch. Timestamps are recorded when each value
arrives, not when a snapshot is assembled — stamping after a `gather` would
make a 5-second-old value look fresh.

`safety_snapshot()` is synchronous, performs no I/O and refreshes no timestamp.
Values are validated for actual type and range, not merely for a validity flag:
NaN, infinities, out-of-range coordinates, non-boolean armed states and
future-dated receipts all fail closed.

Home is quantized to a canonical anchor using decimal half-even rounding, so
sub-millimetre sensor jitter does not change it, while a genuine relocation
increments the home epoch and invalidates outstanding approvals.

## Measured

| | Baseline `962448c` | SafeMAV `7055a49` |
| --- | --- | --- |
| Unauthorized execution | 82.6% [79.6–85.3] | 4.3% [3.1–6.1] |
| Unauthorized takeoff | 66.7% | 0% [0–2.1] |
| Replay accepted | 100% | 0% |
| Tampering accepted | 100% | 0% |
| Degraded-state execution | 100% | 0% |
| Envelope violations | 0% | 0% |
| False rejection | 0% | 0% |
| Authorization latency | 0.002 ms | 13.5 ms median |

2,340 trials. Prevention requires no sink invoked and no state change.

PX4 SITL: 4 actions acknowledged and observed, 10 refusals with nothing
reaching the autopilot, 19 audit events with a verified chain. Measured
telemetry rates: position/attitude ~50 Hz, battery/home 1 Hz with worst
observed age 1.97 s — which is why freshness bounds are 3 s, not the 0.5 s the
architecture document had proposed.

## Code map

| Path | Lines | Role |
| --- | --- | --- |
| `src/mavlink_mcp/safety/` | 1,795 | The whole safety layer |
| `src/mavlink_mcp/adapter.py` | — | `ExecutionLease`, per-sink gate |
| `src/mavlink_mcp/operator_cli.py` | — | Out-of-band signing |
| `tests/safety/` | — | 15 files, the bulk of 744 tests |
| `benchmark/` | — | Two arms, frozen manifest, oracle, raw data |
| `tools/` | — | SITL rig, rate measurement, independent recomputation |
