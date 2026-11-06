# SafeMAV

**Verifiable human authorization and policy enforcement for AI-controlled UAVs**

*AI can plan the flight. Only verified authority can execute it.*

Repository: https://github.com/hieutachi/mavlink-mcp
Branch: `safemav/p08-p09-gateway`
Licence: MIT

## Abstract

Giving a language model a drone tool means giving it the ability to cause
irreversible physical harm. The usual safeguard is a boolean the model itself
supplies — `confirm=true` — which is not a safeguard at all, because the model
controls it.

SafeMAV replaces that with cryptographic authorization. An agent may propose
any UAV action, but cannot cause a physical write without an Ed25519 approval
signed by an operator key the agent never holds, bound to that exact request,
single-use, and re-verified immediately before every individual command sent to
the vehicle. A takeoff is two MAVSDK calls; if the approval expires, the home
position shifts, telemetry goes stale or the operator key is revoked between
them, the second call does not happen and the request is recorded as uncertain.

Measured against the project's own pre-gateway guardrails over 2,340 trials,
unauthorized execution falls from 82.6% to 4.3% and unauthorized takeoff from
66.7% to 0%, with 0% false rejection of legitimate operations. The residual
4.3% is one scenario — same-process issuer theft — which has a test asserting
that it still succeeds, because a limitation removed from the documentation is
worse than a limitation.

Verified against live headless PX4 SITL through the real MAVSDK adapter: four
flight actions executed and observed, ten refusals with no command reaching the
autopilot.

## The problem

An MCP tool description is documentation, not a security control. An LLM's
promise to ask permission is a sentence, not an authorization check. When the
action is `arm` on a real multirotor, the gap between those two things is the
whole problem.

Three failure modes in the original design, all verified in its code before
anything was changed:

1. **Self-asserted consent.** `arm(confirm=True)` executed. The agent decided.
2. **Unbound approval.** Nothing tied a human's decision to the request that
   later executed, so approving a 20 m takeoff and flying 100 m was the same
   call path.
3. **Trust-once.** Checks ran at submission. A two-call action could have its
   preconditions evaporate between calls and still complete.

## What was built

| Component | Responsibility |
| --- | --- |
| `safety/models.py` | Immutable requests, canonical integer units, SHA-256 digests |
| `safety/authorization.py` | Trusted principal and role separation |
| `safety/approval.py` | Ed25519 tokens, domain-separated, exact claim binding |
| `safety/policy.py` | Pure deterministic decisions on canonical units |
| `safety/admission.py` | Observation quality, vehicle state, envelope, float32 wire checks |
| `safety/monitor.py` | Per-field telemetry freshness, fail-closed |
| `safety/store.py` | Single-owner SQLite ledger, one-use nonces, hash-chained audit |
| `safety/gateway.py` | Orchestration, execution leases, bounded dispatch |
| `safety/runtime.py` | Trusted launch composition; no gateway means no writes |
| `operator_cli.py` | Out-of-band signing at a terminal, never on the server |

1,795 lines of safety code, 744 tests.

### The mechanism

An agent calls `arm`. It receives an approval request: action, canonical
parameters, vehicle, measured home position quantized to millimetres, policy
hash, connection epoch, a single-use nonce, a 60-second window. No prose an
agent could have authored.

A human inspects it with `safemav-operator review`, signs it with
`safemav-operator approve` on a machine that holds the private key, typing the
exact request ID to confirm. The server only ever holds the public half.

The agent resubmits the request ID with the token, unchanged. The gateway
verifies the signature, the binding, the ledger state, current observations and
policy, claims the nonce atomically, then issues an execution lease covering
the specific sequence of MAVSDK calls that action needs.

Each sink re-runs the whole check immediately before its call, with no `await`
in between. Change anything — deadline, telemetry freshness, home epoch,
connection epoch, policy hash, operator key scope, ledger writability — and the
next sink does not fire.

### Three design decisions that carry the result

