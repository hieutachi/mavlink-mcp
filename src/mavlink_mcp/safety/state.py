"""Small explicit state and nonce mappings used by the gateway."""
from enum import Enum
class RequestState(str, Enum): PROPOSED='PROPOSED'; AWAITING_APPROVAL='AWAITING_APPROVAL'; AUTHORIZED='AUTHORIZED'; EXECUTING='EXECUTING'; COMPLETED='COMPLETED'; DENIED='DENIED'; EXPIRED='EXPIRED'; FAILED='FAILED'; UNKNOWN='UNKNOWN'
class NonceState(str, Enum): PENDING='PENDING'; CONSUMED='CONSUMED'; EXPIRED='EXPIRED'; INVALIDATED='INVALIDATED'
_ALLOWED={RequestState.PROPOSED:{RequestState.AWAITING_APPROVAL,RequestState.DENIED},RequestState.AWAITING_APPROVAL:{RequestState.EXPIRED,RequestState.DENIED,RequestState.AUTHORIZED},RequestState.AUTHORIZED:{RequestState.EXPIRED,RequestState.DENIED,RequestState.EXECUTING},RequestState.EXECUTING:{RequestState.COMPLETED,RequestState.FAILED,RequestState.UNKNOWN}}
def can_transition(old, new):
    old=RequestState(old); new=RequestState(new); return new in _ALLOWED.get(old,set())
