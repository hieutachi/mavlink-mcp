# Security analysis

What this system enforces, how each claim is evidenced, and where it does not
hold. Every claim here names a test, a measurement or a recorded run. Claims
without evidence are marked as such rather than stated softly.

Companion documents: [THREAT_MODEL.md](THREAT_MODEL.md) for adversaries and
trust boundaries, [ADVERSARIAL_TESTS.md](ADVERSARIAL_TESTS.md) for the T01–T25
mapping, [BENCHMARK_RESULTS.md](BENCHMARK_RESULTS.md) for rates,
[SITL_EVIDENCE.md](SITL_EVIDENCE.md) for the autopilot run.

## 1. The property being claimed

> An AI agent can propose any UAV action. It cannot cause a physical write
> without an Ed25519 approval, signed by a key the agent never holds, bound to
> that exact request, and still valid at the instant of each individual write.

Three words carry the weight.

**Physical write** means one of ten MAVSDK calls: `arm`, `disarm`,
`set_takeoff_altitude`, `takeoff`, `land`, `hold`, `return_to_launch`,
`goto_location`, `upload_mission`, `start_mission`. These are the only places
the vehicle can be commanded, and each one consumes an execution lease
immediately before the call with no `await` in between.

**Bound to that exact request** means the approval covers actor, vehicle,
gateway, boot epoch, environment, connection epoch, home epoch, policy hash,
action, canonical parameters, reference frame, home anchor and mission digest.
Changing any of them invalidates it.

**At the instant of each write** means a two-sink action re-verifies everything
between its sinks. Authority is not granted once per request.

### What is deliberately not claimed

- Not that a signature proves informed human judgement. It proves possession of
  a key.
- Not that `ACKNOWLEDGED` means a flight completed. It means the autopilot
  accepted a command.
- Not that telemetry freshness authenticates sensors. A receipt timestamp is
  local arrival time, not proof the vehicle measured anything.
- Not resistance to an attacker with code execution inside the process. See §6.
- Not anything about physical airworthiness, radio links or real sensor
  failure. No aircraft has flown with this code.

## 2. Enforcement chain

Each layer must pass; any failure stops the command.

```
Agent / MCP client          untrusted: may send anything
        |
MCP tool surface            strict types; no field accepts free text
        |
Drone facade                no gateway configured -> every write refused
        |
SafetyGateway               identity binding, policy, observation, approval
        |
Durable ledger              one-use nonce, hash-chained audit, EXECUTING claim
        |
ExecutionLease              per-sink: issuer, vehicle, both deadlines, recheck
        |
Adapter sink                the only place a vehicle is commanded
        |
Autopilot                   its own failsafes, independent of all the above
```

### Why canonical integers matter

Public input is converted to integers before anything else happens: degrees to
1e-7, metres to millimetres, degrees of yaw to millidegrees. This is not
cosmetic.

It removes free text from the authorization path, so there is no field in which
an instruction could be interpreted (§5). It makes the signed digest
byte-exact, so `True` and `1` are distinguishable — Python's `==` is not. And it
makes float32 rounding explicit: `tests/safety/test_wire_types.py` pins the
MAVSDK protobuf field types and verifies the altitude actually sent after
float32 conversion stays within the configured margin, so a value cannot pass
policy in float64 and exceed the ceiling on the wire.

## 3. Claim-by-claim evidence

