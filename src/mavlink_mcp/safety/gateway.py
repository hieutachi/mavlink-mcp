"""Trusted approval orchestration and bounded dispatch to protected adapter sinks."""
from __future__ import annotations
import asyncio, uuid, time
from dataclasses import dataclass, replace
from typing import Any
from .authorization import TrustedContext
from .models import (FrozenRequest, PolicyDecision, ReasonCode, request_digest, Action,
                     ContractError, Environment, canonical_bytes, normalize_parameters,
                     request_from_dict, request_to_dict, sha256_digest)
from mavlink_mcp.adapter import AdapterError, ExecutionLease, LeaseError
from .policy import evaluate
from .approval import verify, ApprovalError, ApprovalExpired, ApprovalToken
from .admission import AdmissionError, SINKS, check_observation, execution_parameters
from .store import SafetyStore, StoreError, ClaimRejected


@dataclass(frozen=True, slots=True)
class GatewayResult:
    decision: PolicyDecision
    reason_code: ReasonCode
    request_id: str | None = None
    execution_status: str = "NOT_STARTED"
    intent_event_id: str | None = None
    approval_record: Any = None
    approval_request: Any = None
    partial_effects: bool = False

    def to_dict(self):
        result = {"schema_version": 1, "request_id": self.request_id,
                  "decision": self.decision.value, "reason_code": self.reason_code.value,
                  "execution_status": self.execution_status, "audit_event_id": self.intent_event_id,
                  "partial_effects": self.partial_effects}
        if self.approval_request is not None:
            result["approval_request"] = self.approval_request
        # REQUIRE_APPROVAL is the normal first step, not a failure: flagging it
        # as an error teaches agents to treat needing consent as a fault.
        if self.decision is PolicyDecision.DENY:
            result.update(error=self.reason_code.value, guardrail=True)
        return result

