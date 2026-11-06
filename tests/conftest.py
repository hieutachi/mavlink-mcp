"""Shared fixtures: a mock adapter so no SITL is needed to run tests."""

from __future__ import annotations

import pytest
import time
from dataclasses import replace
from mavlink_mcp.capabilities import TelemetryField, TelemetryMetadata, VehicleObservation, canonical_home_anchor
from mavlink_mcp.safety.monitor import valid_observation_value
from mavlink_mcp.safety.approval import sign, TrustStore, TrustedKey
from mavlink_mcp.safety.authorization import Principal, Role, TrustedContext
from mavlink_mcp.safety.config import load_policy
from mavlink_mcp.safety.gateway import SafetyGateway
from mavlink_mcp.safety.models import PolicyDecision, request_from_dict
from mavlink_mcp.safety.store import SafetyStore
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mavlink_mcp.adapter import DroneAdapter
from mavlink_mcp.capabilities import (
    Attitude,
    Battery,
    Capability,
    GpsInfo,
    Position,
    TelemetrySnapshot,
    Waypoint,
)
from mavlink_mcp.drone import Drone
from mavlink_mcp.guardrails import GuardrailConfig

HOME = Position(
    latitude_deg=47.397742,
    longitude_deg=8.545594,
    absolute_altitude_m=488.0,
    relative_altitude_m=0.0,
)


class MockAdapter(DroneAdapter):
    """In-memory DroneAdapter that records every call."""

    name = "mock"
    # Even test adapters use the protected path; calls record actual sinks.
    protected = True

    def __init__(
        self,
        capabilities: frozenset[Capability] | None = None,
        home: Position | None = HOME,
    ) -> None:
        self._capabilities = (
            capabilities
            if capabilities is not None
            else frozenset(Capability)
        )
        self.home = home
        self.clock = time.monotonic
        self.connection_epoch = 1
        self.home_epoch = 1
        self.monitor_healthy = True
        self.refresh = True
        self.sinks = []
        self.after_sink = None
        self.observation = None
        self.connected = False
        self.calls: list[tuple] = []
        self.telemetry = TelemetrySnapshot(
            position=Position(47.397742, 8.545594, 488.0, 0.0),
            attitude=Attitude(0.1, -0.2, 90.0),
            battery=Battery(12.6, 100.0),
            gps=GpsInfo(10, "FIX_3D"),
            flight_mode="HOLD",
            armed=False,
            in_air=False,
        )

    def capabilities(self) -> frozenset[Capability]:
        return self._capabilities

    def is_connected(self) -> bool:
        return self.connected

    async def connect(self) -> None:
        self.connected = True
        self.calls.append(("connect",))

    async def disconnect(self) -> None:
        self.connected = False
        self.calls.append(("disconnect",))

    async def get_telemetry(self) -> TelemetrySnapshot:
        self.calls.append(("get_telemetry",))
        if self.refresh:
            self.observe()
        return self.telemetry

    def observe(self):
        now = self.clock()
        values = {name: getattr(self.telemetry, name) for name in
                  ("position", "attitude", "battery", "gps", "flight_mode", "armed", "in_air")}
        values["home"] = self.home
        source = "MAVSDK" if self.name == "mavsdk" else "SYNTHETIC"
        fields = {name: TelemetryField(now, source, self.connection_epoch,
                    valid_observation_value(name, value), home_epoch=self.home_epoch if name == "home" else None)
                  for name, value in values.items() if value is not None}
        self.telemetry = replace(self.telemetry, metadata=TelemetryMetadata(
            now, source, self.connection_epoch, self.home_epoch, fields, source == "SYNTHETIC"))
        self.observation = VehicleObservation(self.telemetry, self.home,
            canonical_home_anchor(self.home, self.home_epoch) if self.home else None,
            self.connected, self.monitor_healthy)

    def safety_snapshot(self):
        return self.observation

    async def _write(self, lease, sink, parameters=None):
        self._consume_lease(lease, sink, parameters)
        self.sinks.append((sink, parameters))
        if self.after_sink is not None:
            await self.after_sink(sink)

    async def get_flight_mode(self) -> str:
        self.calls.append(("get_flight_mode",))
        return self.telemetry.flight_mode or "UNKNOWN"

    async def get_home(self) -> Position | None:
        self.calls.append(("get_home",))
        return self.home

    async def arm(self, *, lease=None) -> None:
        await self._write(lease, "arm")
        self.calls.append(("arm", lease))
        self.telemetry = replace(self.telemetry, armed=True)

    async def disarm(self, *, lease=None) -> None:
        await self._write(lease, "disarm")
        self.calls.append(("disarm", lease))
        self.telemetry = replace(self.telemetry, armed=False)

    async def takeoff(self, altitude_m: float, *, lease=None) -> None:
        await self._write(lease, "set_takeoff_altitude", {"altitude_m": altitude_m})
        await self._write(lease, "takeoff")
        self.calls.append(("takeoff", altitude_m, lease))
        self.telemetry = replace(self.telemetry, in_air=True)

    async def land(self, *, lease=None) -> None:
        await self._write(lease, "land")
        self.calls.append(("land", lease))
        self.telemetry = replace(self.telemetry, in_air=False)

    async def hold(self, *, lease=None) -> None:
        await self._write(lease, "hold")
        self.calls.append(("hold", lease))

    async def return_to_launch(self, *, lease=None) -> None:
        await self._write(lease, "return_to_launch")
        self.calls.append(("return_to_launch", lease))

    async def goto(self, waypoint: Waypoint, yaw_deg: float = 0.0, *, lease=None, absolute_altitude_m=None) -> None:
        await self._write(lease, "goto_location", {"waypoint": waypoint, "yaw_deg": yaw_deg, "absolute_altitude_m": absolute_altitude_m})
        self.calls.append(("goto", waypoint, yaw_deg, lease, absolute_altitude_m))

    async def fly_mission(self, waypoints: list[Waypoint], *, lease=None) -> None:
        await self._write(lease, "upload_mission", {"waypoints": tuple(waypoints)})
        await self._write(lease, "start_mission")
        self.calls.append(("fly_mission", tuple(waypoints), lease))

    def call_names(self) -> list[str]:
        return [call[0] for call in self.calls]