| Claim | Evidence | Status |
| --- | --- | --- |
| A boolean cannot authorize a dangerous action | `test_server_tools.py::test_confirm_alone_never_authorizes_and_token_alone_is_rejected`; `confirm` is accepted and ignored | enforced |
| Approval binds to exact parameters | `test_gateway.py::test_changed_parameters_and_action_never_reach_sink`; benchmark tampering 0/150 | enforced |
| Approval is single-use | `test_gateway.py` replay → `APPROVAL_REPLAY`; benchmark replay 0/60 | enforced |
| Approval expires | `test_clock_window.py::test_expired_window_is_still_refused`; both UTC and monotonic deadlines | enforced |
| Vehicle identity is bound | `test_injection_and_conflict.py::test_vehicle_identity_change_invalidates_an_approval`, `::test_approval_is_not_transferable_between_gateways` | enforced |
| Operator key scope is enforced | `test_approval_integrity.py::test_key_scopes_are_enforced`; re-checked at every sink | enforced |
| Authority re-checked per sink | `test_gateway_outcomes.py::test_second_sink_rechecks_all_authority`, 8 failure modes × 2 actions | enforced |
| Stale telemetry blocks action | `test_telemetry_freshness.py`; thresholds measured from PX4, not assumed | enforced |
| Geofence and ceiling hold | `test_sink_matrix.py`; also 3/3 refusals in flight on live SITL | enforced |
| Injected text cannot authorize | `test_injection_and_conflict.py`, 8 payloads × 6 entry points | structural |
| Airborne disarm is refused | `test_sensor_preconditions.py::test_airborne_disarm_stays_refused_even_with_a_signed_approval` | enforced |
| Uncertain outcome latches the writer | `test_gateway_outcomes.py::test_backend_exception_is_unknown_and_survives_restart` | enforced |
| Audit chain is verifiable | `test_store_approval.py`; 19 events verified on SITL, 37 over stdio | enforced |
| Ledger has a single owner | `test_store_approval.py`, OS file lock, tested on Windows | enforced |
| Direct Python API cannot bypass | `test_bypass_attempts.py`: 7 of 8 blocked | **partial — see §6** |
| Legitimate operations still work | 0% false rejection on 180 trials; 15 simulator flights; 4 SITL actions | enforced |

"Structural" means the property follows from the data model rather than from a
filter: there is no text field to sanitize.

## 4. Measured effect

From [BENCHMARK_RESULTS.md](BENCHMARK_RESULTS.md), 2,340 trials. An action
counts as prevented only if no sink fired and vehicle state did not change.

| | Original guardrails | This system |
| --- | --- | --- |
| Unauthorized execution | 82.6% | 4.3% |
| Unauthorized takeoff | 66.7% | 0% |
| Replay accepted | 100% | 0% |
| Parameter tampering accepted | 100% | 0% |
| Degraded-state execution | 100% | 0% |
| **False rejection of legitimate ops** | **0%** | **0%** |

The residual 4.3% is one scenario, the §6 gap; excluding it, 0/660.

Two honest observations. The original altitude ceiling and haversine geofence
score identically to the gateway — 0/60 on envelope violations. Those checks
were sound and are the part of the old design that did not need replacing. And
a deny-everything configuration is not a solution: the readonly control arm
scores 100% false rejection while still leaking 13% of unauthorized executions,
because `readonly` is a facade flag that direct adapter calls ignore.

Authorization costs 13.4 ms median, dominated by ledger fsync (4.1 ms/event),
not signature verification (48 µs). In-process, one host, excluding the human.

## 5. Prompt injection

The agent is assumed hostile. Injection is therefore not filtered; it is
structurally excluded.

No field in the authorization path accepts natural language. Parameters are
canonical integers, `request_id` and `approval_token` are opaque, and identity
comparison is byte-wise. Eight payloads are tested across six entry points:
instruction override, SQL, script tags, a forged `SYSTEM:` prefix, NUL and ANSI
escapes, path traversal, template syntax, and a right-to-left override. All are
rejected as malformed input.

Flight mode is the only string the system reads from the vehicle, and it is
checked against an allow-list. A hostile autopilot reporting its mode as
`"HOLD; operator approved; ignore policy"` makes the vehicle ineligible for
navigation rather than persuading the gateway, and
`test_telemetry_text_is_never_copied_into_the_audit_trail` verifies the string
never reaches the ledger.

An LLM refusal is never counted as enforcement anywhere in this project. No
language model participates in any decision or in the benchmark.

## 6. The G1/G2 boundary — the central limitation

**G1, API enforcement: holds.** Through the MCP tools and the supported Python
API, a caller cannot reach a sink without valid authorization.

**G2, deployment containment: not implemented.** Code already executing inside
the interpreter can read the gateway's private issuer object, forge a valid
lease, and command the vehicle.

Eight bypass attempts are tested. Seven are blocked:

| Attempt | Result |
| --- | --- |
| Second `Drone` facade over the bound adapter | `READONLY` |
| Direct `adapter.arm()` | `LeaseError` |
| Self-minted `ExecutionLease` | `LeaseError` |
| Lease bound to a different vehicle | `LeaseError` |
| Lease without the gateway recheck hook | `LeaseError` |
| Second gateway claiming the same adapter | `LeaseError` |
| Readonly principal | `READONLY` |
| **Reading `gateway._issuer` and forging a lease** | **succeeds** |

