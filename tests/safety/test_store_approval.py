import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mavlink_mcp.safety.approval import TrustedKey, TrustStore, sign, verify, ApprovalExpired
from mavlink_mcp.safety.models import Action, Environment, FrozenRequest, ContractError, parse_canonical_json
from mavlink_mcp.safety.store import SafetyStore, ClaimRejected, StoreError, StoreBusy, StoreCorrupt


class Clock:
    def __init__(self, value=1_000_000):
        self.value = value
    def __call__(self):
        return self.value


def request(clock):
    return FrozenRequest(1, "r1", "actor", "v1", "g1", "boot", Environment.MOCK,
                         "conn", "home", "policy", Action.ARM, {}, created_at_utc_ms=clock())


def test_create_approval_uses_store_clock_and_is_atomic(tmp_path):
    clock = Clock()
    with SafetyStore(tmp_path / "ledger.sqlite3", bootstrap=True, clock=clock) as store:
        req = request(clock)
        record = store.create_approval(req, expires_at_utc_ms=clock() + 60_000, required_operator_role="human_operator", nonce="n1")
        assert store.get_request("r1")[2] == "AWAITING_APPROVAL"
        assert store.get_nonce_binding("n1")[:2] == ("r1", "PENDING")
        assert record.expires_at_utc_ms == 1_060_000


def test_expiry_sweep_frees_pending_nonce_and_audits(tmp_path):
    clock = Clock()
    with SafetyStore(tmp_path / "ledger.sqlite3", bootstrap=True, clock=clock) as store:
        store.create_approval(request(clock), expires_at_utc_ms=clock() + 10, required_operator_role="human_operator", nonce="n1")
        clock.value += 10
        assert store.sweep_expired() == 1
        assert store.get_nonce_binding("n1")[1] == "EXPIRED"
        assert store.get_request("r1")[2] == "EXPIRED"
        assert any(e["kind"] == "APPROVAL_EXPIRED" for e in store.export_audit())
        assert SafetyStore.verify_audit(store.export_audit())


def test_state_transitions_are_explicit(tmp_path):
    clock = Clock()
    with SafetyStore(tmp_path / "ledger.sqlite3", bootstrap=True, clock=clock) as store:
        store.create_approval(request(clock), expires_at_utc_ms=clock() + 10, required_operator_role="human_operator", nonce="n1")
        with pytest.raises(ClaimRejected):
            store.set_state("r1", "COMPLETED")


def test_approval_roundtrip_and_store_binding_without_private_key_in_repo(tmp_path):
    clock = Clock()
    private = Ed25519PrivateKey.generate()
    req = request(clock)
    with SafetyStore(tmp_path / "ledger.sqlite3", bootstrap=True, clock=clock) as store:
        store.create_approval(req, expires_at_utc_ms=clock() + 60_000, required_operator_role="human_operator", nonce="n1")
        trust = TrustStore({"k1": TrustedKey("k1", "op1", private.public_key().public_bytes_raw(), frozenset({"human_operator"}))})
        token = sign(req, nonce="n1", key_id="k1", operator_id="op1", private_key=private, issued_at_utc_ms=clock(), expires_at_utc_ms=clock() + 60_000)
        assert verify(token, request=req, trust_store=trust, store=store, now_utc_ms=clock())["nonce"] == "n1"
        store.claim_and_intent(request_id="r1", nonce="n1", boot_epoch="boot")
        with pytest.raises(Exception):
            verify(token, request=req, trust_store=trust, store=store, now_utc_ms=clock())


def test_duplicate_claim_keys_are_rejected():
    with pytest.raises(ContractError):
        parse_canonical_json('{"nonce":"a","nonce":"b"}')


def test_approval_rejects_future_issue_time(tmp_path):
    clock = Clock()
    private = Ed25519PrivateKey.generate()
    req = request(clock)
    with SafetyStore(tmp_path / "ledger.sqlite3", bootstrap=True, clock=clock) as store:
        store.create_approval(req, expires_at_utc_ms=clock() + 60_000, required_operator_role="human_operator", nonce="n1")
        trust = TrustStore({"k1": TrustedKey("k1", "op1", private.public_key().public_bytes_raw(), frozenset({"human_operator"}))})
        token = sign(req, nonce="n1", key_id="k1", operator_id="op1", private_key=private, issued_at_utc_ms=clock() + 1, expires_at_utc_ms=clock() + 60_000)
        with pytest.raises(ApprovalExpired):
            verify(token, request=req, trust_store=trust, store=store, now_utc_ms=clock())


