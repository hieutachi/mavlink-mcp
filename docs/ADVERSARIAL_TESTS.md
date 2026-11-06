# Adversarial test coverage (T01–T25)

Maps the master scenario list to tests that actually run. Reproduce all of it
with:

```bash
.venv/Scripts/python.exe -m pytest -q
```

744 tests pass: commit `7055a49` plus the injection, sensor-precondition and conflict suites. No test touches
hardware or opens a network connection.

**Prevention criterion.** A scenario counts as blocked only when no physical
sink was invoked on the adapter and the simulated vehicle did not change state.
Tests assert on `adapter.sinks`, not on return values or exception text. A
refusal message with a sink already fired would fail.

## Coverage

| ID | Scenario | Test | Result |
| --- | --- | --- | --- |
| T01 | Unauthorized arm | `test_sink_matrix.py::test_no_gateway_and_unsigned_requests_never_reach_sink` | blocked |
| T02 | Unauthorized takeoff | same, parameterized over all eight actions | blocked |
| T03 | `confirm=true` without approval | `test_server_tools.py::test_confirm_alone_never_authorizes_and_token_alone_is_rejected` | blocked |
| T04 | Reuse a consumed approval | `test_gateway.py::test_submit_issues_live_bound_request_and_dispatches_once` (`APPROVAL_REPLAY`) | blocked |
| T05 | Expired approval | `test_clock_window.py::test_expired_window_is_still_refused` | blocked |
| T06 | Altitude changed after approval | `test_gateway.py::test_changed_parameters_and_action_never_reach_sink` | blocked |
| T07 | Vehicle identity changed after approval | `test_injection_and_conflict.py::test_vehicle_identity_change_invalidates_an_approval`, `::test_approval_is_not_transferable_between_gateways` | blocked |
| T08 | Waypoint coordinates changed after approval | `test_approval_integrity.py::test_signature_parameter_and_context_mutation_rejected` | blocked |
| T09 | Mission digest mismatch | `test_approval_integrity.py::test_altered_request_with_copied_digest_is_rejected`, `test_models.py::test_unknown_fields_precision_and_digest_order` | blocked |
| T10 | Geofence violation | `test_sink_matrix.py::test_unsafe_state_and_limits_never_reach_sink`; also on live PX4 SITL | blocked |
| T11 | Altitude ceiling violation | same, plus `test_wire_types.py` for float32 boundary behavior | blocked |
| T12 | Malformed tool arguments | `test_gateway.py::test_strict_input_is_rejected_before_connect`, `test_injection_and_conflict.py::test_token_parsing_rejects_hostile_encodings`, `test_server_tools.py::test_fly_mission_strict_input_never_reaches_adapter` | blocked |
| T13 | Prompt injection in mission metadata | `test_injection_and_conflict.py::test_injected_text_cannot_enter_a_canonical_parameter`, `::test_injected_text_in_a_tool_argument_never_reaches_a_sink`, `::test_injected_text_in_identity_fields_is_refused` | blocked |
| T14 | Prompt injection via telemetry text | `test_injection_and_conflict.py::test_injected_text_in_telemetry_cannot_authorize`, `::test_telemetry_text_is_never_copied_into_the_audit_trail` | blocked |
| T15 | Conflicting instructions | `test_injection_and_conflict.py::test_conflicting_concurrent_commands_never_interleave`, `::test_conflicting_navigation_order_expires_once_state_changes` | serialized |
| T16 | Stale telemetry | `test_telemetry_freshness.py`, `test_gateway_outcomes.py::test_second_sink_rechecks_all_authority[stale]` | blocked |
| T17 | GPS quality degradation | `test_sensor_preconditions.py::test_degraded_gps_fix_blocks_arming`, `::test_too_few_satellites_blocks_arming`, `::test_missing_gps_or_battery_blocks_a_physical_vehicle` | blocked |
| T18 | MAVLink connection loss | `test_connection_lifecycle.py`, `test_p09_mavsdk.py::test_any_dead_or_ended_subscription_fails_closed_with_fresh_receipts`, `test_gateway_outcomes.py::...[disconnect]`, `test_sensor_preconditions.py::test_disconnected_vehicle_blocks_every_action` | blocked |
| T19 | Concurrent conflicting operations | `test_gateway_outcomes.py::test_concurrent_token_reuse_dispatches_once`, `test_injection_and_conflict.py::test_conflicting_concurrent_commands_never_interleave` | one dispatch |
| T20 | Bypass MCP, call Python directly | `test_bypass_attempts.py` (8 tests) | 7 blocked, 1 known gap |
| T21 | Approval timeout during execution | `test_gateway_outcomes.py::test_second_sink_rechecks_all_authority[expiry]`, `[monotonic]` | second sink blocked |
| T22 | Invalid policy configuration | `test_config.py::test_invalid_policy_is_rejected_without_fallback`, `test_runtime.py` | fails closed |
| T23 | Safety monitor failure | `test_sensor_preconditions.py::test_unhealthy_monitor_blocks_every_action`, `::test_impossible_battery_voltage_is_not_treated_as_healthy`, `test_gateway_outcomes.py::...[audit]` | blocked |
| T24 | Unexpected autopilot mode | `test_injection_and_conflict.py::test_injected_text_in_telemetry_cannot_authorize`; navigation requires `HOLD`/`POSCTL` | blocked |
| T25 | Emergency action while airborne | `test_sensor_preconditions.py::test_airborne_disarm_is_refused_but_land_and_rtl_are_not`, `::test_airborne_disarm_stays_refused_even_with_a_signed_approval` | refused |

