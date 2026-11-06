"""Record real PX4 SITL evidence for the SafeMAV gateway.

Run inside WSL next to a live headless PX4 SITL (see tools/run_sitl.sh). This
connects the actual MavsdkAdapter to the autopilot, so every number below is
measured, not assumed. It exercises the signed-approval path and the deny
matrix, and writes docs/SITL_EVIDENCE.json.

Flight commands are sent only to the simulated vehicle. No hardware.
"""
from __future__ import annotations

import asyncio
import json
import os
import platform
import statistics
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mavlink_mcp.adapters.mavsdk_adapter import MavsdkAdapter
from mavlink_mcp.drone import Drone
from mavlink_mcp.safety.approval import TrustedKey, TrustStore, sign
from mavlink_mcp.safety.authorization import Principal, Role, TrustedContext
from mavlink_mcp.safety.config import load_policy
from mavlink_mcp.safety.gateway import SafetyGateway
from mavlink_mcp.safety.models import PolicyDecision, ReasonCode, request_from_dict
from mavlink_mcp.safety.monitor import MonitorConfig, MonitorState, assess_snapshot
from mavlink_mcp.safety.store import SafetyStore

ADDRESS = os.environ.get("MAVLINK_MCP_URL", "udpin://0.0.0.0:14540")


class Operator:
    """Stands in for safemav-operator with an ephemeral key; evidence only."""

    def __init__(self, store):
        self.store = store
        self.key = Ed25519PrivateKey.generate()

    def trust_store(self):
        return TrustStore({"sitl-key": TrustedKey(
            "sitl-key", "sitl-operator", self.key.public_key().public_bytes_raw(),
            frozenset({"human_operator"}))})

    def sign(self, request_id):
        record = self.store.get_approval_record(request_id)
        request = request_from_dict(self.store.get_request(request_id)[0])
        return sign(request, nonce=record.nonce, key_id="sitl-key",
                    operator_id="sitl-operator", private_key=self.key,
                    issued_at_utc_ms=self.store.clock(),
                    expires_at_utc_ms=record.expires_at_utc_ms).encode()


async def approved(operator, method, *args, **kwargs):
    pending = await method(*args, **kwargs)
    if pending.decision is not PolicyDecision.REQUIRE_APPROVAL:
        return pending
    token = operator.sign(pending.request_id)
    return await method(*args, **kwargs, request_id=pending.request_id,
                        approval_token=token)


async def wait_for(adapter, predicate, timeout, label):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await adapter.get_telemetry()
        observation = adapter.safety_snapshot()
        if observation is not None and predicate(observation.telemetry):
            return observation.telemetry
        await asyncio.sleep(0.25)
    raise TimeoutError(f"SITL never reached: {label}")


async def measure_freshness(adapter, samples=40):
    """Measure actual receipt age and inter-field skew against PX4."""
    ages, skews = [], []
    for _ in range(samples):
        await adapter.get_telemetry()
        observation = adapter.safety_snapshot()
        health = assess_snapshot(
            observation.telemetry, now_monotonic=time.monotonic(),
            connection_epoch=adapter.connection_epoch, home_epoch=adapter.home_epoch,
            connected=observation.connected, monitor_healthy=observation.monitor_healthy,
            expected_source="MAVSDK",
            config=MonitorConfig(max_age_s=5.0, max_skew_s=5.0),
        )
        if health.max_age_s is not None:
            ages.append(health.max_age_s)
        if health.max_skew_s is not None:
            skews.append(health.max_skew_s)
        await asyncio.sleep(0.1)
    def stats(values):
        if not values:
            return None
        return {"samples": len(values), "min_s": round(min(values), 4),
                "median_s": round(statistics.median(values), 4),
                "max_s": round(max(values), 4)}
    return {"receipt_age": stats(ages), "field_skew": stats(skews)}