def add_request(store, clock, name="r1", role="human_operator"):
    req = replace(request(clock), request_id=name, request_digest="")
    record = store.create_approval(req, expires_at_utc_ms=clock() + 60_000,
                                   required_operator_role=role, nonce="nonce-" + name)
    return req, record


def test_replay_rolls_back_then_new_transaction_succeeds(tmp_path):
    clock = Clock()
    with SafetyStore(tmp_path / "ledger.sqlite3", bootstrap=True, clock=clock) as store:
        req, record = add_request(store, clock)
        store.claim_and_intent(request_id=req.request_id, nonce=record.nonce, boot_epoch=req.boot_epoch)
        with pytest.raises(ClaimRejected, match="not pending"):
            store.claim_and_intent(request_id=req.request_id, nonce=record.nonce, boot_epoch=req.boot_epoch)
        assert not store._conn.in_transaction
        req2, record2 = add_request(store, clock, "r2")
        store.claim_and_intent(request_id=req2.request_id, nonce=record2.nonce, boot_epoch=req2.boot_epoch)
        assert store.get_request("r2")[2] == "EXECUTING"
        assert SafetyStore.verify_audit(store.export_audit())


@pytest.mark.parametrize("failure", ["missing", "epoch", "expiry", "state"])
def test_rejected_claim_closes_transaction(tmp_path, failure):
    clock = Clock()
    with SafetyStore(tmp_path / "ledger.sqlite3", bootstrap=True, clock=clock) as store:
        req, record = add_request(store, clock)
        if failure == "expiry": clock.value = record.expires_at_utc_ms
        if failure == "state": store.set_state(req.request_id, "DENIED")
        with pytest.raises(ClaimRejected):
            store.claim_and_intent(request_id=req.request_id,
                nonce="missing" if failure == "missing" else record.nonce,
                boot_epoch="wrong" if failure == "epoch" else req.boot_epoch)
        assert not store._conn.in_transaction
        store.record_event("AFTER_REJECTION", {})
        if failure in {"epoch", "expiry"}:
            assert store.get_request(req.request_id)[2] == "EXPIRED"
            assert any(e["kind"] == ("APPROVAL_INVALIDATED" if failure == "epoch" else "APPROVAL_EXPIRED") for e in store.export_audit())


@pytest.mark.parametrize("error", [OSError("disk failure"), sqlite3.OperationalError("write failed"), RuntimeError("serializer failure")])
def test_intent_failure_rolls_back_nonce_and_request(tmp_path, monkeypatch, error):
    clock = Clock()
    with SafetyStore(tmp_path / "ledger.sqlite3", bootstrap=True, clock=clock) as store:
        req, record = add_request(store, clock)
        with monkeypatch.context() as patch:
            def fail(*args, **kwargs): raise error
            patch.setattr(store, "_event", fail)
            with pytest.raises(StoreError if isinstance(error, (OSError, sqlite3.Error)) else RuntimeError):
                store.claim_and_intent(request_id=req.request_id, nonce=record.nonce, boot_epoch=req.boot_epoch)
        assert not store._conn.in_transaction
        assert store.get_request(req.request_id)[2] == "AWAITING_APPROVAL"
        assert store.get_nonce_binding(record.nonce)[1] == "PENDING"
        store.claim_and_intent(request_id=req.request_id, nonce=record.nonce, boot_epoch=req.boot_epoch)