PERMISSIVE = GuardrailConfig(
    readonly=False, max_altitude_m=100.0, geofence_radius_m=500.0
)


@pytest.fixture
def adapter() -> MockAdapter:
    return MockAdapter()


class GatewayRig:
    """Test-only operator key and real ledger/gateway; never used by production."""
    def __init__(self, path, adapter=None, *, clock=None, monotonic=None, policy=None, **gateway_options):
        self.adapter = adapter if adapter is not None else MockAdapter()
        self.key = Ed25519PrivateKey.generate()
        self.store = SafetyStore(path, bootstrap=True, clock=clock)
        environment = {"mock": "mock", "sim": "kinematic", "mavsdk": "px4_sitl"}[self.adapter.name]
        self.policy = load_policy({"environment": environment, "readonly": False,
                                   "max_altitude_m": 100, "max_radius_m": 500, **(policy or {})})
        self.context = TrustedContext(Principal("test-agent", Role.FLIGHT_AGENT, "test-vehicle", False),
                                      environment, "test-gateway", self.store.boot_epoch)
        trust = TrustStore({"test-key": TrustedKey("test-key", "test-operator", self.key.public_key().public_bytes_raw(),
                                                 frozenset({"human_operator"}))})
        options = {"monotonic": monotonic} if monotonic is not None else {}
        self.gateway = SafetyGateway(store=self.store, policy=self.policy, approval_store=trust,
            adapter=self.adapter, context=self.context, **options, **gateway_options)
        self.drone = Drone(self.adapter, safety_gateway=self.gateway)

    def token(self, pending):
        assert pending.decision is PolicyDecision.REQUIRE_APPROVAL, pending.to_dict()
        return self.token_for(pending.request_id)

    def token_for(self, request_id):
        """Sign exactly what the ledger stored, as a real operator CLI would."""
        record = self.store.get_approval_record(request_id)
        request = request_from_dict(self.store.get_request(request_id)[0])
        return sign(request, nonce=record.nonce, key_id="test-key", operator_id="test-operator", private_key=self.key,
                    issued_at_utc_ms=self.store.clock(), expires_at_utc_ms=record.expires_at_utc_ms).encode()

    async def run(self, method, *args, **kwargs):
        pending = await method(*args, **kwargs)
        result = await method(*args, **kwargs, request_id=pending.request_id, approval_token=self.token(pending))
        assert result.execution_status == "ACKNOWLEDGED", result.to_dict()
        return result


@pytest.fixture
def rig_factory(tmp_path):
    rigs = []
    def create(adapter=None, **kwargs):
        rig = GatewayRig(tmp_path / f"ledger-{len(rigs)}.sqlite3", adapter, **kwargs)
        rigs.append(rig)
        return rig
    yield create
    for rig in rigs:
        rig.store.close()


@pytest.fixture
def rig(adapter, rig_factory):
    return rig_factory(adapter)


@pytest.fixture
def drone(rig) -> Drone:
    return rig.drone


@pytest.fixture
def readonly_drone(adapter: MockAdapter) -> Drone:
    """A Drone with default guardrails (readonly)."""
    return Drone(adapter, guardrails=GuardrailConfig())