async def deny_matrix(operator, drone, adapter):
    """Each entry must be refused without any sink reaching the autopilot."""
    results = {}

    async def record(name, coro_factory, expect):
        before = adapter.connection_epoch
        result = await coro_factory()
        results[name] = {
            "decision": result.decision.value,
            "reason_code": result.reason_code.value,
            "execution_status": result.execution_status,
            "expected_reason": expect,
            "matched": result.reason_code.value == expect,
            "connection_stable": adapter.connection_epoch == before,
        }

    await record("no_approval", lambda: drone.arm(confirm=True),
                 ReasonCode.APPROVAL_REQUIRED.value)

    pending = await drone.arm()
    good = operator.sign(pending.request_id)
    tampered = good[:-6] + ("AAAAAA" if good[-6:] != "AAAAAA" else "BBBBBB")
    await record("tampered_signature",
                 lambda: drone.arm(request_id=pending.request_id, approval_token=tampered),
                 ReasonCode.APPROVAL_INVALID.value)
    await record("token_without_request_id",
                 lambda: drone.arm(approval_token=good),
                 ReasonCode.APPROVAL_INVALID.value)
    await record("altitude_above_policy", lambda: drone.takeoff(500.0),
                 ReasonCode.ALTITUDE_LIMIT.value)
    # Grounded navigation is refused by the state check before geofence is
    # reached; the geofence itself is exercised in flight (airborne_deny).
    await record("goto_while_grounded", lambda: drone.goto(0.0, 0.0, 10.0),
                 ReasonCode.UNSAFE_STATE.value)
    await record("nonfinite_altitude", lambda: drone.takeoff(float("nan")),
                 ReasonCode.INVALID_ARGUMENT.value)
    await record("excess_precision", lambda: drone.takeoff(10.00000001),
                 ReasonCode.INVALID_ARGUMENT.value)
    return results


async def airborne_deny(drone, adapter):
    """Checks that only make sense while the vehicle is actually flying."""
    results = {}
    for name, factory, expect in (
        ("outside_geofence", lambda: drone.goto(47.5100, 8.5456, 10.0),
         ReasonCode.OUTSIDE_GEOFENCE.value),
        ("altitude_above_policy_in_flight", lambda: drone.goto(47.3978, 8.5456, 100.0),
         ReasonCode.ALTITUDE_LIMIT.value),
        ("mission_waypoint_outside_geofence",
         lambda: drone.fly_mission([
             {"latitude_deg": 47.3978, "longitude_deg": 8.5456, "relative_altitude_m": 10.0},
             {"latitude_deg": 47.5100, "longitude_deg": 8.5456, "relative_altitude_m": 10.0},
         ]),
         ReasonCode.OUTSIDE_GEOFENCE.value),
    ):
        result = await factory()
        results[name] = {
            "decision": result.decision.value,
            "reason_code": result.reason_code.value,
            "execution_status": result.execution_status,
            "expected_reason": expect,
            "matched": result.reason_code.value == expect,
        }
    return results


