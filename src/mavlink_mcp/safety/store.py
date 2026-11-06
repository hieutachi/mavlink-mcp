"""Small, fail-closed SQLite ledger for SafeMAV authorization and audit intent."""
from __future__ import annotations
import hashlib, os, secrets, sqlite3, time, uuid
from contextlib import contextmanager
from dataclasses import dataclass, fields
from threading import RLock
from pathlib import Path
from typing import Any, Callable
from .models import FrozenRequest, canonical_bytes, parse_canonical_json, request_digest

class StoreError(RuntimeError): pass
class StoreUnavailable(StoreError): pass
class StoreCorrupt(StoreError): pass
class StoreBusy(StoreError): pass
class AlreadyExists(StoreError): pass
class NotFound(StoreError): pass
class ClaimRejected(StoreError): pass

@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    request_id: str
    nonce: str
    expires_at_utc_ms: int
    required_operator_role: str
    nonce_state: str = "PENDING"
    issued_at_utc_ms: int = 0

@dataclass(frozen=True, slots=True)
class ClaimResult:
    request_id: str
    nonce: str
    intent_event_id: str
    claimed_at_utc_ms: int

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS requests (
 request_id TEXT PRIMARY KEY, request_json BLOB NOT NULL, request_digest TEXT NOT NULL,
 actor_id TEXT NOT NULL, vehicle_id TEXT NOT NULL, state TEXT NOT NULL,
 created_at_ms INTEGER NOT NULL, expires_at_ms INTEGER, boot_epoch TEXT NOT NULL,
 UNIQUE(request_digest)
);
CREATE TABLE IF NOT EXISTS approval_nonces (
 nonce TEXT PRIMARY KEY, request_id TEXT NOT NULL REFERENCES requests(request_id),
 state TEXT NOT NULL, issued_at_ms INTEGER NOT NULL, expires_at_ms INTEGER NOT NULL,
 UNIQUE(request_id)
);
CREATE TABLE IF NOT EXISTS audit_events (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE,
 event_json BLOB NOT NULL, previous_hash TEXT NOT NULL, event_hash TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS requests_actor_state ON requests(actor_id,state);
CREATE INDEX IF NOT EXISTS requests_expiry ON requests(expires_at_ms,state);
"""

class SafetyStore:
    """Durable, single-owner ledger. Bootstrap explicitly permits initial creation.

    Cooperating processes must use the same ledger path and its persistent .lock
    sidecar. Never unlink that sidecar, even after closing the store.
    """
    def __init__(self, path: str | os.PathLike[str] | None = None, *, bootstrap=False,
                 clock: Callable[[], int] | None = None, busy_timeout_ms: int = 500):
        self.path = (Path(path) if path else self.default_path()).resolve()
        self.clock = clock or (lambda: int(time.time() * 1000))
        self.busy_timeout_ms = max(1, min(int(busy_timeout_ms), 10_000))
        self.boot_epoch = secrets.token_urlsafe(18)
        self._lock = RLock()
        self._conn = None
        self._owner_file = None
        self._closed = False
        self._poisoned = False
        with self._lock:
            try:
                if bootstrap:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                elif not self.path.is_file():
                    raise StoreUnavailable("established database is missing")
                # Ownership must precede opening/reconciling live approvals.
                self._acquire_owner()
                new = not self.path.exists()
                if new:
                    if not bootstrap:
                        raise StoreUnavailable("established database is missing")
                    fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o666)
                    os.close(fd)
                self._identity = self._file_identity()
                # mode=rw never silently recreates a deleted established ledger.
                self._conn = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True,
                                             timeout=self.busy_timeout_ms / 1000,
                                             isolation_level=None, check_same_thread=False)
                self._conn.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
                self._conn.execute("PRAGMA foreign_keys=ON")
                self._conn.execute("PRAGMA synchronous=FULL")
                if new:
                    self._conn.executescript(_SCHEMA)
                with self._access(write=True):
                    self._check_integrity()
                    columns = {r[1] for r in self._conn.execute("PRAGMA table_info(approval_nonces)")}
                    if "required_operator_role" not in columns:
                        self._conn.execute("ALTER TABLE approval_nonces ADD COLUMN required_operator_role TEXT NOT NULL DEFAULT 'human_operator'")
                    self._conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('boot_epoch',?)", (self.boot_epoch,))
                    for nonce, request_id in self._conn.execute("SELECT nonce,request_id FROM approval_nonces WHERE state='PENDING'").fetchall():
                        self._conn.execute("UPDATE approval_nonces SET state='INVALIDATED' WHERE nonce=?", (nonce,))
                        self._event("APPROVAL_INVALIDATED", {"request_id": request_id, "reason": "restart"})
                    for (request_id,) in self._conn.execute("SELECT request_id FROM requests WHERE state='AWAITING_APPROVAL'").fetchall():
                        self._conn.execute("UPDATE requests SET state='EXPIRED' WHERE request_id=?", (request_id,))
                        self._event("REQUEST_EXPIRED", {"request_id": request_id, "reason": "restart"})
                    for (request_id,) in self._conn.execute("SELECT request_id FROM requests WHERE state='EXECUTING'").fetchall():
                        self._conn.execute("UPDATE requests SET state='UNKNOWN' WHERE request_id=?", (request_id,))
                        self._event("REQUEST_UNKNOWN", {"request_id": request_id, "reason": "restart", "partial_effects": True})
            except BaseException as exc:
                self.close()
                if isinstance(exc, (sqlite3.Error, OSError)):
                    raise self._map_error(exc) from exc
                raise

    @staticmethod
    def default_path() -> Path:
        root = os.environ.get("SAFEMAV_RUNTIME_DIR")
        if root: return Path(root) / "ledger.sqlite3"
        if os.name == "nt": return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "SafeMAV" / "ledger.sqlite3"
        return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "safemav" / "ledger.sqlite3"

    def _acquire_owner(self):
        owner = open(str(self.path) + ".lock", "a+b")
        try:
            owner.seek(0)
            if os.name == "nt":
                import msvcrt
                # Windows allows byte locks beyond EOF; no initialization race.
                msvcrt.locking(owner.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(owner.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            owner.close()
            raise StoreBusy("ledger already has an owner or cannot be locked") from exc
        self._owner_file = owner

    def _file_identity(self):
        stat = self.path.stat()
        return stat.st_dev, stat.st_ino

    def _ensure_available(self):
        if self._closed or self._poisoned or self._conn is None:
            raise StoreUnavailable("durable store is closed or unavailable")
        if not self.path.is_file() or self._file_identity() != self._identity:
            self._poisoned = True
            raise StoreUnavailable("database is missing or replaced")

    @contextmanager
    def _access(self, *, write=False):
        """Serialize SQLite operations, translate I/O failures, always unwind."""
        with self._lock:
            begun = False
            try:
                self._ensure_available()
                if write:
                    self._conn.execute("BEGIN IMMEDIATE")
                    begun = True
                yield self._conn
                self._ensure_available()
                if begun:
                    self._conn.commit()
            except BaseException as exc:
                if begun:
                    try:
                        self._conn.rollback()
                    except sqlite3.Error as rollback_exc:
                        self._poisoned = True
                        raise StoreUnavailable("database rollback failed") from rollback_exc
                if isinstance(exc, (sqlite3.Error, OSError)):
                    raise self._map_error(exc) from exc
                raise

    def _check_integrity(self):
        # Callers hold _lock, and a write transaction for boot/sink checks.
        self._ensure_available()
        if self._conn.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise StoreCorrupt("database integrity check failed")
        tables = {r[0] for r in self._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"meta", "requests", "approval_nonces", "audit_events"}.issubset(tables):
            raise StoreCorrupt("database schema is absent")
        if self._conn.execute("PRAGMA foreign_key_check").fetchone():
            raise StoreCorrupt("database references are corrupt")
        if not self.verify_audit(self._audit_rows()):
            raise StoreCorrupt("audit integrity check failed")

    @staticmethod
    def _map_error(exc):
        msg = str(exc).lower()
        if "locked" in msg or "busy" in msg: return StoreBusy("database is busy")
        if "malformed" in msg or "corrupt" in msg: return StoreCorrupt("database is corrupt")
        if isinstance(exc, sqlite3.IntegrityError): return AlreadyExists("ledger constraint rejected the write")
        return StoreUnavailable("durable store unavailable")

    def close(self):
        with self._lock:
            try:
                if self._conn is not None:
                    self._conn.close()
            except sqlite3.Error as exc:
                raise self._map_error(exc) from exc
            finally:
                self._closed = True
                if self._owner_file is not None:
                    # Descriptor close releases flock / Windows byte locks.
                    self._owner_file.close()
                    self._owner_file = None

    def __enter__(self): return self
    def __exit__(self, *_): self.close()

    def _event(self, kind: str, data: dict[str, Any], *, conn=None) -> str:
        c = conn or self._conn
        previous = c.execute("SELECT event_hash FROM audit_events ORDER BY sequence DESC LIMIT 1").fetchone()
        previous_hash = previous[0] if previous else "0" * 64
        event_id = str(uuid.uuid4()); body = {"event_id":event_id,"kind":kind,"at_utc_ms":self.clock(),"data":data}
        raw = canonical_bytes(body); digest = hashlib.sha256(previous_hash.encode() + raw).hexdigest()
        c.execute("INSERT INTO audit_events(event_id,event_json,previous_hash,event_hash) VALUES(?,?,?,?)",
                  (event_id, raw, previous_hash, digest))
        return event_id

    def record_event(self, kind: str, data: dict[str, Any]) -> str:
        with self._access(write=True):
            return self._event(kind, data)

    def check_writable(self, request_id: str, sink: str | None = None) -> str:
        """Commit a pre-sink check, not evidence of an RPC or flight outcome."""
        with self._access(write=True) as conn:
            self._check_integrity()
            row = conn.execute("SELECT state FROM requests WHERE request_id=?", (request_id,)).fetchone()
            if not row or row[0] != "EXECUTING":
                raise ClaimRejected("sink requires a persisted EXECUTING request")
            data = {"request_id": request_id}
            if sink is not None:
                data["sink"] = sink
            return self._event("SINK_CHECK", data)

    def create_approval(self, request: FrozenRequest, *, expires_at_utc_ms: int, required_operator_role: str,
                        nonce: str | None = None, issued_at_utc_ms: int | None = None) -> ApprovalRecord:
        """Persist the approval window.

        ``issued_at_utc_ms`` must be the instant the caller already told the
        operator about. Re-reading the clock here would store a timestamp a
        millisecond later than the one that was signed, and verification would
        reject an entirely valid approval.
        """
        nonce = nonce or secrets.token_urlsafe(32)
        with self._access(write=True) as conn:
            now = self.clock() if issued_at_utc_ms is None else issued_at_utc_ms
            if type(now) is not int or now < 0:
                raise ClaimRejected("approval issue time is invalid")
            if type(expires_at_utc_ms) is not int or expires_at_utc_ms <= now:
                raise ClaimRejected("approval already expired or invalid")
            if not isinstance(required_operator_role, str) or not required_operator_role.strip():
                raise ClaimRejected("approval role is required")
            if request_digest(request) != request.request_digest:
                raise ClaimRejected("request digest mismatch")
            payload = {f.name: getattr(request, f.name) for f in fields(FrozenRequest)}
            conn.execute("INSERT INTO requests VALUES(?,?,?,?,?,?,?,?,?)", (request.request_id,canonical_bytes(payload),request.request_digest,request.actor_id,request.vehicle_id,"AWAITING_APPROVAL",now,expires_at_utc_ms,request.boot_epoch))
            conn.execute("INSERT INTO approval_nonces(nonce,request_id,state,issued_at_ms,expires_at_ms,required_operator_role) VALUES(?,?,?,?,?,?)", (nonce,request.request_id,"PENDING",now,expires_at_utc_ms,required_operator_role))
            self._event("APPROVAL_REQUESTED", {"request_id":request.request_id,"nonce_hash":hashlib.sha256(nonce.encode()).hexdigest(),"request_digest":request.request_digest})
        return ApprovalRecord(request.request_id,nonce,expires_at_utc_ms,required_operator_role,issued_at_utc_ms=now)

    def get_request(self, request_id: str) -> tuple[dict[str,Any], str, str] | None:
        with self._access() as conn:
            row = conn.execute("SELECT request_json,request_digest,state FROM requests WHERE request_id=?", (request_id,)).fetchone()
            if not row:
                return None
            try:
                payload = parse_canonical_json(row[0])
                if not isinstance(payload, dict):
                    raise ValueError("request is not an object")
            except (ValueError, TypeError, RecursionError) as exc:
                raise StoreCorrupt("stored request is malformed") from exc
            return payload, row[1], row[2]

    def get_approval_record(self, request_id: str) -> ApprovalRecord | None:
        with self._access() as conn:
            row = conn.execute("SELECT request_id,nonce,expires_at_ms,required_operator_role,state,issued_at_ms FROM approval_nonces WHERE request_id=?", (request_id,)).fetchone()
            return ApprovalRecord(*row) if row else None

    def get_nonce_binding(self, nonce: str) -> tuple[str, str, int, int] | None:
        """Return nonce request, state, issue and expiry without exposing SQLite."""
        with self._access() as conn:
            row = conn.execute("SELECT request_id,state,issued_at_ms,expires_at_ms FROM approval_nonces WHERE nonce=?", (nonce,)).fetchone()
            return tuple(row) if row else None

    def request_boot_epoch(self, request_id: str) -> str | None:
        with self._access() as conn:
            row = conn.execute("SELECT boot_epoch FROM requests WHERE request_id=?", (request_id,)).fetchone()
            return row[0] if row else None

    def has_uncertain_execution(self, vehicle_id: str) -> bool:
        with self._access() as conn:
            return conn.execute("SELECT 1 FROM requests WHERE vehicle_id=? AND state IN ('UNKNOWN','EXECUTING') LIMIT 1", (vehicle_id,)).fetchone() is not None

    def claim_and_intent(self, *, request_id: str, nonce: str, boot_epoch: str) -> ClaimResult:
        rejected = False
        with self._access(write=True) as conn:
            now = self.clock()
            row = conn.execute("SELECT expires_at_ms,state FROM approval_nonces WHERE nonce=? AND request_id=?", (nonce,request_id)).fetchone()
            req = conn.execute("SELECT boot_epoch,state,expires_at_ms FROM requests WHERE request_id=?", (request_id,)).fetchone()
            if not row or not req: raise ClaimRejected("approval record not found")
            if row[1] != "PENDING": raise ClaimRejected("approval nonce is not pending")
            if req[1] != "AWAITING_APPROVAL": raise ClaimRejected("request is not awaiting approval")
            if req[2] != row[0]: raise StoreCorrupt("approval deadline mismatch")
            if req[0] != boot_epoch or now >= row[0]:
                state = "INVALIDATED" if req[0] != boot_epoch else "EXPIRED"
                conn.execute("UPDATE approval_nonces SET state=? WHERE nonce=?", (state,nonce))
                conn.execute("UPDATE requests SET state='EXPIRED' WHERE request_id=?", (request_id,))
                self._event("APPROVAL_" + state, {"request_id":request_id,"reason":"epoch" if state == "INVALIDATED" else "expiry"})
                rejected = True
            else:
                conn.execute("UPDATE approval_nonces SET state='CONSUMED' WHERE nonce=? AND state='PENDING'", (nonce,))
                conn.execute("UPDATE requests SET state='EXECUTING' WHERE request_id=?", (request_id,))
                event = self._event("EXECUTION_INTENT", {"request_id":request_id,"nonce_hash":hashlib.sha256(nonce.encode()).hexdigest()})
        if rejected:
            # Invalidation committed; rejection cannot leave a transaction open.
            raise ClaimRejected("approval epoch or expiry invalid")
        return ClaimResult(request_id,nonce,event,now)

    def set_state(self, request_id: str, state: str, *, partial_effects=False) -> str:
        # ACKNOWLEDGED is RPC acceptance only, never observed flight completion.
        allowed={"PROPOSED","AWAITING_APPROVAL","AUTHORIZED","EXECUTING","ACKNOWLEDGED","COMPLETED","DENIED","EXPIRED","FAILED","UNKNOWN"}
        transitions={"PROPOSED":{"AWAITING_APPROVAL","DENIED","EXPIRED"},"AWAITING_APPROVAL":{"EXECUTING","DENIED","EXPIRED"},"EXECUTING":{"ACKNOWLEDGED","COMPLETED","FAILED","UNKNOWN"}}
        if state not in allowed: raise ValueError("invalid request state")
        with self._access(write=True) as conn:
            current = conn.execute("SELECT state FROM requests WHERE request_id=?", (request_id,)).fetchone()
            if not current: raise NotFound("request not found")
            if state != current[0] and state not in transitions.get(current[0], set()):
                raise ClaimRejected(f"invalid state transition {current[0]} -> {state}")
            conn.execute("UPDATE requests SET state=? WHERE request_id=?", (state,request_id))
            if state in {"DENIED", "EXPIRED"}:
                conn.execute("UPDATE approval_nonces SET state='INVALIDATED' WHERE request_id=? AND state='PENDING'", (request_id,))
            return self._event("REQUEST_"+state, {"request_id":request_id,"partial_effects":bool(partial_effects)})

    def sweep_expired(self) -> int:
        with self._access(write=True) as conn:
            rows = conn.execute("SELECT nonce,request_id FROM approval_nonces WHERE state='PENDING' AND expires_at_ms<=?", (self.clock(),)).fetchall()
            for nonce,request_id in rows:
                conn.execute("UPDATE approval_nonces SET state='EXPIRED' WHERE nonce=?", (nonce,))
                conn.execute("UPDATE requests SET state='EXPIRED' WHERE request_id=? AND state='AWAITING_APPROVAL'", (request_id,))
                self._event("APPROVAL_EXPIRED", {"request_id":request_id})
            return len(rows)

    def _audit_rows(self):
        try:
            return [parse_canonical_json(row[0]) | {"previous_hash":row[1],"event_hash":row[2]} for row in self._conn.execute("SELECT event_json,previous_hash,event_hash FROM audit_events ORDER BY sequence")]
        except (ValueError, TypeError, RecursionError) as exc:
            raise StoreCorrupt("stored audit is malformed") from exc

    def export_audit(self) -> list[dict[str,Any]]:
        with self._access():
            return self._audit_rows()

    @staticmethod
    def verify_audit(events: list[dict[str,Any]]) -> bool:
        previous="0"*64
        try:
            for event in events:
                expected=hashlib.sha256(previous.encode()+canonical_bytes({"event_id":event["event_id"],"kind":event["kind"],"at_utc_ms":event["at_utc_ms"],"data":event["data"]})).hexdigest()
                if event.get("previous_hash")!=previous or event.get("event_hash")!=expected:return False
                previous=expected
        except (KeyError, TypeError, ValueError, RecursionError, AttributeError):
            return False
        return True
