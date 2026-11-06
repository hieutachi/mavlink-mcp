"""Trusted launch composition with real public keys, SQLite and SimAdapter."""
from __future__ import annotations

import base64
import builtins
import json
from dataclasses import FrozenInstanceError

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mavlink_mcp.adapters.sim_adapter import SimAdapter
from mavlink_mcp.safety import runtime
from mavlink_mcp.safety.models import Environment
from mavlink_mcp.safety.store import SafetyStore


@pytest.fixture
def launch(tmp_path, monkeypatch):
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes_raw()
    (tmp_path / "operator.pub").write_bytes(public)
    data = {
        "actor_id": "flight-agent", "vehicle_id": "sim-vehicle", "gateway_id": "gateway-1",
        "environment": Environment.KINEMATIC.value, "readonly": False,
        "policy": {"readonly": False}, "ledger_path": "ledger.sqlite3",
        "trust_keys": [{"key_id": "operator-key", "operator_id": "alice",
                        "public_key_path": "operator.pub", "roles": ["human_operator"],
                        "vehicles": ["sim-vehicle"], "actions": ["arm", "hold"]}],
    }
    path = tmp_path / "launch.json"

    def save():
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    save()
    monkeypatch.setenv("MAVLINK_MCP_BACKEND", "sim")
    monkeypatch.setenv("SAFEMAV_CONFIG", str(path))
    return data, save, private


@pytest.fixture
def stores(monkeypatch):
    opened = []

    def track_store(*args, **kwargs):
        store = SafetyStore(*args, **kwargs)
        opened.append(store)
        return store

    monkeypatch.setattr(runtime, "SafetyStore", track_store)
    yield opened
    for store in opened:
        store.close()


def test_config_resolves_paths_and_freezes_policy(launch):
    data, save, private = launch
    path = save()
    cfg = runtime.load_runtime_config(path)
    assert cfg.ledger_path == path.parent / "ledger.sqlite3"
    assert cfg.bootstrap is False
    assert cfg.environment is Environment.KINEMATIC
    key = cfg.trust_store.lookup("operator-key")
    assert key.public_key == private.public_key().public_bytes_raw()
    assert key.roles == frozenset({"human_operator"})
    assert key.vehicles == frozenset({"sim-vehicle"})
    assert key.actions == frozenset({"arm", "hold"})
    with pytest.raises(FrozenInstanceError):
        cfg.readonly = True
    with pytest.raises(FrozenInstanceError):
        cfg.policy.config.readonly = True


def test_inline_public_key_matches_operator_cli(launch):
    data, save, private = launch
    key = data["trust_keys"][0]
    key.pop("public_key_path")
    key["public_key"] = base64.urlsafe_b64encode(private.public_key().public_bytes_raw()).decode().rstrip("=")
    cfg = runtime.load_runtime_config(save())
    assert cfg.trust_store.lookup("operator-key").public_key == private.public_key().public_bytes_raw()