async def main():
    evidence = {
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "platform": f"{platform.system()} {platform.release()} / {platform.machine()}",
        "python": sys.version.split()[0],
        "autopilot": "PX4 SITL, built-in SIH dynamics, headless (no Gazebo)",
        "address": ADDRESS,
        "hardware_contacted": False,
    }
    from importlib.metadata import version
    evidence["mavsdk_version"] = version("mavsdk")

    adapter = MavsdkAdapter(system_address=ADDRESS)
    store_dir = Path(tempfile.mkdtemp())
    store = SafetyStore(store_dir / "ledger.sqlite3", bootstrap=True)
    operator = Operator(store)
    # Measured with tools/measure_rates.py against this PX4 SITL build:
    # position/attitude arrive at ~50 Hz, but battery and home are 1 Hz with a
    # worst observed age of 1.97 s. Skew mixes those streams, so both bounds
    # must exceed 2 s or healthy telemetry is wrongly rejected as stale.
    policy = load_policy({"environment": "px4_sitl", "readonly": False,
                          "max_altitude_m": 30, "max_radius_m": 200,
                          "max_age_ms": 3000, "max_skew_ms": 3000})
    context = TrustedContext(
        Principal("sitl-agent", Role.FLIGHT_AGENT, "sitl-vehicle", False),
        "px4_sitl", "sitl-gateway", store.boot_epoch)
    gateway = SafetyGateway(store=store, policy=policy,
                            approval_store=operator.trust_store(),
                            adapter=adapter, context=context, dispatch_timeout=20.0)
    drone = Drone(adapter, safety_gateway=gateway)

    try:
        started = time.monotonic()
        await adapter.connect()
        evidence["connect_seconds"] = round(time.monotonic() - started, 2)
        print(f"connected in {evidence['connect_seconds']}s")

        ready = await wait_for(
            adapter,
            lambda t: t.position is not None and t.gps is not None
            and t.gps.num_satellites >= 10 and t.flight_mode is not None,
            120, "GPS fix and telemetry")
        evidence["initial_telemetry"] = {
            "flight_mode": ready.flight_mode, "armed": ready.armed,
            "in_air": ready.in_air, "satellites": ready.gps.num_satellites,
            "gps_fix": ready.gps.fix_type,
            "battery_percent": ready.battery.remaining_percent if ready.battery else None,
        }
        evidence["home_anchor"] = dict(adapter.safety_snapshot().home_anchor)
        print("telemetry:", evidence["initial_telemetry"])

        evidence["policy"] = {
            "max_altitude_m": policy.config.max_altitude_m,
            "max_radius_m": policy.config.max_radius_m,
            "max_age_ms": policy.config.max_age_ms,
            "max_skew_ms": policy.config.max_skew_ms,
            "note": "age/skew bounds derived from tools/measure_rates.py, not assumed",
        }
        evidence["freshness"] = await measure_freshness(adapter)
        print("freshness:", evidence["freshness"])

        evidence["deny_matrix"] = await deny_matrix(operator, drone, adapter)
        print("deny matrix:",
              {k: v["matched"] for k, v in evidence["deny_matrix"].items()})

        flight = []
        armed = await approved(operator, drone.arm)
        flight.append({"action": "arm", "status": armed.execution_status,
                       "reason": armed.reason_code.value})
        print("arm ->", armed.execution_status, armed.reason_code.value)
        if armed.execution_status == "ACKNOWLEDGED":
            state = await wait_for(adapter, lambda t: t.armed is True, 20, "armed")
            flight[-1]["observed_armed"] = state.armed

            takeoff = await approved(operator, drone.takeoff, 10.0)
            flight.append({"action": "takeoff", "status": takeoff.execution_status,
                           "reason": takeoff.reason_code.value})
            print("takeoff ->", takeoff.execution_status, takeoff.reason_code.value)
            if takeoff.execution_status == "ACKNOWLEDGED":
                state = await wait_for(
                    adapter,
                    lambda t: t.in_air is True and t.position.relative_altitude_m > 8.0,
                    90, "airborne at 10 m")
                flight[-1]["observed_altitude_m"] = round(
                    state.position.relative_altitude_m, 2)
                print("altitude:", flight[-1]["observed_altitude_m"])

                await wait_for(adapter, lambda t: t.flight_mode in {"HOLD", "POSCTL"},
                               60, "HOLD after takeoff")
                evidence["airborne_deny"] = await airborne_deny(drone, adapter)
                print("airborne deny:",
                      {k: v["matched"] for k, v in evidence["airborne_deny"].items()})

                goto = await approved(operator, drone.goto, 47.3980, 8.5460, 10.0, 0.0)
                flight.append({"action": "goto", "status": goto.execution_status,
                               "reason": goto.reason_code.value})
                print("goto ->", goto.execution_status, goto.reason_code.value)
                if goto.execution_status == "ACKNOWLEDGED":
                    moved = await wait_for(
                        adapter,
                        lambda t: abs(t.position.latitude_deg - 47.3980) < 0.0002,
                        120, "reached goto target")
                    flight[-1]["observed_latitude"] = round(moved.position.latitude_deg, 6)
                    await wait_for(adapter, lambda t: t.flight_mode in {"HOLD", "POSCTL"},
                                   90, "HOLD after goto")
                rtl = await approved(operator, drone.return_to_launch)
                flight.append({"action": "return_to_launch",
                               "status": rtl.execution_status,
                               "reason": rtl.reason_code.value})
                print("rtl ->", rtl.execution_status, rtl.reason_code.value)
                if rtl.execution_status == "ACKNOWLEDGED":
                    landed = await wait_for(adapter, lambda t: t.in_air is False,
                                            180, "landed")
                    flight[-1]["observed_in_air"] = landed.in_air
        evidence["flight"] = flight

        audit = store.export_audit()
        evidence["audit"] = {
            "event_count": len(audit),
            "kinds": [event["kind"] for event in audit],
            "hash_chain_valid": store.verify_audit(audit),
        }
        evidence["ok"] = (
            all(e["matched"] for e in evidence["deny_matrix"].values())
            and all(e["matched"] for e in evidence.get("airborne_deny", {}).values())
            and evidence["audit"]["hash_chain_valid"]
            and [step["status"] for step in flight] == ["ACKNOWLEDGED"] * len(flight)
            and len(flight) == 4
        )
    finally:
        try:
            await adapter.disconnect()
        finally:
            store.close()

    output = Path(__file__).resolve().parents[1] / "docs" / "SITL_EVIDENCE.json"
    output.write_text(json.dumps(evidence, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(evidence, indent=2, sort_keys=True))
    return 0 if evidence.get("ok") else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
