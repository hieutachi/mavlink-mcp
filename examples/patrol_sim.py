"""Bounded end-to-end MCP demonstration with signed operator approvals.

Run with this repository's Python environment:
    python examples/patrol_sim.py

It launches its own stdio MCP child against the network-free kinematic
simulator, generates a throwaway operator key, and signs each dangerous action
the way a human operator would with `safemav-operator approve`.

The key here is ephemeral and the "operator" is this script, so this run proves
the enforcement path works. It is not evidence that a human consented, and the
kinematic simulator is not evidence of flight safety. For real autopilot
evidence see docs/SITL_EVIDENCE.md.
"""

import asyncio
import json
import math
import os
import sys
import tempfile
import time
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastmcp import Client

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mavlink_mcp.safety.approval import sign
from mavlink_mcp.safety.models import Action, Environment, FrozenRequest


def write_launch_config(root: Path, public_key: bytes) -> Path:
    """Trusted launch config: public operator key only, never the private key."""
    import base64

    config = {
        "actor_id": "demo-agent",
        "vehicle_id": "demo-vehicle",
        "gateway_id": "demo-gateway",
        "environment": "kinematic",
        "readonly": False,
        "bootstrap": True,
        "ledger_path": "ledger.sqlite3",
        "policy": {
            "readonly": False,
            "max_altitude_m": 50,
            "max_radius_m": 200,
            "min_battery_percent": 30,
        },
        "trust_keys": [{
            "key_id": "demo-key",
            "operator_id": "demo-operator",
            "roles": ["human_operator"],
            "vehicles": ["demo-vehicle"],
            "actions": [action.value for action in Action],
            "public_key": base64.urlsafe_b64encode(public_key).decode().rstrip("="),
        }],
    }
    path = root / "safemav.json"
    path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return path


def server_config(config_path: Path):
    return {"mcpServers": {"sim": {
        "command": sys.executable,
        "args": ["-B", "-u", "-m", "mavlink_mcp"],
        "env": {
            "MAVLINK_MCP_BACKEND": "sim",
            "SAFEMAV_CONFIG": str(config_path),
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
        },
    }}}


def sign_request(private_key, approval_request) -> str:
    """Rebuild the exact immutable request the gateway stored, then sign it."""
    data = dict(approval_request)
    nonce = data.pop("nonce")
    issued = data.pop("issued_at_utc_ms")
    expires = data.pop("expires_at_utc_ms")
    data["action"] = Action(data["action"])
    data["environment"] = Environment(data["environment"])
    request = FrozenRequest(**data)
    return sign(request, nonce=nonce, key_id="demo-key", operator_id="demo-operator",
                private_key=private_key, issued_at_utc_ms=issued,
                expires_at_utc_ms=expires).encode()


