"""Trusted principal and vehicle/action authorization contract."""
from __future__ import annotations
from dataclasses import dataclass
from enum import Enum
from .models import Action, ContractError, PolicyDecision, PolicyResult, ReasonCode, RiskLabel

class Role(str, Enum): OBSERVER="observer"; FLIGHT_AGENT="flight_agent"; HUMAN_OPERATOR="human_operator"; SAFETY_SUPERVISOR="safety_supervisor"
@dataclass(frozen=True, slots=True)
class Principal:
    actor_id: str
    role: Role
    vehicle_id: str
    readonly: bool = True
    def __post_init__(self):
        if not self.actor_id or not self.vehicle_id: raise ContractError("principal identity is required")
        if not isinstance(self.readonly, bool): raise ContractError("readonly must be boolean")
@dataclass(frozen=True, slots=True)
class TrustedContext:
    principal: Principal
    environment: str
    gateway_id: str
    boot_epoch: str

_MUTATIONS = frozenset(Action)

def authorize(context: TrustedContext, action: Action) -> PolicyResult:
    if action not in _MUTATIONS: return PolicyResult(PolicyDecision.DENY, ReasonCode.INVALID_ARGUMENT, RiskLabel.READ)
    p = context.principal
    if p.role is not Role.FLIGHT_AGENT: return PolicyResult(PolicyDecision.DENY, ReasonCode.UNAUTHORIZED, RiskLabel.HIGH)
    if p.readonly: return PolicyResult(PolicyDecision.DENY, ReasonCode.READONLY, RiskLabel.HIGH)
    return PolicyResult(PolicyDecision.REQUIRE_APPROVAL, ReasonCode.APPROVAL_REQUIRED, RiskLabel.HIGH)