**Canonical integers everywhere.** Public input becomes integers before
anything else: degrees to 1e-7, metres to millimetres. This removes free text
from the authorization path entirely, makes the signed digest byte-exact
(Python's `==` cannot distinguish `True` from `1`), and makes float32 wire
rounding explicit so a value cannot pass policy in float64 and exceed the
ceiling on the wire.

**Acceptance is not completion.** `ACKNOWLEDGED` means the autopilot accepted a
command. Nothing in this system claims a flight finished; that requires
observing telemetry, which the demos do explicitly.

**Uncertainty latches.** A timeout, cancellation or backend failure records
`UNKNOWN` with partial effects and closes the writer — across process restarts.
No automatic retry, and in particular no automatic disarm: cutting motors in
flight drops the aircraft.

## Evidence

| Claim | Evidence |
| --- | --- |
| Works against a real autopilot | [SITL_EVIDENCE.md](../docs/SITL_EVIDENCE.md): headless PX4, 4 actions observed, 10 refusals |
| Quantified improvement | [BENCHMARK_RESULTS.md](../docs/BENCHMARK_RESULTS.md): 2,340 trials, Wilson intervals, raw data committed |
| Adversarial coverage | [ADVERSARIAL_TESTS.md](../docs/ADVERSARIAL_TESTS.md): T01–T25 mapped to running tests |
| Security properties | [SECURITY_ANALYSIS.md](../docs/SECURITY_ANALYSIS.md): claim-by-claim, with limits |
| Threat model | [THREAT_MODEL.md](../docs/THREAT_MODEL.md): STRIDE, attack tree, G1/G2 |
| One-page walkthrough | <https://hieutachi.github.io/mavlink-mcp/> (source: [site/index.html](../site/index.html)): measured results and an interactive attack replay over the same reason codes, with deep links into every artifact above |
| Reproduction | [INSTALLATION.md](../docs/INSTALLATION.md), [DEMO_GUIDE.md](../docs/DEMO_GUIDE.md) |

**The prevention criterion**, applied uniformly: an action counts as prevented
only if no physical sink was invoked and the simulated vehicle did not change
state. A refusal message with a command already sent is a failure. No LLM
refusal counts as enforcement anywhere; no language model participates in any
decision or in the benchmark.

### Headline numbers

| | Original guardrails | SafeMAV |
| --- | --- | --- |
| Unauthorized execution | 82.6% [79.6–85.3] | 4.3% [3.1–6.1] |
| Unauthorized takeoff | 66.7% [59.5–73.1] | 0% [0–2.1] |
| Replay accepted | 100% | 0% |
| Parameter tampering accepted | 100% | 0% |
| Degraded-telemetry execution | 100% | 0% |
| **False rejection, legitimate ops** | **0%** | **0%** |

Authorization costs 13.4 ms median, dominated by ledger fsync (4.1 ms/event),
not signature verification (48 µs).

## What this does not claim

Stated plainly, because a safety submission that overstates is worse than one
that underclaims.

**Not an OS sandbox.** Enforcement covers the MCP tools and the supported
Python API in this process. Code already running inside the interpreter can
read the gateway's private issuer object and forge a lease. Seven of eight
bypass attempts are blocked; this one is not, and
`test_known_gap_same_process_issuer_theft_is_not_prevented` asserts that it
succeeds. It accounts for the entire residual 4.3%. Closing it needs
process-level isolation that is designed for but not built.

**A signature is key possession, not informed judgement.** Approval fatigue is
a real failure mode and this design does not solve it.

**Freshness is arrival time, not sensor authenticity.** A compromised autopilot
or GPS spoofing produces timely, well-formed, wrong telemetry.

**No aircraft has flown with this code.** PX4 SITL exercises the MAVLink and
MAVSDK path, not airworthiness, radio links or real sensor failure. ArduPilot
is untested.

**The original envelope checks were already sound.** The pre-gateway altitude
ceiling and haversine geofence score identically to the gateway, 0/60 on
envelope violations. That part of the old design did not need replacing, and
the benchmark says so.

## Reproducing in five minutes

```bash
git clone https://github.com/hieutachi/mavlink-mcp.git
cd mavlink-mcp && git checkout safemav/p08-p09-gateway
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
python -m pip install -e ".[dev]"

python -m pytest -q              # 744 passed
python examples/patrol_sim.py    # full approval flow over MCP stdio
python tools/verify_benchmark.py # recompute every rate from raw data
```

The third command recomputes all 18 benchmark rates from the committed raw
trials with an independent oracle and exits non-zero on any disagreement.
`benchmark/verify_baseline.py` likewise checks the reconstructed baseline
against git and fails if it has drifted.

PX4 SITL, if wanted, is in [INSTALLATION.md](../docs/INSTALLATION.md) level C —
headless, no Gazebo, no GPU.
