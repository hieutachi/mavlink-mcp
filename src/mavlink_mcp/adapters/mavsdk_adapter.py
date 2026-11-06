"""MAVSDK backend: subscription-only observations and gateway-leased sinks.

Local receipt freshness is not sensor authenticity. No telemetry query opens a
stream, resets a timestamp, changes a rate, or retries an actuator command.
"""
from __future__ import annotations

import asyncio
import inspect
import os
import time
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Callable

from mavsdk import System
from mavsdk.mission import MissionItem, MissionPlan

from mavlink_mcp.adapter import AdapterError, DroneAdapter, ExecutionLease, LeaseError
from mavlink_mcp.capabilities import (
    Attitude, Battery, Capability, GpsInfo, MissionObservation, Position,
    TelemetryField, TelemetryMetadata, TelemetrySnapshot, VehicleObservation,
    Waypoint, canonical_home_anchor,
)
from mavlink_mcp.safety.monitor import valid_observation_value

_DEFAULT_CONNECT_TIMEOUT_S = 30.0
_DEFAULT_MISSION_SPEED_M_S = 5.0
_CLEANUP_TIMEOUT_S = 1.0
_MISSION_OBSERVATION_TIMEOUT_S = 1.0
_TELEMETRY_NAMES = ("position", "attitude", "battery", "gps", "flight_mode", "armed", "in_air")


def default_system_address() -> str:
    """Select the installed MAVSDK version's endpoint syntax."""
    try:
        major = int(version("mavsdk").split(".")[0])
    except (PackageNotFoundError, ValueError):
        major = 3
    return "udpin://0.0.0.0:14540" if major >= 3 else "udp://:14540"