OPERATIONS = [
    lambda s, c: s.get_request("r1"),
    lambda s, c: s.get_nonce_binding("nonce-r1"),
    lambda s, c: s.get_approval_record("r1"),
    lambda s, c: s.request_boot_epoch("r1"),
    lambda s, c: s.has_uncertain_execution("v1"),
    lambda s, c: s.export_audit(),
    lambda s, c: s.sweep_expired(),
    lambda s, c: s.record_event("TEST", {}),
    lambda s, c: s.check_writable("r1"),
    lambda s, c: s.set_state("r1", "DENIED"),
    lambda s, c: s.claim_and_intent(request_id="r1", nonce="nonce-r1", boot_epoch="boot"),
    lambda s, c: add_request(s, c, "r2"),
]


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("failure", ["closed", "raw_closed", "deleted", "missing_live"])
def test_unavailable_store_operations_fail_closed(tmp_path, monkeypatch, operation, failure):
    clock = Clock()
    path = tmp_path / "ledger.sqlite3"
    with SafetyStore(path, bootstrap=True, clock=clock) as store:
        add_request(store, clock)
        if failure == "closed": store.close()
        if failure in {"raw_closed", "deleted"}: store._conn.close()
        if failure == "deleted": path.unlink()  # Windows forbids unlinking open SQLite files.
        if failure == "missing_live":
            original = type(path).is_file
            monkeypatch.setattr(type(path), "is_file", lambda p: False if p == path else original(p))
        with pytest.raises(StoreError): operation(store, clock)
        if failure == "deleted": assert not path.exists()