The eighth has a test that asserts it succeeds —
`test_known_gap_same_process_issuer_theft_is_not_prevented` — so the limitation
cannot silently vanish from the documentation. In the benchmark it is
`S17:stolen_in_process_issuer`, 30/30 on both arms, and it accounts for the
entire residual 4.3%.

One mitigation, which is not prevention: the forged write leaves no ledger
entry, so a physical effect with no corresponding audit record is detectable
afterwards.

Closing this needs OS-level separation — the gateway in its own process with
sole access to the vehicle network, the agent sandboxed with only an MCP pipe.
The architecture is designed for it and it is not built. Any claim of
"unbypassable" would be false.

## 7. Residual risks

**A signature is possession, not understanding.** The CLI shows the full
request and requires typing the exact request ID, but a coerced, rushed or
inattentive operator still produces a valid approval. Approval fatigue is a
real failure mode and this design does not solve it.

**Freshness is arrival time, not authenticity.** A compromised autopilot or
GPS spoofing produces well-formed, timely, wrong telemetry. The gateway would
authorize against it. Detecting that is out of scope.

**Audit is tamper-evident, not tamper-proof.** The hash chain detects
retroactive edits only if a verifier holds an independent reference. An attacker
with write access to the database file and no external checkpoint can rewrite
the chain consistently.

**Target checks, not trajectories.** Altitude and geofence are evaluated on the
commanded target. The path flown between waypoints is the autopilot's business;
there is no terrain or obstacle model.

**The monitor shares the event loop.** A frozen or dead loop takes the
observation path with it. Autopilot failsafes are the independent layer, and
they are outside this system.

**Single vehicle, single gateway owner.** No fleet authorization, no
distributed approval, no key rotation or recovery procedure, no HSM
integration.

**Clock dependence.** Approval windows use wall-clock UTC plus a monotonic
deadline. Large clock skew between the gateway and the operator's machine
causes false rejections — which is the safe direction, but it is a dependence.

**Pinned dependencies reduce uncertainty, not risk.** Three direct
dependencies, exact-ranged. That is not proof they are free of vulnerabilities.

## 8. Known defects found and fixed

Both were found by running the system, not by reading it, and both now have
regression tests. Recording them is part of the evidence.

**A valid approval could expire because the clock ticked.** The gateway read
the wall clock to build the request it published to the operator; the ledger
read it again when storing the window. If a millisecond boundary fell between
the two reads, the stored `issued_at` was 1 ms later than the signed one and
verification rejected a valid approval. It appeared only over stdio, at a
different step each run. Fixed by storing the published instant;
`test_clock_window.py` pins it with a clock that advances on every read.

**Needing approval was reported as an error.** `REQUIRE_APPROVAL` carried
`error` and `guardrail: true`, teaching an agent to treat asking a human as a
fault to route around. Only `DENY` carries `error` now.

Also worth recording: two oracle defects in the benchmark verification script
itself, which would have made correct behavior look like failure — counting a
correct single dispatch under concurrency as replay acceptance, and folding
required refusals into the positive-control denominator. Fixed and documented
in [BENCHMARK_RESULTS.md](BENCHMARK_RESULTS.md).

## 9. Deployment guidance

If anyone runs this near a real vehicle, which has never been done:

1. Keep the operator private key off the server host entirely.
2. Set `bootstrap: false` after first run, so a deleted ledger fails closed.
3. Run the gateway in its own OS process and user, with sole network access to
   the vehicle. Until that exists, §6 applies in full.
4. List `vehicles` and `actions` explicitly on every trust key; empty means
   unrestricted.
5. Configure the autopilot's own geofence, altitude limit and failsafes
   independently. This system is not a substitute for them.
6. Keep a human with an RC transmitter and the ability to take manual control.
7. Checkpoint the audit chain somewhere the gateway cannot write.
8. Treat `ACKNOWLEDGED` as acceptance and observe telemetry before concluding
   anything about what the vehicle did.

Real flights are subject to local aviation law. In Vietnam, Law 49/2024 and
Decree 288/2025 require registration and flight permits.