class MavsdkAdapter(DroneAdapter):
    """One vehicle, with a persistent link watcher and generation-bound cache."""

    name = "mavsdk"
    protected = True
    _CAPABILITIES = frozenset(Capability)

    def __init__(
        self,
        system_address: str | None = None,
        connect_timeout_s: float = _DEFAULT_CONNECT_TIMEOUT_S,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._system_address = (
            system_address or os.environ.get("MAVLINK_MCP_URL", "").strip()
            or default_system_address()
        )
        self._connect_timeout_s = connect_timeout_s
        self._clock = clock
        self._lifecycle_lock = asyncio.Lock()
        self._discovered = asyncio.Event()
        self._system: System | None = None
        self._connected = False
        self._generation = 0
        self._connection_epoch = 0
        self._home_epoch = 0
        self._home_signature: tuple[int, int, int] | None = None
        self._home_anchor = None
        self._telemetry_cache: dict[str, Any] = {}
        self._telemetry_fields: dict[str, TelemetryField] = {}
        self._mission_observation = MissionObservation()
        self._subscription_tasks: set[asyncio.Task[Any]] = set()
        self._retired_tasks: set[asyncio.Task[Any]] = set()
        self._connection_task: asyncio.Task[Any] | None = None
        self._subscription_error: str | None = None

    @property
    def system_address(self) -> str:
        return self._system_address

    def capabilities(self) -> frozenset[Capability]:
        return self._CAPABILITIES

    def is_connected(self) -> bool:
        return self._connected

    @property
    def connection_epoch(self) -> int:
        return self._connection_epoch

    @property
    def home_epoch(self) -> int:
        return self._home_epoch

    def _clear_cache(self) -> None:
        self._telemetry_cache.clear()
        self._telemetry_fields.clear()
        self._home_signature = None
        self._home_anchor = None
        self._mission_observation = MissionObservation()

    def _current(self, system: System, generation: int, epoch: int | None = None) -> bool:
        return (self._system is system and self._generation == generation
                and (epoch is None or (self._connected and self._connection_epoch == epoch)))

    def _retire(self, task: asyncio.Task[Any]) -> None:
        """Keep cancellation-resistant tasks referenced and retrieve exceptions."""
        self._retired_tasks.add(task)

        def finished(done: asyncio.Task[Any]) -> None:
            self._retired_tasks.discard(done)
            if not done.cancelled():
                done.exception()

        task.add_done_callback(finished)

    async def _bounded(self, awaitable: Any, timeout: float) -> Any:
        # wait_for can exceed its deadline while waiting for cancellation.
        task = asyncio.ensure_future(awaitable)
        self._retire(task)
        try:
            done, _ = await asyncio.wait({task}, timeout=timeout)
            if not done:
                raise asyncio.TimeoutError
            return task.result()
        finally:
            if not task.done():
                task.cancel()

    async def connect(self) -> None:
        async with self._lifecycle_lock:
            if self._connected:
                return
            try:
                # A dropped link keeps the same watcher. Never reconnect the
                # System underneath an active stream or resend physical writes.
                if self._connection_task is None or self._connection_task.done():
                    await self._shutdown()
                    system = System()
                    self._system = system
                    generation = self._generation

                    async def initialize() -> None:
                        await system.connect(system_address=self._system_address)
                        if not self._current(system, generation):
                            await self._close_system(system)
                            return
                        self._connection_task = asyncio.create_task(self._watch_connection(system, generation))
                        await self._discovered.wait()

                    await self._bounded(initialize(), self._connect_timeout_s)
                else:
                    await self._bounded(self._discovered.wait(), self._connect_timeout_s)
                if not self._connected:
                    raise AdapterError(self._subscription_error or "connection stream ended before discovery")
            except BaseException as exc:
                await self._shutdown()
                if isinstance(exc, asyncio.CancelledError):
                    raise
                if isinstance(exc, asyncio.TimeoutError):
                    raise AdapterError(
                        f"No vehicle discovered on {self._system_address} within {self._connect_timeout_s}s"
                    ) from None
                if not isinstance(exc, Exception):
                    raise
                raise AdapterError(f"Failed to connect to {self._system_address}: {exc}") from exc

    async def _watch_connection(self, system: System, generation: int) -> None:
        stream = None
        try:
            stream = system.core.connection_state()
            async for state in stream:
                if not self._current(system, generation):
                    return
                if type(state.is_connected) is not bool:
                    raise ValueError("invalid connection state")
                if state.is_connected and not self._connected:
                    self._clear_cache()
                    self._connection_epoch += 1
                    self._home_epoch += 1
                    self._subscription_error = None
                    self._connected = True
                    self._start_subscriptions(system, generation, self._connection_epoch)
                    self._discovered.set()
                elif not state.is_connected and self._connected:
                    # Invalidate BEFORE awaiting cancellation so late callbacks
                    # cannot repopulate the cache after a disconnect.
                    self._connected = False
                    self._discovered.clear()
                    self._clear_cache()
                    await self._stop_subscriptions()
            if self._current(system, generation):
                self._subscription_error = "connection_state: stream ended"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._current(system, generation):
                self._subscription_error = f"connection_state: {exc}"
        finally:
            if self._current(system, generation):
                self._connected = False
                self._clear_cache()
                self._discovered.set()  # Wake a pending connect, which fails closed.
                await self._stop_subscriptions()
            await self._close_stream(stream)

    def _start_subscriptions(self, system: System, generation: int, epoch: int) -> None:
        # Resolve factories inside each task so a missing/broken API is health
        # failure rather than a partially started connection with no watcher.
        for name, api in {
            "position": "position", "attitude": "attitude_euler", "battery": "battery",
            "gps": "gps_info", "flight_mode": "flight_mode", "armed": "armed",
            "in_air": "in_air", "home": "home",
        }.items():
            task = asyncio.create_task(self._consume_stream(system, generation, epoch, name, api))
            self._subscription_tasks.add(task)
            task.add_done_callback(self._subscription_tasks.discard)
        task = asyncio.create_task(self._consume_mission(system, generation, epoch))
        self._subscription_tasks.add(task)
        task.add_done_callback(self._subscription_tasks.discard)

    def _field(self, epoch: int, *, valid: bool = True, home: bool = False) -> TelemetryField:
        return TelemetryField(self._clock(), "MAVSDK", epoch, valid,
                              home_epoch=self._home_epoch if home else None)

    def _cache_value(self, name: str, value: Any, epoch: int) -> None:
        if name in {"home", "position"}:
            value = _to_position(value)
        elif name == "attitude":
            value = Attitude(value.roll_deg, value.pitch_deg, value.yaw_deg)
        elif name == "battery":
            value = Battery(value.voltage_v, value.remaining_percent)
        elif name == "gps":
            value = GpsInfo(value.num_satellites, _enum_name(value.fix_type))
        elif name == "flight_mode":
            value = _enum_name(value) if value is not None else None
        valid = valid_observation_value(name, value)
        if name == "home":
            anchor = canonical_home_anchor(value, self._home_epoch) if valid else None
            signature = ((anchor["latitude_e7"], anchor["longitude_e7"], anchor["absolute_altitude_amsl_mm"])
                         if anchor else None)
            if signature != self._home_signature:
                self._home_epoch += 1
                self._home_signature = signature
            self._home_anchor = canonical_home_anchor(value, self._home_epoch) if valid else None
        self._telemetry_cache[name] = value
        self._telemetry_fields[name] = self._field(epoch, valid=valid, home=name == "home")

    async def _consume_stream(self, system: System, generation: int, epoch: int, name: str, api: str) -> None:
        stream = None
        try:
            stream = getattr(system.telemetry, api)()
            async for value in stream:
                if not self._current(system, generation, epoch):
                    return
                self._cache_value(name, value, epoch)
            if self._current(system, generation, epoch):
                self._subscription_error = f"{name}: stream ended"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._current(system, generation, epoch):
                self._subscription_error = f"{name}: {exc}"
        finally:
            if self._current(system, generation, epoch) and self._subscription_error is None:
                self._subscription_error = f"{name}: subscription stopped"
            await self._close_stream(stream)

    async def _consume_mission(self, system: System, generation: int, epoch: int) -> None:
        stream = None
        try:
            stream = system.mission.mission_progress()  # async generator, not RPC
            async for progress in stream:
                if not self._current(system, generation, epoch):
                    return
                current, total = progress.current, progress.total
                valid = (type(current) is int and type(total) is int
                         and 0 <= current <= total)
                self._mission_observation = MissionObservation(
                    current, total, progress_metadata=self._field(epoch, valid=valid),
                )
                if not valid:
                    raise ValueError("invalid mission progress")
                # This is a bounded read-only RPC, not an async stream. A true
                # result does not establish completion of a particular lease.
                finished = await self._bounded(system.mission.is_mission_finished(), _MISSION_OBSERVATION_TIMEOUT_S)
                if not self._current(system, generation, epoch):
                    return
                if type(finished) is not bool:
                    raise ValueError("invalid mission finished response")
                self._mission_observation = MissionObservation(
                    current, total, finished,
                    self._mission_observation.progress_metadata, self._field(epoch),
                )
            if self._current(system, generation, epoch):
                self._subscription_error = "mission_progress: stream ended"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._current(system, generation, epoch):
                self._subscription_error = f"mission_progress: {type(exc).__name__}: {exc}"
        finally:
            if self._current(system, generation, epoch) and self._subscription_error is None:
                self._subscription_error = "mission_progress: subscription stopped"
            await self._close_stream(stream)

    def mission_snapshot(self) -> MissionObservation:
        """Return the last observed progress/finished result, without any I/O."""
        return self._mission_observation

    async def _close_stream(self, stream: Any) -> None:
        close = getattr(stream, "aclose", None)
        if close is not None:
            try:
                await self._bounded(close(), _CLEANUP_TIMEOUT_S)
            except Exception:
                # Cancellation/epoch invalidation already makes this generation
                # unusable, including backends that fail to close a generator.
                pass

    async def _cancel_tasks(self, tasks: tuple[asyncio.Task[Any], ...]) -> None:
        for task in tasks:
            # Register before the await: even cancellation of the cleanup
            # caller must not orphan a subscription that resists cancellation.
            self._retire(task)
            task.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=_CLEANUP_TIMEOUT_S)

    async def _stop_subscriptions(self) -> None:
        tasks = tuple(self._subscription_tasks)
        self._subscription_tasks.clear()
        await self._cancel_tasks(tasks)

    async def _close_system(self, system: System) -> None:
        """Release only this System's resources, with bounded waiting.

        Installed MAVSDK has no public close/disconnect. Its private stop
        method kills only its owned _server_process, not an external server.
        Its plugin API does not expose a channel close handle. Prefer a public
        close API if a later MAVSDK provides one; never invent a disconnect RPC.
        """
        close = getattr(system, "close", None)
        try:
            if callable(close):
                result = close()
                if inspect.isawaitable(result):
                    await self._bounded(result, _CLEANUP_TIMEOUT_S)
                return
            process = getattr(system, "_server_process", None)
            if process is not None:
                system._stop_mavsdk_server()
                await self._bounded(asyncio.to_thread(process.wait, timeout=_CLEANUP_TIMEOUT_S), _CLEANUP_TIMEOUT_S)
        except Exception as exc:
            # An old generation's delayed cleanup cannot fault its successor.
            if self._system is system:
                self._subscription_error = f"cleanup: {exc}"

    async def _shutdown(self) -> None:
        system, self._system = self._system, None
        self._generation += 1
        self._connected = False
        self._clear_cache()
        self._discovered.clear()
        watcher, self._connection_task = self._connection_task, None
        tasks = tuple(self._subscription_tasks) + ((watcher,) if watcher else ())
        self._subscription_tasks.clear()

        async def cleanup() -> None:
            try:
                await self._cancel_tasks(tasks)
            finally:
                if system is not None:
                    await self._close_system(system)

        # Invalidation above has no await. Shield cleanup, not the caller: a
        # cancelled disconnect returns promptly while resource owners remain
        # referenced until their bounded cleanup completes.
        task = asyncio.create_task(cleanup())
        self._retire(task)
        await asyncio.shield(task)

    async def disconnect(self) -> None:
        async with self._lifecycle_lock:
            await self._shutdown()

    def _require_system(self) -> System:
        if self._system is None or not self._connected:
            raise AdapterError("Not connected — call connect() first.")
        return self._system

    def safety_snapshot(self) -> VehicleObservation:
        """Synchronous cache copy: no I/O, clock query, or receipt refresh."""
        fields = dict(self._telemetry_fields)
        snapshot = TelemetrySnapshot(
            **{name: self._telemetry_cache.get(name) for name in _TELEMETRY_NAMES},
            metadata=TelemetryMetadata(
                max((field.received_monotonic for field in fields.values()), default=0.0),
                "MAVSDK", self._connection_epoch, self._home_epoch, fields,
            ),
        )
        return VehicleObservation(
            snapshot, self._telemetry_cache.get("home"), self._home_anchor,
            self._connected,
            self._connected and self._subscription_error is None
            and self._connection_task is not None and self._task_alive(self._connection_task)
            and len(self._subscription_tasks) == 9
            and all(self._task_alive(task) for task in self._subscription_tasks),
        )

    @staticmethod
    def _task_alive(task: asyncio.Task[Any]) -> bool:
        # Task.cancelling() is available from Python 3.11; on 3.10 the
        # subscription's finally block still latches an unexpected stop.
        return not task.done() and not getattr(task, "cancelling", lambda: 0)()

    async def get_telemetry(self) -> TelemetrySnapshot:
        self._require_system()
        return self.safety_snapshot().telemetry

    async def get_flight_mode(self) -> str:
        self._require_system()
        mode = self._telemetry_cache.get("flight_mode")
        if mode is None:
            raise AdapterError("The vehicle has not reported a flight mode.")
        return mode

    async def get_home(self) -> Position | None:
        self._require_system()
        return self._telemetry_cache.get("home")

    async def arm(self, *, lease: ExecutionLease | None = None) -> None:
        system = self._require_system()
        self._consume_lease(lease, "arm")
        await self._action("arm", system.action.arm())

    async def disarm(self, *, lease: ExecutionLease | None = None) -> None:
        system = self._require_system()
        self._consume_lease(lease, "disarm")
        await self._action("disarm", system.action.disarm())

    async def takeoff(self, altitude_m: float, *, lease: ExecutionLease | None = None) -> None:
        system = self._require_system()
        altitude = float(altitude_m)
        self._consume_lease(lease, "set_takeoff_altitude", {"altitude_m": altitude_m})
        await self._action("set_takeoff_altitude", system.action.set_takeoff_altitude(altitude))
        if self._require_system() is not system:
            raise AdapterError("Connection changed during takeoff")
        self._consume_lease(lease, "takeoff")
        await self._action("takeoff", system.action.takeoff())

    async def land(self, *, lease: ExecutionLease | None = None) -> None:
        system = self._require_system()
        self._consume_lease(lease, "land")
        await self._action("land", system.action.land())

    async def hold(self, *, lease: ExecutionLease | None = None) -> None:
        system = self._require_system()
        self._consume_lease(lease, "hold")
        await self._action("hold", system.action.hold())

    async def return_to_launch(self, *, lease: ExecutionLease | None = None) -> None:
        system = self._require_system()
        self._consume_lease(lease, "return_to_launch")
        await self._action("return_to_launch", system.action.return_to_launch())

    async def goto(self, waypoint: Waypoint, yaw_deg: float = 0.0, *, lease: ExecutionLease | None = None, absolute_altitude_m: float | None = None) -> None:
        system = self._require_system()
        if absolute_altitude_m is None:
            raise LeaseError("goto requires the gateway-resolved absolute altitude")
        yaw = float(yaw_deg)
        self._consume_lease(lease, "goto_location", {"waypoint": waypoint, "yaw_deg": yaw_deg, "absolute_altitude_m": absolute_altitude_m})
        await self._action("goto_location", system.action.goto_location(waypoint.latitude_deg, waypoint.longitude_deg, absolute_altitude_m, yaw))

    async def fly_mission(self, waypoints: list[Waypoint], *, lease: ExecutionLease | None = None) -> None:
        system = self._require_system()
        plan = MissionPlan([_mission_item(wp) for wp in waypoints])
        self._consume_lease(lease, "upload_mission", {"waypoints": tuple(waypoints)})
        await self._action("upload_mission", system.mission.upload_mission(plan))
        if self._require_system() is not system:
            raise AdapterError("Connection changed during mission upload")
        self._consume_lease(lease, "start_mission")
        await self._action("start_mission", system.mission.start_mission())

    @staticmethod
    async def _action(operation: str, coro: Any) -> None:
        try:
            await coro
        except Exception as exc:
            raise AdapterError(f"{operation} failed: {exc}") from exc


