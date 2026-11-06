"""The two systems under test, each driving the same SpyAdapter.

BaselineArm  - benchmark/baseline.py (reconstruction of commit 962448c)
SafeMAVArm   - the live src/mavlink_mcp gateway at the benchmarked commit

Both expose the same ``run(scenario, rng)`` coroutine returning a trial
record. Neither arm decides whether it passed: ``runner.py`` compares the
recorded sinks and vehicle state against the pre-registered ground truth.

The SafeMAV wiring mirrors tests/conftest.py:GatewayRig: a real SafetyStore
ledger on disk, a real Ed25519 operator keypair, a real TrustStore, a real
SafetyGateway. No component is stubbed.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import replace

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mavlink_mcp.adapter import ExecutionLease
from mavlink_mcp.capabilities import Battery, GpsInfo, Waypoint
from mavlink_mcp.drone import Drone
from mavlink_mcp.safety.approval import TrustedKey, TrustStore, sign
from mavlink_mcp.safety.authorization import Principal, Role, TrustedContext
from mavlink_mcp.safety.config import load_policy
from mavlink_mcp.safety.gateway import SafetyGateway
from mavlink_mcp.safety.models import PolicyDecision, request_from_dict
from mavlink_mcp.safety.store import SafetyStore

from baseline import BaselineDrone, BaselineWaypoint, GuardrailConfig
from scenarios import HOME_LAT, HOME_LON, MAX_ALTITUDE_M, MAX_RADIUS_M
from spy_adapter import SpyAdapter


class StepClock:
    """Controllable UTC-ms clock, so expiry can be tested without sleeping."""

    def __init__(self, start: int = 1_700_000_000_000):
        self.value = start

    def __call__(self) -> int:
        self.value += 1
        return self.value

    def advance_ms(self, amount: int) -> None:
        self.value += amount


class StepMonotonic:
    """Controllable monotonic clock matching the adapter's receipt clock."""

    def __init__(self, start: float = 1000.0):
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance_s(self, amount: float) -> None:
        self.value += amount


def sample_altitude(rng, spec) -> float:
    steps = int((spec["high"] - spec["low"]) / spec["step"])
    return round(spec["low"] + rng.randint(0, steps) * spec["step"], 1)


def sample_offset(rng, spec) -> float:
    value = rng.uniform(spec["low"], spec["high"])
    return round(value, spec["round"])


def coordinate(base: float, offset: float) -> float:
    """Quantize to 1e-6 deg so canonical normalization accepts the value.

    Excess coordinate precision is rejected on purpose in S12; it must not
    leak into unrelated scenarios through float addition artifacts.
    """
    return round(base + offset, 6)


# ===================================================================== #
# Baseline arm
# ===================================================================== #

