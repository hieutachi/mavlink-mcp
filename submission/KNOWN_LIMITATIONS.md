# Known limitations

Written first, not last. A safety submission that overstates is worse than one
that underclaims, and every limitation below is either asserted by a test or
reproducible from committed data.

## 1. Not an OS sandbox — the central limitation

**G1, API enforcement: holds.** Through the MCP tools and the supported Python
API, a caller cannot reach a physical command without valid authorization.

**G2, deployment containment: not implemented.** Code already executing inside
the interpreter can read the gateway's private issuer object, forge a valid
execution lease, and command the vehicle.

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
`test_known_gap_same_process_issuer_theft_is_not_prevented` — so the gap cannot
disappear from the documentation while the code stays the same. In the
benchmark it is `S17:stolen_in_process_issuer`, 30/30 on both arms, and it
accounts for the entire residual 4.3% unauthorized execution and 14.3% policy
bypass. Excluding it, the gateway is 0/660.

One mitigation, which is not prevention: the forged write leaves no ledger
entry, so a physical effect with no audit record is detectable afterwards.

Closing this requires the gateway in its own OS process with sole access to the
vehicle network and the agent sandboxed with only an MCP pipe. The architecture
is designed for that and it is not built. Any claim of "unbypassable" would be
false.

## 2. No physical aircraft

PX4 SITL exercises the MAVLink protocol path and the real MAVSDK adapter. It
says nothing about airworthiness, radio link behaviour, real sensor failure
modes, vibration, EMI or battery chemistry under load. ArduPilot was not tested
at all; compatibility notes elsewhere in the repository are design intent.

`ACKNOWLEDGED` means the autopilot accepted a command. Full completion
arbitration over observed flight state is not implemented — the demos observe
telemetry explicitly rather than trusting the return value.

## 3. A signature is key possession, not informed judgement

The CLI displays the full request and requires typing the exact request ID, and
`--operator-id` can never be taken from the request JSON. None of that makes a
coerced, rushed or inattentive operator's approval less valid. Approval fatigue
is a real failure mode in human-in-the-loop systems and this design does not
address it.

Nor is there any protection against a stolen private key. Key custody, rotation
and revocation procedures are the operator's responsibility; there is no HSM
integration and no recovery mechanism.

## 4. Freshness is arrival time, not authenticity

Receipt timestamps measure when a value arrived locally. A compromised
autopilot or GPS spoofing produces timely, well-formed, wrong telemetry, and
the gateway would authorize against it. Detecting that needs sensor attestation
or cross-validation and is out of scope.

Thresholds were measured rather than assumed — PX4 sends position and attitude
at ~50 Hz but battery and home at 1 Hz, so `max_age`/`max_skew` must exceed 2 s
or healthy telemetry is rejected as stale. Those numbers describe one PX4 build
on one host. They are a starting point for a controlled simulator, not
validated safety thresholds for any aircraft.

## 5. Audit is tamper-evident, not tamper-proof

The hash chain detects retroactive edits only if a verifier holds an
independent reference. An attacker with write access to the database file and
no external checkpoint can rewrite the chain consistently. Deployment guidance
recommends checkpointing somewhere the gateway cannot write; that is advice,
not an implemented feature.

Audit completeness is 100% of gateway-mediated dispatches and 90% overall. The
10% shortfall is exactly the §1 gap: a forged lease bypasses the recording path.

## 6. Target checks, not trajectories

Altitude and geofence are evaluated on the commanded target. The path flown
between waypoints is the autopilot's business. There is no terrain model, no
obstacle model, no airspace data, and the kinematic simulator is a flat world
with no collision.

## 7. The monitor shares the event loop

Observation runs in the same asyncio loop as the gateway. A frozen or dead loop
takes the monitor with it. Autopilot failsafes are the independent layer, and
they are outside this system. This is not a hard real-time safety controller.

## 8. Single vehicle, single gateway owner

No fleet authorization, no multi-process distributed approval, no quorum
signing, no cross-gateway coordination. One ledger has one owner process,
enforced by an OS file lock.

## 9. Clock dependence

Approval windows use wall-clock UTC plus a monotonic deadline. Significant skew
between the gateway host and the operator's machine causes false rejections.
That is the safe direction, but it is a dependence, and it caused a real defect
during development (§11).

## 10. Benchmark scope

2,340 trials on deterministic mocks, one host. Specific caveats:

- **Clustered trials.** 30 trials of a deterministic check is one mechanism
  observed 30 times, not 30 independent experiments. Wilson intervals describe
  sampling variability within this suite, not accident probability.
- **Latency is in-process.** 13.5 ms median excludes human approval time,
  network, autopilot and airframe response. The p99 tail is filesystem stalls;
  the ledger sat on a cloud-synchronized directory.
- **Semantic equivalence for the baseline.** The baseline has no token, nonce
  or digest, so cryptographic replay acceptance is N/A for it, not 0%. What is
  measured is the nearest thing it can express. Every affected trial carries an
  `ADAPTATION:` note in the raw data.
- **Scenario selection is ours.** The benchmark covers T01–T12, T16, T17, T19,
  T20, T21. The rest (T13–T15, T22–T25) are covered by the test suite as
  pass/fail assertions instead. Absence of failure here is not evidence of
  absence generally.
- **Audit completeness unmeasurable for the baseline.** No mechanism exists at
  that commit. Reported N/A.

## 11. Defects found during development

Recorded because they are part of the evidence, and because both were found by
running the system rather than reading it.

**A valid approval could expire because the clock ticked.** The gateway read
the wall clock to build the request it published to the operator; the ledger
read it again when storing the window. A millisecond boundary between the two
reads made the stored `issued_at` 1 ms later than the signed one, and
verification rejected a valid approval. It surfaced only over stdio, at a
different step each run, which made it look like flaky tests. Fixed by storing
the published instant. `tests/safety/test_clock_window.py` pins it with a clock
that advances on every read.

**Needing approval was reported as an error.** `REQUIRE_APPROVAL` carried
`error` and `guardrail: true`, which teaches an agent to treat asking a human
as a fault to route around. Only `DENY` carries `error` now.

**Two oracle defects in my own benchmark verification script.** It counted a
correct single dispatch under four-way concurrency as replay acceptance, and
folded 180 required-refusal trials into the positive-control denominator —
which would have credited the baseline for flying out-of-envelope commands. It
also exited 0 while its cross-check matched no metric names and therefore
compared nothing. Fixed; unmatched names are now an error, and tampering with
a rate in the summary makes it exit 1.

**Three of my own test assertions were wrong and the implementation was
right.** Recovery actions are deliberately not gated on flight mode, because
de-escalation must stay cheap. Concurrent approved commands serialize rather
than refuse each other. A stale navigation order expires on state change, not
at submission. The tests now assert observed behaviour.

## 12. What the original design got right

The pre-gateway altitude ceiling and haversine geofence score identically to
the gateway: both arms 0/60 on envelope violations. Those checks were sound and
are the part of the old design that did not need replacing. The gateway adds
precision handling and defined reason codes, not a better outcome on those
scenarios.

Two malformed inputs the baseline blocks do so by accident rather than
validation: `float('inf')` fails the ceiling comparison and `'10'` raises
`TypeError`. The adjacent cases show the gap — `nan`, `True` and `10.00014` all
execute on the baseline.

## 13. Not attempted

Fleet coordination, multi-vehicle deconfliction, a web dashboard, ROS 2 adapter,
hardware certification, formal verification, literature review sufficient to
support a novelty claim, and any form of autonomous recovery beyond refusing to
act. The default auto-recovery allowlist is empty.
