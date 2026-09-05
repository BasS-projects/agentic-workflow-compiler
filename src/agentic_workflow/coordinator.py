"""Durable HTTP-worker queue with expiring leases and fenced state changes.

A lease fences coordinator writes, not third-party effects. Unknown outcomes stop
in ``needs_recovery`` unless the submitter explicitly declared replay safe.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import time
import uuid

from .dispatch import validate_any, bind_inputs


class ConflictError(RuntimeError):
    """Operation conflicts with a durable job state or immutable identity."""


class LeaseLostError(ConflictError):
    """Lease is expired, revoked, or belongs to a different worker."""


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _id(value, name="run_id"):
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise ValueError(f"{name} must be a nonempty string of at most 256 characters")
    return value


def _positive(value, name, maximum=86400):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value <= maximum:
        raise ValueError(f"{name} must be > 0 and <= {maximum}")
    return float(value)


def _validate(workflow, inputs):
    if isinstance(workflow, dict) and "bundle_version" in workflow:
        from .compilation import verify_bundle
        workflow = verify_bundle(workflow)
    workflow = validate_any(workflow)
    inputs = bind_inputs(workflow, inputs)
    return json.loads(_json(workflow)), json.loads(_json(inputs))


class Coordinator:
    def __init__(self, db_path):
        if str(db_path) == ":memory:":
            raise ValueError("Coordinator requires a durable SQLite file")
        self.db_path = Path(db_path).expanduser().resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as con:
            con.execute("PRAGMA journal_mode=WAL")
            con.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    run_id TEXT PRIMARY KEY, workflow_json TEXT NOT NULL,
                    inputs_json TEXT NOT NULL, status TEXT NOT NULL,
                    actor TEXT NOT NULL, replay_safe INTEGER NOT NULL,
                    worker_id TEXT, required_worker_id TEXT,
                    lease_token INTEGER NOT NULL DEFAULT 0, lease_until REAL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    recovery_requested INTEGER NOT NULL DEFAULT 0,
                    result_json TEXT, decisions_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS coordinator_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT,
                    event TEXT NOT NULL, actor TEXT NOT NULL,
                    details_json TEXT NOT NULL, created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS schedules (
                    schedule_id TEXT PRIMARY KEY, workflow_json TEXT NOT NULL,
                    inputs_json TEXT NOT NULL, interval_seconds REAL NOT NULL,
                    actor TEXT NOT NULL, next_at REAL NOT NULL,
                    created_at REAL NOT NULL
                );
            """)

    @contextmanager
    def _connect(self, write=False):
        con = sqlite3.connect(str(self.db_path), timeout=30)
        con.row_factory = sqlite3.Row
        try:
            if write:
                con.execute("BEGIN IMMEDIATE")
            with con:
                yield con
        finally:
            con.close()

    @staticmethod
    def _event(con, run_id, event, actor, details=None):
        con.execute("INSERT INTO coordinator_events(run_id,event,actor,details_json,created_at) VALUES(?,?,?,?,?)",
                    (run_id, event, actor, _json(details or {}), time.time()))

    @staticmethod
    def _row(con, run_id):
        row = con.execute("SELECT * FROM jobs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        return row

    @staticmethod
    def _public(row):
        result = dict(row)
        for field in ("workflow", "inputs", "result", "decisions"):
            raw = result.pop(field + "_json")
            result[field] = json.loads(raw) if raw is not None else None
        result["replay_safe"] = bool(result["replay_safe"])
        result["recovery_requested"] = bool(result["recovery_requested"])
        result["resume"] = result["attempts"] > 1
        result["approvals"] = (result["result"] or {}).get("approvals", [])
        return result

    def _submit(self, con, workflow, inputs, run_id, actor, replay_safe, now):
        old = con.execute("SELECT * FROM jobs WHERE run_id=?", (run_id,)).fetchone()
        if old:
            if (old["workflow_json"], old["inputs_json"], bool(old["replay_safe"])) != (_json(workflow), _json(inputs), replay_safe):
                raise ConflictError("run_id already has different immutable workflow, inputs, or replay policy")
            return self._public(old)
        con.execute("INSERT INTO jobs(run_id,workflow_json,inputs_json,status,actor,replay_safe,created_at,updated_at) VALUES(?,?,?,'queued',?,?,?,?)",
                    (run_id, _json(workflow), _json(inputs), actor, replay_safe, now, now))
        self._event(con, run_id, "submitted", actor, {"replay_safe": replay_safe})
        return self._public(self._row(con, run_id))

    def submit(self, workflow, inputs, run_id=None, actor="local", replay_safe=False):
        workflow, inputs = _validate(workflow, inputs)
        run_id = _id(run_id or str(uuid.uuid4()))
        _id(actor, "actor")
        if type(replay_safe) is not bool:
            raise ValueError("replay_safe must be boolean")
        with self._connect(write=True) as con:
            return self._submit(con, workflow, inputs, run_id, actor, replay_safe, time.time())

    def get(self, run_id):
        self.expire()
        with self._connect() as con:
            return self._public(self._row(con, run_id))

    def list_runs(self, limit=100):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("limit must be an integer between 1 and 1000")
        self.expire()
        with self._connect() as con:
            return [self._public(row) for row in con.execute("SELECT * FROM jobs ORDER BY created_at DESC,run_id LIMIT ?", (limit,))]

    def _expire(self, con, now):
        expired = con.execute("SELECT * FROM jobs WHERE status='running' AND lease_until<=?", (now,)).fetchall()
        for row in expired:
            status = "queued" if row["replay_safe"] else "needs_recovery"
            con.execute("UPDATE jobs SET status=?,lease_until=NULL,lease_token=lease_token+1,required_worker_id=NULL,recovery_requested=1,error=?,updated_at=? WHERE run_id=?",
                        (status, "Lease expired; prior external effects may have completed", now, row["run_id"]))
            self._event(con, row["run_id"], "lease_expired", "coordinator", {"worker_id": row["worker_id"], "status": status})
        return len(expired)

    def expire(self, now=None):
        with self._connect(write=True) as con:
            return self._expire(con, time.time() if now is None else float(now))

    def claim(self, worker_id, lease_seconds=60):
        _id(worker_id, "worker_id")
        lease_seconds = _positive(lease_seconds, "lease_seconds", 3600)
        with self._connect(write=True) as con:
            now = time.time()
            self._expire(con, now)
            row = con.execute("SELECT * FROM jobs WHERE status='queued' AND (required_worker_id IS NULL OR required_worker_id=?) ORDER BY created_at,run_id LIMIT 1", (worker_id,)).fetchone()
            if row is None:
                return None
            con.execute("UPDATE jobs SET status='running',worker_id=?,lease_token=lease_token+1,lease_until=?,attempts=attempts+1,updated_at=? WHERE run_id=?",
                        (worker_id, now + lease_seconds, now, row["run_id"]))
            self._event(con, row["run_id"], "claimed", worker_id)
            return self._public(self._row(con, row["run_id"]))

    @staticmethod
    def _lease(row, lease_token, worker_id, now):
        if type(lease_token) is not int or row["status"] != "running" or row["lease_token"] != lease_token or row["worker_id"] != worker_id or row["lease_until"] is None or row["lease_until"] <= now:
            raise LeaseLostError("Lease lost: state, token, worker, or deadline no longer matches")

    def heartbeat(self, run_id, lease_token, worker_id, lease_seconds=60):
        lease_seconds = _positive(lease_seconds, "lease_seconds", 3600)
        with self._connect(write=True) as con:
            now = time.time()
            row = self._row(con, run_id)
            self._lease(row, lease_token, worker_id, now)
            con.execute("UPDATE jobs SET lease_until=?,updated_at=? WHERE run_id=?", (now + lease_seconds, now, run_id))
            return {"run_id": run_id, "status": "running", "lease_token": lease_token, "lease_until": now + lease_seconds}

    def complete(self, run_id, lease_token, worker_id, result):
        if not isinstance(result, dict) or result.get("status") not in {"completed", "failed", "waiting_approval", "cancelled"}:
            raise ValueError("result requires completed, failed, waiting_approval, or cancelled status")
        _json(result)
        if result.get("run_id", run_id) != run_id:
            raise ValueError("result run_id differs from leased run")
        if result["status"] == "waiting_approval":
            approvals = result.get("approvals")
            if not isinstance(approvals, list) or not approvals or any(not isinstance(a, dict) or not isinstance(a.get("step_path"), str) or not isinstance(a.get("prompt"), str) for a in approvals):
                raise ValueError("waiting_approval result requires approval paths and prompts")
        with self._connect(write=True) as con:
            self._lease(self._row(con, run_id), lease_token, worker_id, time.time())
            con.execute("UPDATE jobs SET status=?,result_json=?,error=?,required_worker_id=?,lease_until=NULL,recovery_requested=0,updated_at=? WHERE run_id=?",
                        (result["status"], _json(result), result.get("error"), worker_id, time.time(), run_id))
            self._event(con, run_id, result["status"], worker_id)
            return self._public(self._row(con, run_id))

    def fail(self, run_id, lease_token, worker_id, error):
        """Executor/protocol failure is uncertain, unlike a recorded runtime failure."""
        if not isinstance(error, str):
            raise ValueError("error must be a string")
        with self._connect(write=True) as con:
            self._lease(self._row(con, run_id), lease_token, worker_id, time.time())
            con.execute("UPDATE jobs SET status='needs_recovery',error=?,lease_until=NULL,recovery_requested=1,updated_at=? WHERE run_id=?", (error[:8192], time.time(), run_id))
            self._event(con, run_id, "executor_failed", worker_id, {"error": error[:8192]})
            return self._public(self._row(con, run_id))

    def cancel(self, run_id, actor="local"):
        with self._connect(write=True) as con:
            row = self._row(con, run_id)
            if row["status"] in {"completed", "cancelled"}:
                return self._public(row)
            con.execute("UPDATE jobs SET status='cancelled',lease_token=lease_token+1,lease_until=NULL,updated_at=? WHERE run_id=?", (time.time(), run_id))
            self._event(con, run_id, "cancelled", actor)
            return self._public(self._row(con, run_id))

    def approve(self, run_id, step_path, approved, actor="local"):
        if type(approved) is not bool:
            raise ValueError("approved must be boolean")
        with self._connect(write=True) as con:
            row = self._row(con, run_id)
            decisions = json.loads(row["decisions_json"])
            decision = {"approved": approved, "actor": actor}
            if step_path in decisions:
                if decisions[step_path] != decision:
                    raise ConflictError("A recorded approval decision is immutable")
                return self._public(row)
            if row["status"] != "waiting_approval":
                raise ConflictError("Run is not waiting for approval")
            result = json.loads(row["result_json"])
            pending = {a["step_path"] for a in result.get("approvals", [])}
            if step_path not in pending:
                raise ValueError("step_path is not a pending approval")
            decisions[step_path] = decision
            pending -= decisions.keys()
            status = "queued" if not pending or not approved else "waiting_approval"
            con.execute("UPDATE jobs SET status=?,decisions_json=?,updated_at=? WHERE run_id=?", (status, _json(decisions), time.time(), run_id))
            self._event(con, run_id, "approved" if approved else "rejected", actor, {"step_path": step_path})
            return self._public(self._row(con, run_id))

    def recover(self, run_id, actor="local", retry=True):
        if type(retry) is not bool:
            raise ValueError("retry must be boolean")
        if not retry:
            return self.cancel(run_id, actor)
        self.expire()
        with self._connect(write=True) as con:
            row = self._row(con, run_id)
            if row["status"] not in {"needs_recovery", "failed", "waiting_approval"}:
                raise ConflictError("Only uncertain, failed, or unavailable approval-worker runs can be recovered")
            con.execute("UPDATE jobs SET status='queued',required_worker_id=NULL,lease_token=lease_token+1,lease_until=NULL,recovery_requested=1,error=NULL,updated_at=? WHERE run_id=?", (time.time(), run_id))
            self._event(con, run_id, "recovery_authorized", actor, {"retry": True, "external_effects_may_repeat": True})
            return self._public(self._row(con, run_id))

    def add_schedule(self, workflow, inputs, interval_seconds, actor="local", schedule_id=None):
        workflow, inputs = _validate(workflow, inputs)
        interval = _positive(interval_seconds, "interval_seconds", 366 * 86400)
        schedule_id = _id(schedule_id or str(uuid.uuid4()), "schedule_id")
        now = time.time()
        with self._connect(write=True) as con:
            if con.execute("SELECT 1 FROM schedules WHERE schedule_id=?", (schedule_id,)).fetchone():
                raise ConflictError("schedule_id already exists")
            con.execute("INSERT INTO schedules VALUES(?,?,?,?,?,?,?)", (schedule_id, _json(workflow), _json(inputs), interval, actor, now + interval, now))
            self._event(con, None, "schedule_created", actor, {"schedule_id": schedule_id})
        return next(s for s in self.list_schedules() if s["schedule_id"] == schedule_id)

    def list_schedules(self):
        with self._connect() as con:
            result = []
            for row in con.execute("SELECT * FROM schedules ORDER BY created_at,schedule_id"):
                item = dict(row)
                item["workflow"] = json.loads(item.pop("workflow_json"))
                item["inputs"] = json.loads(item.pop("inputs_json"))
                result.append(item)
            return result

    def tick(self, now=None):
        now = time.time() if now is None else float(now)
        if not math.isfinite(now):
            raise ValueError("now must be finite")
        runs = []
        with self._connect(write=True) as con:
            self._expire(con, now)
            for row in con.execute("SELECT * FROM schedules WHERE next_at<=? ORDER BY next_at,schedule_id", (now,)).fetchall():
                due = row["next_at"]
                # Coalesce missed intervals into one occurrence; preserve cadence.
                steps = math.floor((now - due) / row["interval_seconds"]) + 1
                next_at = due + steps * row["interval_seconds"]
                key = hashlib.sha256(f"{row['schedule_id']}:{due:.9f}".encode()).hexdigest()
                run = self._submit(con, json.loads(row["workflow_json"]), json.loads(row["inputs_json"]), "scheduled-" + key, row["actor"], False, now)
                con.execute("UPDATE schedules SET next_at=? WHERE schedule_id=?", (next_at, row["schedule_id"]))
                self._event(con, run["run_id"], "schedule_triggered", "scheduler", {"schedule_id": row["schedule_id"], "due_at": due})
                runs.append(run)
        return runs

    def events(self, run_id=None, limit=1000):
        if type(limit) is not int or not 1 <= limit <= 10000:
            raise ValueError("limit must be between 1 and 10000")
        with self._connect() as con:
            rows = con.execute("SELECT * FROM coordinator_events WHERE (? IS NULL OR run_id=?) ORDER BY id DESC LIMIT ?", (run_id, run_id, limit)).fetchall()
            result = []
            for row in reversed(rows):
                event = dict(row)
                event["details"] = json.loads(event.pop("details_json"))
                result.append(event)
            return result

    def metrics(self):
        self.expire()
        with self._connect() as con:
            counts = {row["status"]: row["n"] for row in con.execute("SELECT status,count(*) n FROM jobs GROUP BY status")}
            return {"runs": counts, "runs_total": sum(counts.values()), "schedules_total": con.execute("SELECT count(*) FROM schedules").fetchone()[0], "events_total": con.execute("SELECT count(*) FROM coordinator_events").fetchone()[0]}

    def close(self):
        """Connections are scoped to each operation; retained for API symmetry."""