def test_missing_or_corrupt_established_ledger_is_not_recreated(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    with pytest.raises(StoreError): SafetyStore(path)
    assert not path.exists()
    path.write_bytes(b"not sqlite")
    with pytest.raises(StoreError): SafetyStore(path, bootstrap=True)
    assert path.read_bytes() == b"not sqlite"
    # Failed initialization released the OS owner lock, not just the DB handle.
    with pytest.raises(StoreError) as caught: SafetyStore(path)
    assert not isinstance(caught.value, StoreBusy)


def test_second_owner_cannot_invalidate_live_approval_and_close_releases(tmp_path):
    clock = Clock()
    path = tmp_path / "ledger.sqlite3"
    with SafetyStore(path, bootstrap=True, clock=clock) as store:
        _, record = add_request(store, clock)
        before = store.export_audit()
        with pytest.raises(StoreBusy): SafetyStore(path, clock=clock)
        assert store.get_approval_record("r1") == record
        assert store.export_audit() == before
    with SafetyStore(path, clock=clock) as restarted:
        assert restarted.get_approval_record("r1").nonce_state == "INVALIDATED"


def test_os_lock_excludes_other_process_and_releases_on_process_exit(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    with SafetyStore(path, bootstrap=True) as store:
        child = subprocess.run([sys.executable, "-c", """
import sys
from mavlink_mcp.safety.store import SafetyStore, StoreBusy
try:
    SafetyStore(sys.argv[1])
except StoreBusy:
    sys.exit(0)
sys.exit(9)
""", str(path)], capture_output=True, text=True, timeout=20)
        assert child.returncode == 0, child.stderr
        store.record_event("STILL_OWNED", {})
    child = subprocess.run([sys.executable, "-c", """
import os, sys
from mavlink_mcp.safety.store import SafetyStore
store = SafetyStore(sys.argv[1])
store.record_event('CHILD_OWNER', {})
os._exit(0)
""", str(path)], capture_output=True, text=True, timeout=20)
    assert child.returncode == 0, child.stderr
    with SafetyStore(path) as restarted:
        assert any(e["kind"] == "CHILD_OWNER" for e in restarted.export_audit())


def test_restart_invalidates_pending_and_quarantines_executing_without_timer_clear(tmp_path):
    clock = Clock()
    path = tmp_path / "ledger.sqlite3"
    with SafetyStore(path, bootstrap=True, clock=clock) as store:
        add_request(store, clock, "pending", role="supervisor")
        req, record = add_request(store, clock, "executing")
        store.claim_and_intent(request_id=req.request_id, nonce=record.nonce, boot_epoch=req.boot_epoch)
        assert store.has_uncertain_execution("v1")
        assert not store.has_uncertain_execution("other")
    with SafetyStore(path, clock=clock) as store:
        assert store.get_request("pending")[2] == "EXPIRED"
        assert store.get_approval_record("pending").nonce_state == "INVALIDATED"
        assert store.get_approval_record("pending").required_operator_role == "supervisor"
        assert store.get_request("executing")[2] == "UNKNOWN"
        assert store.get_approval_record("executing").nonce_state == "CONSUMED"
        clock.value += 1_000_000
        assert store.sweep_expired() == 0
        assert store.has_uncertain_execution("v1")
        with pytest.raises(ClaimRejected): store.set_state("executing", "EXPIRED")
        events = store.export_audit()
        assert any(e["kind"] == "APPROVAL_INVALIDATED" and e["data"]["request_id"] == "pending" for e in events)
        assert any(e["kind"] == "REQUEST_UNKNOWN" and e["data"]["partial_effects"] for e in events)
        assert SafetyStore.verify_audit(events)


@pytest.mark.parametrize("state", ["ACKNOWLEDGED", "COMPLETED", "FAILED", "UNKNOWN"])
def test_execution_outcomes_and_partial_effects_are_persisted(tmp_path, state):
    clock = Clock()
    path = tmp_path / "ledger.sqlite3"
    with SafetyStore(path, bootstrap=True, clock=clock) as store:
        req, record = add_request(store, clock)
        store.claim_and_intent(request_id=req.request_id, nonce=record.nonce, boot_epoch=req.boot_epoch)
        event_id = store.set_state(req.request_id, state, partial_effects=state in {"FAILED", "UNKNOWN"})
    with SafetyStore(path, clock=clock) as store:
        assert store.get_request(req.request_id)[2] == state
        event = next(e for e in store.export_audit() if e["event_id"] == event_id)
        assert event["data"]["partial_effects"] is (state in {"FAILED", "UNKNOWN"})
        assert store.has_uncertain_execution("v1") is (state == "UNKNOWN")
        with pytest.raises(ClaimRejected): store.check_writable(req.request_id)


def test_sink_check_requires_executing_and_durably_records_sink(tmp_path):
    clock = Clock()
    path = tmp_path / "ledger.sqlite3"
    with SafetyStore(path, bootstrap=True, clock=clock) as store:
        req, record = add_request(store, clock)
        for request_id in (req.request_id, "missing"):
            with pytest.raises(ClaimRejected): store.check_writable(request_id)
        store.claim_and_intent(request_id=req.request_id, nonce=record.nonce, boot_epoch=req.boot_epoch)
        event_id = store.check_writable(req.request_id, sink="arm")
        assert not store._conn.in_transaction
    with SafetyStore(path, clock=clock) as store:
        event = next(e for e in store.export_audit() if e["event_id"] == event_id)
        assert event["kind"] == "SINK_CHECK"
        assert event["data"] == {"request_id": req.request_id, "sink": "arm"}


def test_sink_check_rejects_readonly_connection_and_corrupt_audit(tmp_path):
    clock = Clock()
    with SafetyStore(tmp_path / "ledger.sqlite3", bootstrap=True, clock=clock) as store:
        req, record = add_request(store, clock)
        store.claim_and_intent(request_id=req.request_id, nonce=record.nonce, boot_epoch=req.boot_epoch)
        before = store.export_audit()
        store._conn.execute("PRAGMA query_only=ON")
        with pytest.raises(StoreError): store.check_writable(req.request_id)
        assert not store._conn.in_transaction
        store._conn.execute("PRAGMA query_only=OFF")
        assert store.export_audit() == before
        store._conn.execute("UPDATE audit_events SET previous_hash='tampered' WHERE sequence=1")
        with pytest.raises(StoreCorrupt): store.check_writable(req.request_id)
        assert not store._conn.in_transaction


def test_threads_serialize_all_transactions_and_audit_chain(tmp_path):
    clock = Clock()
    with SafetyStore(tmp_path / "ledger.sqlite3", bootstrap=True, clock=clock) as store:
        def operation(index):
            req, record = add_request(store, clock, str(index))
            store.get_request(req.request_id)
            store.claim_and_intent(request_id=req.request_id, nonce=record.nonce, boot_epoch=req.boot_epoch)
            store.check_writable(req.request_id, "arm")
            store.set_state(req.request_id, "ACKNOWLEDGED")
            store.sweep_expired()
            store.export_audit()
        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(operation, range(24)))
        events = store.export_audit()
        assert len(events) == 24 * 4
        assert SafetyStore.verify_audit(events)
