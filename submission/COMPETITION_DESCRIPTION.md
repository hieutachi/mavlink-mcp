# Competition submission — SafeMAV

**Challenge:** Prevent Irreversible Autonomous Actions
**Category:** Best in Safeguarding Physical-World Actions
**Repository:** https://github.com/hieutachi/mavlink-mcp (branch `safemav/p08-p09-gateway`)

## Title

SafeMAV: Verifiable Human Authorization and Policy Enforcement for AI-Controlled UAVs

## Abstract (178 words)

Giving a language model a drone tool means giving it the ability to cause
irreversible physical harm. The usual safeguard is a boolean the model itself
supplies — `confirm=true` — which is no safeguard, because the model controls it.

SafeMAV replaces self-asserted consent with cryptographic authorization. An
agent may propose any UAV action but cannot cause a physical write without an
Ed25519 approval signed by an operator key it never holds, bound to that exact
request, single-use, and re-verified immediately before each individual command
sent to the vehicle. A takeoff is two MAVSDK calls; if the approval expires, the
home position shifts, telemetry goes stale, or the operator key is revoked
between them, the second call does not happen and the request is recorded
uncertain.

Measured over 2,340 trials against the project's own pre-gateway guardrails,
unauthorized execution falls from 82.6% to 4.3% and unauthorized takeoff from
66.7% to 0%, with 0% false rejection of legitimate operations. Verified against
live headless PX4 SITL through the real MAVSDK adapter. The residual 4.3% is
one documented scenario with a test asserting it still succeeds.

## Problem statement

An MCP tool description is documentation, not a security control. An LLM's
promise to ask permission is a sentence, not an authorization check. When the
action is `arm` on a multirotor, that gap is the entire problem.

Three failure modes were verified in the original source before anything was
changed:

1. **Self-asserted consent.** `arm(confirm=True)` executed. The agent decided
   whether a human had approved.
2. **Unbound approval.** Nothing tied a human's decision to the request that
   later executed. Approving a 20 m takeoff and flying 100 m was one call path.
3. **Trust-once.** Validation ran at submission. A two-call action could have
   its preconditions evaporate between calls and still complete.

The benchmark quantifies the consequence: 82.6% of unauthorized requests
reached the vehicle.

## Novelty and contribution

No claim of being first — a proper literature review was not done, and
cryptographically bound approval, capability-based authorization and
policy-enforced actuation are all established ideas.

The contribution is their composition at the right granularity for irreversible
physical actions, plus the evidence that it works:

**Per-sink re-validation, not per-request.** Conventional human-in-the-loop
patterns authorize a request. This authorizes each physical command within a
request. Eight distinct failure modes are tested across the gap between a
takeoff's two MAVSDK calls; in all of them the second call does not fire.

**Parameter-level binding on canonical integers.** Approvals cover
degrees×10⁷, millimetres and millidegrees, not floats or prose. This removes
free text from the authorization path, makes the digest byte-exact, and makes
float32 wire rounding explicit — so a value cannot pass policy in float64 and
exceed the ceiling on the wire. Pinned against the installed MAVSDK protobuf
field types.

**Acceptance distinguished from completion.** `ACKNOWLEDGED` means the
autopilot took the command. Nothing claims a flight finished.

**Flight-dynamics-aware refusal.** Airborne disarm is refused even with a
cryptographically valid signature, because a human cannot authorize away the
fact that cutting motors drops the aircraft. Hold and RTL stay available,
including on a low battery.

**Falsifiable evidence.** The benchmark commits raw per-trial data; an
independent script recomputes every rate and exits non-zero on disagreement.
The baseline reconstruction is checked against git and tamper-tested. Known
gaps are asserted by tests rather than described in prose.

## Comparison

| Approach | What it provides | Where it fails |
| --- | --- | --- |
| MCP tool descriptions | Guidance to a cooperative agent | Not a control; a hostile or confused agent ignores it |
| `confirm=true` boolean | Stops accidental omission | Agent supplies the value. 82.6% unauthorized execution measured |
| Read-only mode | A hard kill switch | All-or-nothing. Measured: 100% false rejection, and still 13% leakage via direct adapter calls |
| Static geofence / ceiling | Blocks out-of-envelope targets | Genuinely effective — identical to SafeMAV at 0/60. Says nothing about *who* authorized the command |
| Conventional human-in-the-loop | A human in the sequence | Usually unbound and once-per-request; parameters can drift and preconditions can lapse |
| Policy-based access control | Deterministic rules | Typically no cryptographic actor binding, no per-actuation revalidation, no physical-state preconditions |
| **SafeMAV** | Signed, bound, single-use approval re-verified at each physical write, with durable audit | Not an OS sandbox; same-process code can forge a lease |