def test_pem_public_key_is_accepted(launch):
    _, save, private = launch
    path = save()
    (path.parent / "operator.pub").write_bytes(private.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    assert runtime.load_runtime_config(path).trust_store.lookup("operator-key").public_key == private.public_key().public_bytes_raw()


@pytest.mark.parametrize("patch", [
    {"unexpected": True}, {"actor_id": ""}, {"actor_id": " agent "},
    {"actor_id": 2}, {"vehicle_id": False}, {"gateway_id": "x\x00y"},
    {"readonly": "false"}, {"readonly": 0}, {"bootstrap": "true"}, {"bootstrap": 1},
    {"environment": "mock"}, {"environment": "hardware"}, {"environment": None},
    {"policy": []}, {"policy": {"readonly": 0}},
    {"policy": {"max_altitude_m": True}}, {"policy": {"max_radius_m": "200"}},
    {"policy": {"max_altitude_m": 0}}, {"policy": {"allow_unsigned": True}},
    {"policy": {"environment": "px4_sitl"}}, {"policy": {"environment": "mock"}},
    {"trust_keys": {}}, {"trust_keys": [None]}, {"ledger_path": ""},
    {"ledger_path": None}, {"private_key": "secret"},
])
def test_invalid_config_fails_before_any_store(launch, stores, patch):
    data, save, _ = launch
    data.update(patch)
    save()
    with pytest.raises(runtime.RuntimeConfigError):
        runtime.create_drone()
    assert not stores


@pytest.mark.parametrize("field", [
    "actor_id", "vehicle_id", "gateway_id", "environment", "readonly",
    "policy", "trust_keys", "ledger_path",
])
def test_required_launch_fields_cannot_be_omitted(launch, field):
    data, save, _ = launch
    del data[field]
    with pytest.raises(runtime.RuntimeConfigError):
        runtime.load_runtime_config(save())


@pytest.mark.parametrize("patch", [
    {"private_key": "secret"}, {"key_id": ""}, {"operator_id": False},
    {"roles": []}, {"roles": ["human_operator", "human_operator"]},
    {"roles": ["admin"]}, {"vehicles": []}, {"vehicles": ["*"] , "actions": []},
    {"vehicles": [3]}, {"actions": ["all"]}, {"actions": ["arm", "arm"]},
    {"roles": "human_operator"}, {"revoked": "false"}, {"public_key": "bad"},
])
def test_trust_fields_are_strict(launch, patch):
    data, save, _ = launch
    data["trust_keys"][0].update(patch)
    with pytest.raises(runtime.RuntimeConfigError):
        runtime.load_runtime_config(save())


def test_duplicate_key_identity_is_rejected(launch):
    data, save, _ = launch
    data["trust_keys"].append(dict(data["trust_keys"][0]))
    with pytest.raises(runtime.RuntimeConfigError):
        runtime.load_runtime_config(save())


@pytest.mark.parametrize("encoded", ["", "not-a-key", "A" * 43 + "=", "A" * 42, "A" * 100])
def test_malformed_inline_key_is_rejected(launch, encoded):
    data, save, _ = launch
    key = data["trust_keys"][0]
    key.pop("public_key_path")
    key["public_key"] = encoded
    with pytest.raises(runtime.RuntimeConfigError):
        runtime.load_runtime_config(save())


@pytest.mark.parametrize("kind", ["private_pem", "rsa_public", "oversize", "missing"])
def test_nonpublic_or_invalid_key_file_is_rejected(launch, kind):
    _, save, private = launch
    path = save()
    public_path = path.parent / "operator.pub"
    if kind == "private_pem":
        public_path.write_bytes(private.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    elif kind == "rsa_public":
        from cryptography.hazmat.primitives.asymmetric.rsa import generate_private_key
        public_path.write_bytes(generate_private_key(public_exponent=65537, key_size=2048)
            .public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    elif kind == "oversize":
        public_path.write_bytes(b"x" * (runtime.MAX_PUBLIC_KEY_BYTES + 1))
    else:
        public_path.unlink()
    with pytest.raises(runtime.RuntimeConfigError):
        runtime.load_runtime_config(path)


@pytest.mark.parametrize("raw", [
    b'{"readonly":true,"readonly":false}',
    b'{"policy":{"readonly":true,"readonly":false}}',
    b'{"trust_keys":[{"key_id":"a","key_id":"b"}]}',
    b'{"readonly":NaN}', b'{"readonly":Infinity}', b'[]', b'null',
    b'{', b'\xff', b'[' * 2000 + b']' * 2000,
])
def test_json_parser_fails_closed(launch, raw):
    _, save, _ = launch
    path = save()
    path.write_bytes(raw)
    with pytest.raises(runtime.RuntimeConfigError):
        runtime.load_runtime_config(path)


def test_oversized_config_is_rejected(launch):
    _, save, _ = launch
    path = save()
    path.write_bytes(b" " * (runtime.MAX_CONFIG_BYTES + 1))
    with pytest.raises(runtime.RuntimeConfigError, match="size limit"):
        runtime.load_runtime_config(path)


@pytest.mark.parametrize("value", ["", " ", "nonexistent-config.json"])
def test_explicit_invalid_config_never_defaults_readonly(monkeypatch, value):
    monkeypatch.setenv("MAVLINK_MCP_BACKEND", "sim")
    monkeypatch.setenv("SAFEMAV_CONFIG", value)
    with pytest.raises(runtime.RuntimeConfigError):
        runtime.create_drone()


async def test_absent_config_is_fixed_readonly_despite_legacy_env(monkeypatch, stores):
    monkeypatch.delenv("SAFEMAV_CONFIG", raising=False)
    monkeypatch.setenv("MAVLINK_MCP_BACKEND", "sim")
    monkeypatch.setenv("MAVLINK_MCP_READONLY", "false")
    monkeypatch.setenv("MAVLINK_MCP_MAX_ALTITUDE_M", "0")
    monkeypatch.setenv("MAVLINK_MCP_GEOFENCE_RADIUS_M", "0")
    drone = runtime.create_drone()
    assert drone.guardrails.readonly is True
    assert drone.guardrails.max_altitude_m == 50
    assert drone.guardrails.geofence_radius_m == 200
    result = (await drone.arm(confirm=True)).to_dict()
    assert result["reason_code"] == "READONLY"
    assert result["execution_status"] == "NOT_STARTED"
    assert drone.backend_info["connected"] is False
    assert (await drone.get_telemetry()).armed is False
    assert not stores


@pytest.mark.parametrize("principal,policy", [(True, False), (False, True), (True, True)])
async def test_both_readonly_switches_are_authoritative(launch, stores, monkeypatch, principal, policy):
    data, save, _ = launch
    data.update(readonly=principal, bootstrap=True, policy={"readonly": policy})
    save()
    monkeypatch.setenv("MAVLINK_MCP_READONLY", "false")
    drone = runtime.create_drone()
    assert drone.guardrails.readonly is True
    assert (await drone.hold()).to_dict()["reason_code"] == "READONLY"
    assert not drone.backend_info["connected"]
    assert len(stores) == 1


async def test_missing_policy_readonly_stays_readonly(launch, stores):
    data, save, _ = launch
    data.update(bootstrap=True, policy={})
    save()
    drone = runtime.create_drone()
    assert drone.guardrails.readonly is True
    assert (await drone.arm()).to_dict()["reason_code"] == "READONLY"


async def test_gateway_binds_trusted_identity_policy_and_live_store(launch, stores, monkeypatch):
    data, save, _ = launch
    data.update(bootstrap=True, policy={"readonly": False, "max_altitude_m": 25, "max_radius_m": 100})
    save()
    monkeypatch.setenv("MAVLINK_MCP_MAX_ALTITUDE_M", "0")
    monkeypatch.setenv("MAVLINK_MCP_GEOFENCE_RADIUS_M", "0")
    monkeypatch.setenv("SAFEMAV_RUNTIME_DIR", "unused")
    drone = runtime.create_drone()
    assert drone.guardrails.max_altitude_m == 25
    assert drone.guardrails.geofence_radius_m == 100
    assert drone.backend_info["connected"] is False
    result = (await drone.arm(confirm=True)).to_dict()
    assert result["decision"] == "REQUIRE_APPROVAL"
    request = result["approval_request"]
    assert request["actor_id"] == data["actor_id"]
    assert request["vehicle_id"] == data["vehicle_id"]
    assert request["gateway_id"] == data["gateway_id"]
    assert request["environment"] == Environment.KINEMATIC.value
    assert request["boot_epoch"] == stores[0].boot_epoch
    assert request["policy_hash"] == runtime.load_runtime_config(save()).policy.policy_hash
    assert stores[0].path == save().parent / "ledger.sqlite3"
    # Subsequent environment/config edits do not reconfigure the live runtime.
    data["readonly"] = True
    save()
    monkeypatch.setenv("MAVLINK_MCP_READONLY", "true")
    assert drone.guardrails.readonly is False


@pytest.mark.parametrize("bootstrap", [None, False])
def test_missing_ledger_requires_explicit_bootstrap(launch, stores, bootstrap):
    data, save, _ = launch
    if bootstrap is not None:
        data["bootstrap"] = bootstrap
    path = save()
    with pytest.raises(runtime.RuntimeConfigError, match="unavailable"):
        runtime.create_drone()
    assert not (path.parent / "ledger.sqlite3").exists()
    assert not stores


async def test_explicit_bootstrap_then_established_reopen(launch, stores):
    data, save, _ = launch
    data["bootstrap"] = True
    save()
    first = runtime.create_drone()
    request = (await first.arm()).to_dict()
    epoch = stores[-1].boot_epoch
    stores[-1].close()
    data["bootstrap"] = False
    save()
    second = runtime.create_drone()
    assert stores[-1].boot_epoch != epoch
    assert stores[-1].get_request(request["request_id"])[2] == "EXPIRED"
    assert second.backend_info["connected"] is False


def test_bootstrap_never_repairs_a_corrupt_ledger(launch, stores):
    data, save, _ = launch
    data["bootstrap"] = True
    path = save()
    ledger = path.parent / "ledger.sqlite3"
    ledger.write_bytes(b"not sqlite")
    with pytest.raises(runtime.RuntimeConfigError):
        runtime.create_drone()
    assert ledger.read_bytes() == b"not sqlite"
    assert not stores


def test_failed_gateway_construction_releases_store(launch, stores, monkeypatch):
    data, save, _ = launch
    data["bootstrap"] = True
    path = save()

    def fail(**kwargs):
        raise ValueError("bad binding")

    monkeypatch.setattr(runtime, "SafetyGateway", fail)
    with pytest.raises(runtime.RuntimeConfigError):
        runtime.create_drone()
    # The failed composition must not retain exclusive ledger ownership.
    with SafetyStore(path.parent / "ledger.sqlite3"):
        pass


@pytest.mark.parametrize("backend", ["", "mock", "sim_typo"])
def test_unknown_backend_fails_closed(launch, stores, monkeypatch, backend):
    monkeypatch.setenv("MAVLINK_MCP_BACKEND", backend)
    with pytest.raises(runtime.RuntimeConfigError, match="No fallback"):
        runtime.create_drone()
    assert not stores


def test_backend_environment_mismatch_precedes_mavsdk_import(launch, stores, monkeypatch):
    monkeypatch.setenv("MAVLINK_MCP_BACKEND", "mavsdk")
    original = builtins.__import__

    def no_mavsdk(name, *args, **kwargs):
        if "mavsdk" in name:
            raise AssertionError("must validate before backend import")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_mavsdk)
    with pytest.raises(runtime.RuntimeConfigError, match="environments? differ"):
        runtime.create_drone()
    assert not stores


def test_px4_profile_is_not_accepted_for_real_sim_adapter(launch, stores):
    data, save, _ = launch
    data["environment"] = Environment.PX4_SITL.value
    cfg = runtime.load_runtime_config(save())
    with pytest.raises(runtime.RuntimeConfigError, match="environment differ"):
        runtime.compose_drone(SimAdapter(), cfg)
    assert not stores