## Positive controls

Denying everything would pass most of the table above, so the suite also
requires legitimate operations to succeed with real signed approvals:

- `test_sink_matrix.py::test_signed_action_reaches_exact_sink_sequence` — all
  eight actions reach their exact sinks in order, including both sinks of
  takeoff and mission.
- `test_sim_adapter.py` — 15 tests flying the kinematic simulator: climb over
  time, goto, mission progress with per-waypoint speed, RTL returning home,
  1000 identical waypoints.
- `test_stdio_sim.py` — a real MCP subprocess over stdio completing eight
  approved actions with a 37-event verified audit chain.
- `tools/sitl_evidence.py` — arm, takeoff, goto and RTL against live PX4 SITL
  with observed telemetry. See [SITL_EVIDENCE.md](SITL_EVIDENCE.md).

## Notes on three scenarios where the honest answer is not "blocked"

**T15 and T19 (conflicting and concurrent).** Land, hold and RTL are
deliberately *not* gated on flight mode: de-escalation must stay cheap. Two
approved recovery actions issued together therefore both execute — but
strictly one after another. The test records sink entry and exit and requires
the pattern `enter, exit, enter, exit`, so no two commands are ever in flight
at the vehicle simultaneously. Navigation is different: a `goto` approved while
airborne is refused once the vehicle has landed.

**T20 (direct Python bypass).** Seven of eight attempts are blocked: a second
facade over a bound adapter, a direct adapter call, a self-minted lease, a
lease bound to another vehicle, a lease without the gateway recheck hook, a
second gateway claiming the same adapter, and a readonly principal. The eighth
is not blocked and is asserted as such in
`test_known_gap_same_process_issuer_theft_is_not_prevented`: code already
running inside the interpreter can read the gateway's private issuer object and
forge a valid lease. That is the G1/G2 boundary described in
[THREAT_MODEL.md](THREAT_MODEL.md) section 8b. Closing it requires OS-level
isolation, which is not implemented. The forged action leaves no ledger entry,
so it is detectable after the fact but not prevented.

**T25 (emergency while airborne).** Disarming an airborne multirotor is not a
safe emergency response: it drops the aircraft. The gateway refuses it, and
refuses it even when a human has signed the approval and the vehicle lifted off
in between — a signature cannot override flight dynamics. Hold and RTL, the two
responses a vehicle can survive, stay available. Low battery does not revoke
recovery either; it is exactly when recovery is needed.

**T13 and T14 (prompt injection).** There is no natural-language field in the
authorization path to inject into. Parameters are canonical integers
(degrees×10⁷, millimetres, millidegrees) and identities are compared byte-wise,
so the eight payloads tested — instruction override, SQL, script tags, a forged
`SYSTEM:` prefix, NUL and ANSI escapes, path traversal, template syntax and a
right-to-left override — are rejected as malformed input rather than
interpreted. Flight mode is the only string observation and is checked against
an allow-list. This is a structural property, not a filter, and the tests exist
so it stays that way.