async def run():
    trace = []
    private_key = Ed25519PrivateKey.generate()
    # The MCP child owns the ledger and its lock file; on Windows those cannot
    # be removed until it exits, so clean up explicitly after the client closes.
    workdir = tempfile.mkdtemp(prefix="safemav-demo-")
    try:
        root = Path(workdir)
        config_path = write_launch_config(
            root, private_key.public_key().public_bytes_raw())

        async with Client(server_config(config_path)) as client:
            tools = await client.list_tools()
            assert len(tools) == 12, len(tools)

            async def call(name, args=None, *, error=False):
                result = (await client.call_tool(name, args or {})).data
                assert isinstance(result, dict), (name, result)
                assert ("error" in result) == error, (name, result)
                trace.append({"tool": name, "result": result})
                return result

            async def approved(name, args=None):
                """Propose, sign out of band, resubmit the exact request."""
                pending = await call(name, args)
                assert pending["decision"] == "REQUIRE_APPROVAL", pending
                assert pending["execution_status"] == "NOT_STARTED", pending
                token = sign_request(private_key, pending["approval_request"])
                result = await call(name, {**(args or {}),
                                           "request_id": pending["request_id"],
                                           "approval_token": token})
                assert result["execution_status"] == "ACKNOWLEDGED", result
                return result

            async def wait_state(predicate, timeout=8.0):
                deadline = time.monotonic() + timeout
                while True:
                    state = await call("get_telemetry")
                    if predicate(state):
                        return state
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Synthetic flight did not reach the state")
                    await asyncio.sleep(0.1)

            caps = await call("get_capabilities")
            assert caps["adapter"] == "sim"
            assert caps["simulation"]["network_enabled"] is False
            initial = await call("get_telemetry")
            assert not initial["armed"] and initial["battery"] is None
            connection = await call("get_connection_status")
            assert connection["connected"] and "system_address" not in connection

            # An unsigned call yields an approval request, never an action.
            unsigned = await call("arm", {"confirm": True})
            assert unsigned["decision"] == "REQUIRE_APPROVAL", unsigned
            # A forged token is refused.
            await call("arm", {"request_id": unsigned["request_id"],
                               "approval_token": "e30.AAAA"}, error=True)
            # Policy limits hold even with a willing operator.
            await call("takeoff", {"altitude_m": 500}, error=True)

            await approved("arm")
            await approved("takeoff", {"altitude_m": 1.0})
            await wait_state(lambda s: s["flight_mode"] == "HOLD" and s["in_air"])

            home = initial["simulation"]["home"]
            lat = round(home["latitude_deg"], 7)
            lon = round(home["longitude_deg"], 7)
            north_10m = round(math.degrees(10 / 6_371_000), 7)

            # Outside the 200 m fence.
            await call("goto_location", {"latitude_deg": round(lat + 1, 7),
                                         "longitude_deg": lon,
                                         "relative_altitude_m": 1}, error=True)
            await approved("goto_location", {
                "latitude_deg": round(lat + north_10m, 7), "longitude_deg": lon,
                "relative_altitude_m": 1, "yaw_deg": 45})
            moving = await wait_state(
                lambda s: s["simulation"]["local_position_enu_m"][1] > 0.2)
            assert moving["in_air"]

            await approved("hold")
            # The simulator integrates motion on its own clock, so a mode change
            # becomes visible on the next sample, not on acknowledgement.
            await wait_state(lambda s: s["flight_mode"] == "HOLD")
            assert (await call("get_flight_mode"))["flight_mode"] == "HOLD"
            await approved("fly_mission", {"waypoints": [
                {"latitude_deg": round(lat + north_10m / 5, 7), "longitude_deg": lon,
                 "relative_altitude_m": 1, "speed_m_s": 2},
                {"latitude_deg": round(lat + north_10m / 10, 7), "longitude_deg": lon,
                 "relative_altitude_m": 1, "speed_m_s": 2},
            ]})
            completed = await wait_state(
                lambda s: s["simulation"]["mission"]["state"] == "completed")
            assert completed["simulation"]["mission"]["completed_waypoints"] == 2

            await approved("return_to_launch")
            landed = await wait_state(lambda s: not s["in_air"] and not s["armed"],
                                      timeout=20.0)
            assert abs(landed["position"]["latitude_deg"] - lat) < 1e-6
            await approved("land")  # Idempotent on the ground.
            await approved("disarm")
            assert {event["tool"] for event in trace} == {t.name for t in tools}

        import shutil as _shutil
        from mavlink_mcp.safety.store import SafetyStore
        # Single-owner locking is deliberate, and the MCP child keeps the ledger
        # until its process exits. Audit the chain on a copy instead of taking
        # ownership away from a live writer.
        audit_copy = root / "audit-copy.sqlite3"
        _shutil.copyfile(root / "ledger.sqlite3", audit_copy)
        with SafetyStore(audit_copy) as store:
            audit = store.export_audit()
            assert store.verify_audit(audit), "audit hash chain is broken"
            audit_summary = {"event_count": len(audit),
                             "hash_chain_valid": True,
                             "acknowledged": sum(
                                 1 for e in audit if e["kind"] == "REQUEST_ACKNOWLEDGED")}
    finally:
        import shutil
        shutil.rmtree(workdir, ignore_errors=True)

    return {"passed": True, "backend": "sim", "tool_count": 12,
            "hardware_contacted": False, "approvals": "ephemeral test key",
            "audit": audit_summary, "trace": trace}


if __name__ == "__main__":
    print(json.dumps(asyncio.run(run()), indent=2))