class SafetyGateway:
    def __init__(self, *, store, policy, approval_store, adapter, context, clock=None,
                 monotonic=time.monotonic, lock_timeout=1.0, dispatch_timeout=5.0, approval_ttl_ms=60000):
        if context.boot_epoch != store.boot_epoch:
            raise ValueError("context must bind the live store boot epoch")
        if not 0 < approval_ttl_ms <= 60000 or not 0 < dispatch_timeout <= 60:
            raise ValueError("invalid gateway deadline")
        self.store, self.policy, self.approval_store = store, policy, approval_store
        self.adapter, self.context = adapter, context
        self.clock, self.monotonic = clock or store.clock, monotonic
        self.lock_timeout, self.dispatch_timeout = lock_timeout, dispatch_timeout
        self.approval_ttl_ms = approval_ttl_ms
        self._lock, self._connect_lock = asyncio.Lock(), asyncio.Lock()
        self._issuer = object()
        self._approval_deadlines = {}
        self._dispatch_tasks = set()
        self._retired = set()
        self._quarantined = store.has_uncertain_execution(context.principal.vehicle_id)
        adapter._bind_gateway(self._issuer, context.principal.vehicle_id)

    def _deny(self, reason, request_id=None):
        return GatewayResult(PolicyDecision.DENY, reason, request_id)

    def _binding(self, request, context):
        if context != self.context or request.actor_id != self.context.principal.actor_id:
            raise AdmissionError(ReasonCode.UNAUTHORIZED)
        if (request.vehicle_id != self.context.principal.vehicle_id or request.gateway_id != self.context.gateway_id
                or request.boot_epoch != self.store.boot_epoch or request.environment.value != self.context.environment):
            raise AdmissionError(ReasonCode.VEHICLE_CHANGED)
        if request.policy_hash != self.policy.policy_hash:
            raise AdmissionError(ReasonCode.POLICY_CHANGED)
        if request_digest(request) != request.request_digest:
            raise AdmissionError(ReasonCode.PARAMETER_MISMATCH)
        if request.reference_frame != "HOME_RELATIVE":
            raise AdmissionError(ReasonCode.INVALID_ARGUMENT)

    def _check(self, request, *, preflight=True):
        check_observation(request, self.adapter.safety_snapshot(), context=self.context,
                          policy=self.policy, adapter_name=self.adapter.name,
                          now=self.monotonic(), preflight=preflight)

    def _retired_done(self, task):
        self._retired.discard(task)
        if not task.cancelled():
            task.exception()

    async def _bounded(self, awaitable):
        # wait_for can overrun its deadline if the inner task resists cancel.
        task = asyncio.ensure_future(awaitable)
        try:
            done, _ = await asyncio.wait({task}, timeout=self.dispatch_timeout)
            if not done:
                raise asyncio.TimeoutError
            return task.result()
        finally:
            if not task.done():
                task.cancel()
                self._retired.add(task)
                task.add_done_callback(self._retired_done)

    async def _observe(self):
        async with self._connect_lock:
            if not self.adapter.is_connected():
                await self._bounded(self.adapter.connect())
        await self._bounded(self.adapter.get_telemetry())
        return self.adapter.safety_snapshot()

    async def execute(self, action: Action, parameters, *, request_id=None, approval_token=None):
        try:
            parameters = normalize_parameters(action, parameters)  # Before ANY await.
            if len(canonical_bytes(parameters)) > 256 * 1024:
                raise ContractError("request too large")
            if self._quarantined:
                return self._deny(ReasonCode.EXECUTION_UNKNOWN, request_id)
            if self._dispatch_tasks:
                return self._deny(ReasonCode.BUSY, request_id)
            base = evaluate(self.context, self.policy, action, parameters)
            if base.decision is PolicyDecision.DENY:
                return self._deny(base.reason_code, request_id)
            if request_id is not None:
                if not isinstance(request_id, str) or not request_id or not approval_token:
                    return self._deny(ReasonCode.APPROVAL_INVALID, request_id)
                record = self.store.get_request(request_id)
                if record is None:
                    return self._deny(ReasonCode.APPROVAL_INVALID, request_id)
                if record[2] in {"EXECUTING", "ACKNOWLEDGED", "COMPLETED", "FAILED", "UNKNOWN"}:
                    return self._deny(ReasonCode.APPROVAL_REPLAY, request_id)
                request = request_from_dict(record[0])
                # Canonical bytes distinguish True from 1, unlike ==.
                if request.action is not action or canonical_bytes(request.parameters) != canonical_bytes(parameters):
                    return self._deny(ReasonCode.PARAMETER_MISMATCH, request_id)
                return await self.submit(request, self.context, approval=approval_token)
            if approval_token is not None:
                return self._deny(ReasonCode.APPROVAL_INVALID)
            observation = await self._observe()
            if observation is None or observation.telemetry.metadata is None:
                return self._deny(ReasonCode.TELEMETRY_INCOMPLETE)
            meta = observation.telemetry.metadata
            request = FrozenRequest(1, str(uuid.uuid4()), self.context.principal.actor_id,
                self.context.principal.vehicle_id, self.context.gateway_id, self.store.boot_epoch,
                Environment(self.context.environment), str(meta.connection_epoch), str(meta.home_epoch),
                self.policy.policy_hash, action, parameters, home_anchor=observation.home_anchor,
                mission_digest=sha256_digest(parameters) if action is Action.FLY_MISSION else None,
                created_at_utc_ms=self.clock())
            return await self.submit(request, self.context)
        except AdmissionError as exc:
            return self._deny(exc.reason, request_id)
        except (ContractError, ValueError, TypeError, KeyError, OverflowError):
            return self._deny(ReasonCode.INVALID_ARGUMENT, request_id)
        except StoreError:
            return self._deny(ReasonCode.AUDIT_UNAVAILABLE, request_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            return self._deny(ReasonCode.TELEMETRY_INCOMPLETE, request_id)

    async def submit(self, request: FrozenRequest, context, *, approval=None):
        try:
            self._binding(request, context)
            if self._quarantined:
                raise AdmissionError(ReasonCode.EXECUTION_UNKNOWN)
            if approval is None:
                self._check(request)
                issued = self.clock()
                request = replace(request, created_at_utc_ms=issued, request_digest="")
                # Pin the same instant in the request, the ledger and the
                # operator payload; a second clock read would shift the window.
                record = self.store.create_approval(request, expires_at_utc_ms=issued + self.approval_ttl_ms,
                                                    required_operator_role="human_operator",
                                                    issued_at_utc_ms=issued)
                now = self.monotonic()
                self._approval_deadlines = {k: v for k, v in self._approval_deadlines.items() if v > now}
                self._approval_deadlines[request.request_id] = now + self.approval_ttl_ms / 1000
                payload = request_to_dict(request) | {"nonce": record.nonce, "issued_at_utc_ms": issued,
                                                       "expires_at_utc_ms": record.expires_at_utc_ms}
                return GatewayResult(PolicyDecision.REQUIRE_APPROVAL, ReasonCode.APPROVAL_REQUIRED,
                                     request.request_id, approval_record=record, approval_request=payload)
            token = ApprovalToken.decode(approval) if isinstance(approval, str) else approval
            verify(token, request=request, trust_store=self.approval_store, store=self.store, now_utc_ms=self.clock())
            try:
                await asyncio.wait_for(self._lock.acquire(), self.lock_timeout)
            except asyncio.TimeoutError:
                return self._deny(ReasonCode.BUSY, request.request_id)
            try:
                if self._quarantined:
                    raise AdmissionError(ReasonCode.EXECUTION_UNKNOWN)
                if self._dispatch_tasks:
                    raise AdmissionError(ReasonCode.BUSY)
                await self._observe()
                self._binding(request, context)
                self._check(request)
                claims = verify(token, request=request, trust_store=self.approval_store,
                                store=self.store, now_utc_ms=self.clock())
                deadline = self._approval_deadlines.get(request.request_id, 0)
                if self.monotonic() >= deadline:
                    raise ApprovalExpired("approval monotonic deadline elapsed")
                plan = execution_parameters(request)
                def recheck(sink):
                    self._binding(request, context)
                    try:
                        key = self.approval_store.lookup(claims["key_id"])
                        if (key.operator_id != claims["operator_id"] or "human_operator" not in key.roles
                                or (key.vehicles and request.vehicle_id not in key.vehicles)
                                or (key.actions and request.action.value not in key.actions)):
                            raise ApprovalError("operator scope changed")
                    except ApprovalError:
                        raise AdmissionError(ReasonCode.APPROVAL_INVALID) from None
                    self._check(request, preflight=lease.consumed_sinks == 0)
                    self.store.check_writable(request.request_id, sink=sink)
                lease = ExecutionLease(str(uuid.uuid4()), request.request_id, request.action.value,
                    request.vehicle_id, claims["expires_at_utc_ms"], plan, SINKS[request.action],
                    _issuer=self._issuer, _clock=self.clock, _monotonic=self.monotonic,
                    _deadline=min(deadline, self.monotonic() + self.dispatch_timeout), _recheck=recheck)
                claim = self.store.claim_and_intent(request_id=request.request_id,
                                                   nonce=claims["nonce"], boot_epoch=self.store.boot_epoch)
                try:
                    return await self._run_dispatch(request, lease, plan, claim.intent_event_id)
                except BaseException as exc:
                    # Nothing after the durable claim may leave the writer open
                    # if dispatch setup itself fails (including cancellation).
                    lease.revoke()
                    self._quarantined = True
                    try:
                        if self.store.get_request(request.request_id)[2] == "EXECUTING":
                            self.store.set_state(request.request_id, "UNKNOWN", partial_effects=lease.consumed_sinks > 0)
                    except StoreError:
                        pass
                    if not isinstance(exc, Exception):
                        raise
                    return GatewayResult(PolicyDecision.DENY, ReasonCode.EXECUTION_UNKNOWN, request.request_id,
                                         "UNKNOWN", claim.intent_event_id, partial_effects=lease.consumed_sinks > 0)
            finally:
                self._lock.release()
        except AdmissionError as exc:
            return self._deny(exc.reason, request.request_id)
        except ApprovalExpired:
            return self._deny(ReasonCode.APPROVAL_EXPIRED, request.request_id)
        except ApprovalError:
            return self._deny(ReasonCode.APPROVAL_INVALID, request.request_id)
        except ClaimRejected:
            return self._deny(ReasonCode.APPROVAL_REPLAY, request.request_id)
        except StoreError:
            return self._deny(ReasonCode.AUDIT_UNAVAILABLE, request.request_id)
        except (asyncio.TimeoutError, AdapterError):
            # Only pre-claim observation can reach here; post-claim errors
            # return UNKNOWN above.
            return self._deny(ReasonCode.TELEMETRY_INCOMPLETE, request.request_id)

    async def _dispatch(self, request, lease, plan):
        await getattr(self.adapter, request.action.value)(**dict(plan), lease=lease)

    async def _run_dispatch(self, request, lease, plan, event_id):
        task = asyncio.create_task(self._dispatch(request, lease, plan))
        self._dispatch_tasks.add(task)
        def finished(done):
            self._dispatch_tasks.discard(done)
            if not done.cancelled():
                done.exception()
        task.add_done_callback(finished)
        status, reason = "UNKNOWN", ReasonCode.EXECUTION_UNKNOWN
        try:
            done, _ = await asyncio.wait({task}, timeout=self.dispatch_timeout)
            if not done:
                lease.revoke()
                task.cancel()
                self._quarantined = True
            else:
                try:
                    task.result()
                    if lease.consumed_sinks != len(lease.sinks):
                        raise RuntimeError("dispatcher did not consume the full sink plan")
                    status, reason = "ACKNOWLEDGED", ReasonCode.ACKNOWLEDGED
                except LeaseError as exc:
                    status = "UNKNOWN" if lease.consumed_sinks else "FAILED"
                    if lease.consumed_sinks:
                        self._quarantined = True  # Never un-latch an uncertain writer.
                    reason = exc.reason if isinstance(exc, AdmissionError) else ReasonCode.APPROVAL_INVALID
                except StoreError:
                    reason = ReasonCode.AUDIT_UNAVAILABLE
                    self._quarantined = True
                except BaseException:
                    self._quarantined = True
            partial = lease.consumed_sinks > 0 and status != "ACKNOWLEDGED"
            try:
                self.store.set_state(request.request_id, status, partial_effects=partial)
            except StoreError:
                self._quarantined = True
                return GatewayResult(PolicyDecision.DENY, ReasonCode.AUDIT_UNAVAILABLE,
                    request.request_id, "UNKNOWN", event_id, partial_effects=lease.consumed_sinks > 0)
            return GatewayResult(PolicyDecision.ALLOW if status == "ACKNOWLEDGED" else PolicyDecision.DENY,
                reason, request.request_id, status, event_id, partial_effects=partial)
        except asyncio.CancelledError:
            lease.revoke()
            task.cancel()
            self._quarantined = True
            try:
                self.store.set_state(request.request_id, "UNKNOWN", partial_effects=lease.consumed_sinks > 0)
            except StoreError:
                pass
            raise
        finally:
            lease.revoke()
            self._approval_deadlines.pop(request.request_id, None)