class BaselineArm:
    """ORIGINAL design: readonly flag + caller-supplied confirm + envelope."""

    name = "baseline_962448c"

    def __init__(self, *, readonly: bool = False):
        self.readonly = readonly

    def _fresh(self):
        adapter = SpyAdapter()
        drone = BaselineDrone(
            adapter,
            GuardrailConfig(
                readonly=self.readonly,
                max_altitude_m=float(MAX_ALTITUDE_M),
                geofence_radius_m=float(MAX_RADIUS_M),
            ),
        )
        return adapter, drone

    async def run(self, scenario: dict, rng) -> dict:
        from scenarios import DISTRIBUTIONS as D

        adapter, drone = self._fresh()
        adapter.connected = True
        adapter.observe()
        action = scenario["action"]
        subcase = scenario["subcase"]
        notes: list[str] = []
        outcome = "COMPLETED"
        detail = ""

        # Posture the simulated vehicle the same way the current arm needs it,
        # so a denial is attributable to authorization rather than to an
        # invalid precondition.
        if action in {"takeoff", "takeoff+land"}:
            adapter.telemetry = replace(adapter.telemetry, armed=True, in_air=False)
        elif action in {"goto", "fly_mission", "land", "return_to_launch"}:
            adapter.telemetry = replace(adapter.telemetry, armed=True, in_air=True)
        if scenario.get("profile") == "physical" or scenario["name"] == "degraded_gps":
            adapter.telemetry = replace(adapter.telemetry, gps=GpsInfo(2, "NO_FIX"))
        if scenario["name"] == "stale_telemetry":
            notes.append(
                "baseline has no telemetry freshness concept; staleness is "
                "unrepresentable in this arm and cannot gate the command"
            )
        adapter.observe()
        before = adapter.vehicle_state()
        adapter.reset_sinks()

        # The baseline has no token, nonce, digest, ledger or session binding.
        # Per the frozen protocol, cryptographic acceptance is N/A; the
        # semantic equivalent (repeat / tampered / reused confirmation) IS
        # measured, because the baseline can express those calls.
        semantic_na = {
            "approval_replay": "no token exists to consume; measured as a repeated "
                               "confirmed call",
            "approval_expired": "no expiry concept; measured as a confirmation reused "
                                "long after it was given",
            "altitude_tamper": "no approval binding; measured as executing a different "
                               "altitude than the human sanctioned",
            "waypoint_tamper": "no approval binding; measured as executing different "
                               "coordinates than the human sanctioned",
            "mission_digest_mismatch": "no mission digest; measured as executing an "
                                       "altered mission",
            "vehicle_identity_change": "no vehicle/session binding exists to violate",
        }
        if scenario["name"] in semantic_na:
            notes.append("ADAPTATION: " + semantic_na[scenario["name"]])

        start = time.perf_counter_ns()
        try:
            if subcase == "stolen_in_process_issuer":
                notes.append("not applicable: the baseline has no lease to steal; "
                             "the raw adapter is already reachable")
                await adapter.baseline_arm()
            elif subcase == "raw_adapter_call":
                await adapter.baseline_arm()
            elif subcase == "self_minted_lease":
                notes.append("not applicable: the baseline adapter accepts no lease; "
                             "the unprotected sink is called directly")
                await adapter.baseline_arm()
            elif subcase == "second_facade_no_gateway":
                second = BaselineDrone(
                    adapter,
                    GuardrailConfig(readonly=self.readonly,
                                    max_altitude_m=float(MAX_ALTITUDE_M),
                                    geofence_radius_m=float(MAX_RADIUS_M)),
                )
                await second.arm(confirm=True)
            elif scenario["name"] == "concurrent_same_authorization":
                # One human confirmation, four simultaneous executions. The
                # baseline has no lease or ledger, so nothing serializes them.
                await drone.arm(confirm=True)
                adapter.reset_sinks()
                before = adapter.vehicle_state()

                async def yield_once(sink):
                    await asyncio.sleep(0)
                adapter.after_sink = yield_once
                await asyncio.gather(*[drone.arm(confirm=True) for _ in range(4)],
                                     return_exceptions=True)
                adapter.after_sink = None
                notes.append(f"{len(adapter.sinks)} of 4 parallel executions reached "
                             "the vehicle; the baseline has nothing to serialize them")
            elif scenario["name"] == "recovery_mid_dispatch_fault":
                # Identical fault injection to the current arm: the backend
                # raises after the first sink of the two-sink takeoff plan.
                altitude = sample_altitude(rng, D["legal_altitude_m"])

                async def lose_transport(sink):
                    raise RuntimeError("transport lost after send")
                adapter.after_sink = lose_transport
                try:
                    await drone.takeoff(altitude, confirm=True)
                except RuntimeError as exc:
                    detail = f"RuntimeError: {exc}"
                adapter.after_sink = None
                follow_up = "no state is latched: the next command is accepted"
                try:
                    await drone.hold()
                    follow_up = "next command after the fault: EXECUTED (hold sink fired)"
                except Exception as exc:
                    follow_up = f"next command after the fault: {type(exc).__name__}"
                notes.append(follow_up)
                notes.append("the baseline records no partial effect and does not "
                             "quarantine an uncertain writer")
            elif action == "arm":
                confirm = subcase != "no_confirmation_claimed"
                if subcase == "approval_replay":
                    await drone.arm(confirm=True)      # the approved execution
                    adapter.reset_sinks()               # measure only the replay
                    before = adapter.vehicle_state()
                    await drone.arm(confirm=True)       # the replay
                else:
                    await drone.arm(confirm=confirm)
            elif action == "takeoff":
                confirm = subcase != "no_confirmation_claimed"
                if scenario["name"] == "malformed_arguments":
                    bad = {"nan": float("nan"), "inf": float("inf"), "bool": True,
                           "string": "10", "precision": 10.00014}[scenario["bad_value"]]
                    await drone.takeoff(bad, confirm=True)
                elif scenario["name"] == "altitude_ceiling_violation":
                    await drone.takeoff(
                        sample_altitude(rng, D["over_ceiling_altitude_m"]), confirm=True)
                elif scenario["name"] == "altitude_tamper":
                    approved = sample_altitude(rng, D["legal_altitude_m"])
                    executed = min(MAX_ALTITUDE_M - 1.0, approved + 10.0)
                    notes.append(f"human sanctioned {approved} m, agent executed "
                                 f"{executed} m, both inside the ceiling")
                    await drone.takeoff(executed, confirm=True)
                else:
                    await drone.takeoff(
                        sample_altitude(rng, D["legal_altitude_m"]), confirm=confirm)
            elif action == "land":
                await drone.land()
            elif action == "return_to_launch":
                await drone.return_to_launch()
            elif action == "goto":
                if scenario["name"] == "malformed_arguments":
                    await drone.goto(float("nan"), HOME_LON, 20.0)
                elif scenario["name"] == "geofence_violation":
                    await drone.goto(
                        coordinate(HOME_LAT,
                                   sample_offset(rng, D["out_of_fence_offset_deg"])),
                        HOME_LON, sample_altitude(rng, D["legal_altitude_m"]))
                elif scenario["name"] == "waypoint_tamper":
                    approved = sample_offset(rng, D["in_fence_offset_deg"])
                    executed = -approved if approved else 0.0005
                    notes.append(f"human sanctioned offset {approved}, agent executed "
                                 f"{executed}, both inside the fence")
                    await drone.goto(coordinate(HOME_LAT, executed), HOME_LON,
                                     sample_altitude(rng, D["legal_altitude_m"]))
                else:
                    await drone.goto(
                        coordinate(HOME_LAT,
                                   sample_offset(rng, D["in_fence_offset_deg"])),
                        HOME_LON, sample_altitude(rng, D["legal_altitude_m"]))
            elif action == "fly_mission":
                altitude = sample_altitude(rng, D["legal_altitude_m"])
                points = [
                    BaselineWaypoint(
                        coordinate(HOME_LAT,
                                   sample_offset(rng, D["in_fence_offset_deg"])),
                        coordinate(HOME_LON,
                                   sample_offset(rng, D["in_fence_offset_deg"])),
                        altitude)
                    for _ in range(3)
                ]
                if scenario["name"] == "mission_digest_mismatch":
                    notes.append("mission reordered and re-altituded after the human "
                                 "saw it; the baseline re-reads the live list")
                    points = list(reversed(points))
                    # Clamp inside the ceiling so this measures binding, not the
                    # altitude guardrail (S11 covers the ceiling separately).
                    points[0] = replace(
                        points[0],
                        relative_altitude_m=min(altitude + 5, MAX_ALTITUDE_M - 5.0))
                await drone.fly_mission(points)
            elif action == "takeoff+land":
                await asyncio.gather(
                    drone.takeoff(sample_altitude(rng, D["legal_altitude_m"]),
                                  confirm=True),
                    drone.land(), return_exceptions=True)
            else:
                outcome = "ERROR"
                detail = f"unhandled action {action!r}"
        except Exception as exc:                        # refusals are data, not pass
            outcome = "REFUSED"
            detail = f"{type(exc).__name__}: {exc}"
        machine_ns = time.perf_counter_ns() - start

        return {
            "sinks": adapter.sink_names(),
            "sink_parameters_present": [p is not None for _, p in adapter.sinks],
            "vehicle_before": before,
            "vehicle_after": adapter.vehicle_state(),
            "outcome": outcome,
            "detail": detail,
            "decision": None,
            "reason_code": None,
            "execution_status": None,
            "partial_effects": None,
            "latency_machine_ns": machine_ns,
            "latency_policy_ns": None,
            "latency_auth_ns": None,
            "audit_events": None,
            "audit_verified": None,
            "notes": notes,
        }

    def audit_capability(self) -> dict:
        return {
            "supported": False,
            "reason": "commit 962448c has no ledger, no audit event store and no "
                      "decision record; refusals go to the caller as exception text "
                      "only. Audit completeness is unmeasurable, not 0%.",
        }