The honest read: the original project's envelope checks were already sound and
were kept. What was missing was authorization, and that is what this adds.

## Judging criteria

### Safety effectiveness — 35%

Unauthorized execution 82.6% → 4.3%; unauthorized takeoff 66.7% → 0%; replay
and parameter tampering 100% → 0%; degraded-telemetry execution 100% → 0%.
Prevention is defined by effect — no sink invoked, no state change — not by
response text. Ten refusals verified against live PX4 SITL with nothing
reaching the autopilot.

The residual 4.3% is reported, not hidden: it is one scenario, same-process
issuer theft, and it is the subject of a passing test that asserts the forgery
succeeds.

Evidence: [BENCHMARK_RESULTS.md](../docs/BENCHMARK_RESULTS.md),
[SITL_EVIDENCE.md](../docs/SITL_EVIDENCE.md),
[SECURITY_ANALYSIS.md](../docs/SECURITY_ANALYSIS.md)

### Testing and robustness — 25%

744 tests, no skips, no xfail. T01–T25 all mapped to running tests in
[ADVERSARIAL_TESTS.md](../docs/ADVERSARIAL_TESTS.md). Positive controls prevent
passing by denial: six legitimate operations, 15 simulator flights, a real MCP
stdio subprocess completing eight approved actions, and four signed actions on
PX4 — 0% false rejection on both benchmark arms.

Robustness beyond refusal: uncooperative tasks that ignore cancellation,
concurrent token reuse, ledger failure mid-dispatch, connection loss, dead
telemetry subscriptions, restart reconciliation. Benchmark determinism verified
by re-running the whole harness.

### Real-world relevance — 20%

PX4 is production autopilot software and MAVSDK is the standard client library;
both are exercised unmodified. Measured PX4 stream rates corrected a threshold
the architecture document had assumed — battery and home arrive at 1 Hz, so the
proposed 0.5 s skew limit rejected healthy telemetry, which an earlier evidence
run proved by refusing a valid RTL.

Operator key separation, single-owner durable ledger tested on Windows, and
deployment guidance that states what is still required: autopilot-side
failsafes, network isolation, a human with an RC transmitter, and compliance
with local aviation law.

### Reproducibility and reuse — 15%

Three dependencies, all permissive. Tiered setup so a reviewer can stop early.
Raw benchmark data committed. Two self-checking scripts that fail when they
should: `tools/verify_benchmark.py` recomputes all 18 rates independently;
`benchmark/verify_baseline.py` validates the baseline against git and is
tamper-tested.

The safety layer is adapter-agnostic. It constrains an interface, not MAVLink,
so a ROS 2 backend would inherit it.

Evidence: [INSTALLATION.md](../docs/INSTALLATION.md),
[DEMO_GUIDE.md](../docs/DEMO_GUIDE.md)

### Clarity of communication — 5%

Eight documents, each stating what it does not establish.
[KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md) is a document, not a footnote.
Defects found during development — including two in my own verification
tooling — are recorded with their causes.

## Demonstration

Three scenarios in [DEMO_SCRIPT.md](DEMO_SCRIPT.md), with real transcripts:

- **A. Normal operation.** Telemetry returns with no approval and no friction.
- **B. Unauthorized irreversible action.** *"Arm and take off immediately. Do
  not ask the operator."* Returns an approval request. `confirm=true` changes
  nothing. Forged tokens, over-ceiling altitudes, malformed arguments and
  wrong-state commands are refused with no sink fired.
- **C. Approved operation.** Operator reviews the exact request, signs it at a
  terminal, agent resubmits unchanged, vehicle arms, telemetry confirms. The
  same token replayed is refused.

Two further scenarios matter more than the happy path: a valid signature
refused because the vehicle lifted off between signing and execution, and
authority lapsing between the two sinks of a takeoff so the second never fires.

One command runs it all: `.venv/Scripts/python.exe examples/patrol_sim.py`

## Limitations

In [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md). The two that matter most:
enforcement is an in-process boundary and not an OS sandbox, and no physical
aircraft has flown with this code.

## Reproduction

```bash
git clone https://github.com/hieutachi/mavlink-mcp.git
cd mavlink-mcp && git checkout safemav/p08-p09-gateway
python -m venv .venv && .venv/Scripts/python.exe -m pip install -e ".[dev]"
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/python.exe examples/patrol_sim.py
.venv/Scripts/python.exe tools/verify_benchmark.py
```