def _enum_name(value: Any) -> str | None:
    # Never turn a malformed sensor value (NaN, bool, arbitrary object) into
    # a seemingly valid flight-mode string. MAVSDK enums expose a string name.
    name = getattr(value, "name", value)
    return name if type(name) is str else None


def _to_position(position: Any) -> Position | None:
    if position is None:
        return None
    return Position(position.latitude_deg, position.longitude_deg,
                    position.absolute_altitude_m, position.relative_altitude_m)


def _mission_item(waypoint: Waypoint) -> MissionItem:
    nan = float("nan")
    kwargs = dict(
        latitude_deg=waypoint.latitude_deg,
        longitude_deg=waypoint.longitude_deg,
        relative_altitude_m=waypoint.relative_altitude_m,
        speed_m_s=waypoint.speed_m_s if waypoint.speed_m_s is not None else _DEFAULT_MISSION_SPEED_M_S,
        is_fly_through=True,
        gimbal_pitch_deg=nan, gimbal_yaw_deg=nan,
        camera_action=MissionItem.CameraAction.NONE,
        loiter_time_s=nan, camera_photo_interval_s=nan, acceptance_radius_m=nan,
        yaw_deg=nan, camera_photo_distance_m=nan,
    )
    if hasattr(MissionItem, "VehicleAction"):
        try:
            return MissionItem(**kwargs, vehicle_action=MissionItem.VehicleAction.NONE)
        except TypeError:
            pass
    return MissionItem(**kwargs)