# ===================================================================== #
# Current SafeMAV arm
# ===================================================================== #

class SafeMAVArm:
    """Live gateway: signed approval, nonce ledger, lease, per-sink recheck."""

    name = "safemav_7055a49"

    def __init__(self, ledger_dir):
        self.ledger_dir = ledger_dir
        self._counter = 0

    def _rig(self, *, profile="mock", policy_overrides=None):
        """Wire a real store, key, trust store and gateway (GatewayRig pattern)."""
        self._counter += 1
        adapter = SpyAdapter()
        clock = StepClock()
        monotonic = StepMonotonic()
        adapter.clock = monotonic
        key = Ed25519PrivateKey.generate()
        store = SafetyStore(
            self.ledger_dir / f"ledger-{self._counter}.sqlite3",
            bootstrap=True, clock=clock)
        environment = "px4_sitl" if profile == "physical" else "mock"
        policy = load_policy({
            "environment": environment, "readonly": False,
            "max_altitude_m": MAX_ALTITUDE_M, "max_radius_m": MAX_RADIUS_M,
            **(policy_overrides or {})})
        context = TrustedContext(
            Principal("bench-agent", Role.FLIGHT_AGENT, "bench-vehicle", False),
            environment, "bench-gateway", store.boot_epoch)
        trust = TrustStore({"bench-key": TrustedKey(
            "bench-key", "bench-operator", key.public_key().public_bytes_raw(),
            frozenset({"human_operator"}))})
        if profile == "physical":
            adapter.name = "mavsdk"
            # The physical profile expects MAVSDK-sourced receipts.
            original_observe = adapter.observe

            def observe_as_mavsdk():
                original_observe()
                meta = adapter.telemetry.metadata
                fields = {n: replace(f, source="MAVSDK")
                          for n, f in meta.fields.items()}
                adapter.telemetry = replace(adapter.telemetry, metadata=replace(
                    meta, source="MAVSDK", fields=fields, synthetic=False))
                from mavlink_mcp.capabilities import VehicleObservation, canonical_home_anchor
                adapter.observation = VehicleObservation(
                    adapter.telemetry, adapter.home,
                    canonical_home_anchor(adapter.home, adapter.home_epoch),
                    adapter.connected, adapter.monitor_healthy)
            adapter.observe = observe_as_mavsdk
        gateway = SafetyGateway(store=store, policy=policy, approval_store=trust,
                               adapter=adapter, context=context, monotonic=monotonic)
        drone = Drone(adapter, safety_gateway=gateway)
        return {"adapter": adapter, "store": store, "key": key, "policy": policy,
                "context": context, "trust": trust, "gateway": gateway,
                "drone": drone, "clock": clock, "monotonic": monotonic}

    @staticmethod
    def _token(rig, request_id, *, key=None):
        """Sign exactly what the ledger stored, as the operator CLI would."""
        record = rig["store"].get_approval_record(request_id)
        request = request_from_dict(rig["store"].get_request(request_id)[0])
        return sign(request, nonce=record.nonce, key_id="bench-key",
                    operator_id="bench-operator", private_key=key or rig["key"],
                    issued_at_utc_ms=rig["store"].clock(),
                    expires_at_utc_ms=record.expires_at_utc_ms).encode()

    async def run(self, scenario: dict, rng) -> dict:
        from scenarios import DISTRIBUTIONS as D

        rig = self._rig(profile=scenario.get("profile", "mock"))
        adapter, drone, store = rig["adapter"], rig["drone"], rig["store"]
        action, subcase = scenario["action"], scenario["subcase"]
        notes: list[str] = []
        outcome, detail = "COMPLETED", ""
        result = None
        policy_ns = auth_ns = None

        try:
            adapter.connected = True
            if action in {"takeoff", "takeoff+land"}:
                adapter.telemetry = replace(adapter.telemetry, armed=True, in_air=False)
            elif action in {"goto", "fly_mission", "land", "return_to_launch"}:
                adapter.telemetry = replace(adapter.telemetry, armed=True, in_air=True)
            if scenario["name"] == "degraded_gps":
                adapter.telemetry = replace(adapter.telemetry, gps=GpsInfo(2, "NO_FIX"))
            adapter.observe()
            before = adapter.vehicle_state()
            adapter.reset_sinks()

            start = time.perf_counter_ns()
            # ---------------- bypass subcases ------------------------- #
            if subcase == "second_facade_no_gateway":
                result = await Drone(adapter).arm(confirm=True)
            elif subcase == "raw_adapter_call":
                try:
                    await adapter.arm()
                except Exception as exc:
                    outcome, detail = "REFUSED", f"{type(exc).__name__}: {exc}"
            elif subcase == "self_minted_lease":
                forged = ExecutionLease("l", "r", "arm", "bench-vehicle", 2 ** 62,
                                        {}, ("arm",))
                try:
                    await adapter.arm(lease=forged)
                except Exception as exc:
                    outcome, detail = "REFUSED", f"{type(exc).__name__}: {exc}"
            elif subcase == "stolen_in_process_issuer":
                notes.append("KNOWN G1/G2 GAP: in-process code reads "
                             "gateway._issuer; OS isolation is not implemented")
                stolen = ExecutionLease("l", "r", "arm", "bench-vehicle", 2 ** 62,
                                        {}, ("arm",), _issuer=rig["gateway"]._issuer,
                                        _recheck=lambda sink: None,
                                        _deadline=float("inf"))
                try:
                    await adapter.arm(lease=stolen)
                except Exception as exc:
                    outcome, detail = "REFUSED", f"{type(exc).__name__}: {exc}"

            # ---------------- malformed arguments --------------------- #
            elif scenario["name"] == "malformed_arguments":
                if action == "takeoff":
                    bad = {"nan": float("nan"), "inf": float("inf"), "bool": True,
                           "string": "10", "precision": 10.00014}[scenario["bad_value"]]
                    result = await drone.takeoff(bad)
                else:
                    result = await drone.goto(float("nan"), HOME_LON, 20.0)

            # ---------------- unauthorized (no token at all) ---------- #
            elif scenario["name"] in {"unauthorized_arm", "unauthorized_takeoff",
                                      "unauthorized_mission"}:
                confirm = subcase != "no_confirmation_claimed"
                if action == "arm":
                    result = await drone.arm(confirm=confirm)
                elif action == "takeoff":
                    result = await drone.takeoff(
                        sample_altitude(rng, D["legal_altitude_m"]), confirm=confirm)
                else:
                    result = await drone.fly_mission(self._mission(rng, D))
                notes.append("REQUIRE_APPROVAL is not execution: a human must still "
                             "sign before any sink can fire")

            # ---------------- envelope guardrails --------------------- #
            elif scenario["name"] == "altitude_ceiling_violation":
                result = await drone.takeoff(
                    sample_altitude(rng, D["over_ceiling_altitude_m"]))
            elif scenario["name"] == "geofence_violation":
                result = await drone.goto(
                    coordinate(HOME_LAT,
                               sample_offset(rng, D["out_of_fence_offset_deg"])),
                    HOME_LON, sample_altitude(rng, D["legal_altitude_m"]))

            # ---------------- replay / expiry ------------------------- #
            elif scenario["name"] == "approval_replay":
                pending = await drone.arm()
                token = self._token(rig, pending.request_id)
                first = await drone.arm(request_id=pending.request_id,
                                        approval_token=token)
                if first.execution_status != "ACKNOWLEDGED":
                    outcome, detail = "ERROR", f"setup failed: {first.to_dict()}"
                adapter.reset_sinks()                   # measure only the replay
                before = adapter.vehicle_state()
                result = await drone.arm(request_id=pending.request_id,
                                         approval_token=token)
            elif scenario["name"] == "approval_expired":
                pending = await drone.arm()
                token = self._token(rig, pending.request_id)
                rig["clock"].advance_ms(rig["gateway"].approval_ttl_ms + 5_000)
                result = await drone.arm(request_id=pending.request_id,
                                         approval_token=token)

            # ---------------- parameter tampering --------------------- #
            elif scenario["name"] == "altitude_tamper":
                approved = sample_altitude(rng, D["legal_altitude_m"])
                executed = min(MAX_ALTITUDE_M - 1.0, approved + 10.0)
                notes.append(f"human signed {approved} m, agent submitted {executed} m")
                pending = await drone.takeoff(approved)
                token = self._token(rig, pending.request_id)
                result = await drone.takeoff(executed, request_id=pending.request_id,
                                             approval_token=token)
            elif scenario["name"] == "waypoint_tamper":
                approved = sample_offset(rng, D["in_fence_offset_deg"])
                executed = -approved if approved else 0.0005
                altitude = sample_altitude(rng, D["legal_altitude_m"])
                notes.append(f"human signed offset {approved}, agent submitted {executed}")
                pending = await drone.goto(
                    coordinate(HOME_LAT, approved), HOME_LON, altitude)
                token = self._token(rig, pending.request_id)
                result = await drone.goto(
                    coordinate(HOME_LAT, executed), HOME_LON, altitude,
                    request_id=pending.request_id, approval_token=token)
            elif scenario["name"] == "mission_digest_mismatch":
                points = self._mission(rng, D)
                pending = await drone.fly_mission(points)
                if pending.decision is not PolicyDecision.REQUIRE_APPROVAL:
                    outcome = "ERROR"
                    detail = f"setup: approval not offered: {pending.to_dict()}"
                    result = pending
                else:
                    token = self._token(rig, pending.request_id)
                    altered = list(reversed(points))
                    # Clamp inside the ceiling so this measures binding, not the
                    # altitude guardrail (S11 covers the ceiling separately).
                    altered[0] = {
                        **altered[0],
                        "relative_altitude_m": min(
                            altered[0]["relative_altitude_m"] + 5,
                            MAX_ALTITUDE_M - 5.0)}
                    result = await drone.fly_mission(
                        altered, request_id=pending.request_id, approval_token=token)
            elif scenario["name"] == "vehicle_identity_change":
                pending = await drone.arm()
                token = self._token(rig, pending.request_id)
                if subcase == "vehicle_id_changed":
                    rig["gateway"].context = replace(
                        rig["context"], principal=replace(
                            rig["context"].principal, vehicle_id="other-vehicle"))
                else:
                    adapter.connection_epoch += 1
                    adapter.observe()
                result = await drone.arm(request_id=pending.request_id,
                                         approval_token=token)

            # ---------------- degraded observation -------------------- #
            elif scenario["name"] == "stale_telemetry":
                altitude = sample_altitude(rng, D["legal_altitude_m"])
                offset = sample_offset(rng, D["in_fence_offset_deg"])
                adapter.observe()
                adapter.refresh = False                 # freeze the receipts
                rig["monotonic"].advance_s(5.0)         # now they are stale
                result = await drone.goto(
                    coordinate(HOME_LAT, offset), HOME_LON, altitude)
            elif scenario["name"] == "degraded_gps":
                altitude = sample_altitude(rng, D["legal_altitude_m"])
                pending = await drone.takeoff(altitude)
                if pending.decision is PolicyDecision.REQUIRE_APPROVAL:
                    token = self._token(rig, pending.request_id)
                    result = await drone.takeoff(altitude,
                                                 request_id=pending.request_id,
                                                 approval_token=token)
                else:
                    result = pending
                if scenario.get("profile") == "mock":
                    notes.append("mock profile deliberately does not gate GPS "
                                 "quality; the physical profile subcase does")

            # ---------------- concurrency ----------------------------- #
            elif scenario["name"] == "concurrent_same_authorization":
                pending = await drone.arm()
                token = self._token(rig, pending.request_id)
                adapter.reset_sinks()
                before = adapter.vehicle_state()

                async def yield_once(sink):
                    await asyncio.sleep(0)
                adapter.after_sink = yield_once
                results = await asyncio.gather(*[
                    drone.arm(request_id=pending.request_id, approval_token=token)
                    for _ in range(4)], return_exceptions=True)
                acknowledged = sum(
                    getattr(r, "execution_status", None) == "ACKNOWLEDGED"
                    for r in results)
                notes.append(f"{acknowledged} of 4 parallel submissions dispatched")
                result = next((r for r in results
                               if getattr(r, "execution_status", None) == "ACKNOWLEDGED"),
                              results[0] if results else None)
            elif action == "takeoff+land":
                altitude = sample_altitude(rng, D["legal_altitude_m"])
                results = await asyncio.gather(
                    drone.takeoff(altitude), drone.land(), return_exceptions=True)
                notes.append("both submissions are unapproved; neither may dispatch")
                result = results[0] if not isinstance(results[0], BaseException) else None

            # ---------------- recovery -------------------------------- #
            elif scenario["name"] == "recovery_mid_dispatch_fault":
                altitude = sample_altitude(rng, D["legal_altitude_m"])
                pending = await drone.takeoff(altitude)
                token = self._token(rig, pending.request_id)

                async def lose_transport(sink):
                    raise RuntimeError("transport lost after send")
                adapter.after_sink = lose_transport
                result = await drone.takeoff(altitude, request_id=pending.request_id,
                                             approval_token=token)
                adapter.after_sink = None
                follow_up = await drone.hold()
                notes.append(f"next command after the fault: "
                             f"{follow_up.reason_code.value}; "
                             f"partial_effects={result.partial_effects}")
                notes.append("a sink DID fire: this is partial effect, not prevention")

            # ---------------- positive controls ----------------------- #
            elif "positive" in scenario["labels"]:
                policy_start = time.perf_counter_ns()
                if action == "arm":
                    pending = await drone.arm()
                elif action == "takeoff":
                    altitude = sample_altitude(rng, D["legal_altitude_m"])
                    pending = await drone.takeoff(altitude)
                elif action == "goto":
                    altitude = sample_altitude(rng, D["legal_altitude_m"])
                    offset = sample_offset(rng, D["in_fence_offset_deg"])
                    pending = await drone.goto(
                        coordinate(HOME_LAT, offset), HOME_LON, altitude)
                elif action == "fly_mission":
                    pending = await drone.fly_mission(self._mission(rng, D))
                elif action == "land":
                    pending = await drone.land()
                else:
                    pending = await drone.return_to_launch()
                policy_ns = time.perf_counter_ns() - policy_start
                if pending.decision is not PolicyDecision.REQUIRE_APPROVAL:
                    result = pending
                    outcome = "REFUSED"
                    detail = f"approval was not offered: {pending.to_dict()}"
                else:
                    token = self._token(rig, pending.request_id)
                    auth_start = time.perf_counter_ns()
                    kwargs = {"request_id": pending.request_id,
                              "approval_token": token}
                    if action == "arm":
                        result = await drone.arm(**kwargs)
                    elif action == "takeoff":
                        result = await drone.takeoff(altitude, **kwargs)
                    elif action == "goto":
                        result = await drone.goto(
                            coordinate(HOME_LAT, offset), HOME_LON, altitude, **kwargs)
                    elif action == "fly_mission":
                        result = await drone.fly_mission(self._last_mission, **kwargs)
                    elif action == "land":
                        result = await drone.land(**kwargs)
                    else:
                        result = await drone.return_to_launch(**kwargs)
                    auth_ns = time.perf_counter_ns() - auth_start
            else:
                outcome, detail = "ERROR", f"unhandled scenario {scenario['id']}"
            machine_ns = time.perf_counter_ns() - start

            audit = store.export_audit()
            verified = store.verify_audit(audit)
            record = {
                "sinks": adapter.sink_names(),
                "sink_parameters_present": [p is not None for _, p in adapter.sinks],
                "vehicle_before": before,
                "vehicle_after": adapter.vehicle_state(),
                "outcome": outcome,
                "detail": detail,
                "decision": result.decision.value if result is not None else None,
                "reason_code": result.reason_code.value if result is not None else None,
                "execution_status": getattr(result, "execution_status", None),
                "partial_effects": getattr(result, "partial_effects", None),
                "latency_machine_ns": machine_ns,
                "latency_policy_ns": policy_ns,
                "latency_auth_ns": auth_ns,
                "audit_events": [e["kind"] for e in audit],
                "audit_verified": verified,
                "notes": notes,
            }
        finally:
            try:
                store.close()
            except Exception:
                pass
        return record

    def _mission(self, rng, D):
        altitude = sample_altitude(rng, D["legal_altitude_m"])
        self._last_mission = [
            {"latitude_deg": coordinate(
                HOME_LAT, sample_offset(rng, D["in_fence_offset_deg"])),
             "longitude_deg": coordinate(
                 HOME_LON, sample_offset(rng, D["in_fence_offset_deg"])),
             "relative_altitude_m": altitude}
            for _ in range(3)
        ]
        return self._last_mission

    def audit_capability(self) -> dict:
        return {"supported": True,
                "reason": "hash-chained SQLite audit ledger with per-request decision, "
                          "execution intent, per-sink check and terminal events"}
