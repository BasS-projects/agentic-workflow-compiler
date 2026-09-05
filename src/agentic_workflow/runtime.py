"""A sequential, durable local runtime with isolated, time-limited workers.

This runtime requires POSIX (Linux/macOS). ``max_attempts`` limits automatic
attempts in each explicit run/resume invocation; cumulative attempts are stored.
After a process interruption, reconcile any external effects, call
``recover(run_id, policy='retry')``, then explicitly resume. Completed steps are
never repeated. Tool/provider implementation identity is not version-pinned.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import signal
import sqlite3
import tempfile
import time
from typing import Any
import uuid

from .ir import validate_inputs, validate_json, validate_workflow
from .worker import TaskContext, default_tools


class RunBusyError(RuntimeError):
    """Another executor or its surviving worker holds this run's lock."""


class RecoveryRequiredError(RuntimeError):
    """An interrupted run has effects that need explicit reconciliation."""


class IdentityMismatchError(ValueError):
    """A resume request changes the workflow, inputs, or workspace."""


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _child_entry(tool, args: dict, context: TaskContext, result_path: str) -> None:
    """Write the result to disk so large IPC messages cannot defeat timeouts."""
    os.setsid()
    try:
        result = tool(args, context)
        if type(result) is not dict:
            raise TypeError("Tool output must be a JSON object")
        validate_json(result, "tool output")
        payload = {"ok": True, "result": result}
        serialized = _json(payload)
    except BaseException as exc:
        serialized = _json({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
    temporary = result_path + ".partial"
    with open(temporary, "w", encoding="utf-8") as stream:
        stream.write(serialized)
    os.replace(temporary, result_path)


def _stop_worker(process: multiprocessing.Process) -> None:
    """Terminate the worker's session, including subprocesses it created."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        if process.is_alive():
            process.terminate()
    process.join(0.2)
    # Descendants can survive even if the worker already exited on SIGTERM.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        if process.is_alive():
            process.kill()
    process.join()


def _execute(tool, args: dict, context: TaskContext, timeout: float) -> dict:
    with tempfile.TemporaryDirectory(prefix="agentic-worker-") as directory:
        result_path = str(Path(directory) / "result.json")
        process = multiprocessing.get_context("fork").Process(
            target=_child_entry, args=(tool, args, context, result_path)
        )
        try:
            deadline = time.monotonic() + timeout
            process.start()
            # join(timeout) waits on a sentinel pipe that a tool's os.fork child
            # can inherit. Poll direct process death instead, so descendants
            # cannot postpone cleanup after their parent has returned.
            while process.is_alive() and time.monotonic() < deadline:
                time.sleep(min(0.01, max(0, deadline - time.monotonic())))
            if process.is_alive():
                _stop_worker(process)
                raise TimeoutError(f"Step exceeded timeout of {timeout:g} seconds")
            if process.exitcode != 0 or not Path(result_path).is_file():
                raise RuntimeError(f"Worker exited without a result (exit code {process.exitcode})")
            payload = json.loads(Path(result_path).read_text(encoding="utf-8"))
            if not payload["ok"]:
                raise RuntimeError(payload["error"])
            return payload["result"]
        finally:
            if process.pid is not None:
                # A returning/crashed worker can leave descendants and inherited
                # lock descriptors behind. Workers are strictly synchronous.
                _stop_worker(process)
                process.close()


def _resolve(expression: Any, inputs: dict, results: dict) -> Any:
    if isinstance(expression, dict):
        if set(expression) == {"$ref"}:
            parts = expression["$ref"].split(".")
            if parts[0] == "inputs":
                value = inputs[parts[1]]
                rest = parts[2:]
            else:
                value = results[parts[1]]
                rest = parts[2:]
                if value is None:  # Every field of a skipped step resolves to null.
                    return None
            for field in rest:
                if not isinstance(value, dict) or field not in value:
                    raise ValueError(f"Reference has no field: {expression['$ref']}")
                value = value[field]
            return value
        return {key: _resolve(value, inputs, results) for key, value in expression.items()}
    if isinstance(expression, list):
        return [_resolve(value, inputs, results) for value in expression]
    return expression


def _equals(left: Any, right: Any) -> bool:
    # JSON booleans are not interchangeable with numbers (Python's True == 1).
    if isinstance(left, bool) != isinstance(right, bool):
        return False
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(_equals(a, b) for a, b in zip(left, right))
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(_equals(left[key], right[key]) for key in left)
    return left == right


class Runtime:
    """SQLite persistence plus a per-run process lock; no external dependencies."""

    def __init__(self, db_path, workspace, tools=None, ai_tools=None):
        if os.name != "posix":
            raise RuntimeError("Runtime requires POSIX process isolation (Linux or macOS)")
        if str(db_path) == ":memory:":
            raise ValueError("Use a filesystem SQLite path for durable runtime state")
        self.db_path = Path(db_path).expanduser().resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.workspace = str(Path(workspace).expanduser().resolve())
        Path(self.workspace).mkdir(parents=True, exist_ok=True)
        self.tools = default_tools() if tools is None else dict(tools)
        self.ai_tools = {} if ai_tools is None else dict(ai_tools)
        self.lock_directory = Path(str(self.db_path) + ".locks")
        self.lock_directory.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL,
                    workflow_json TEXT NOT NULL, inputs_json TEXT NOT NULL,
                    workspace TEXT NOT NULL, status TEXT NOT NULL,
                    outputs_json TEXT, error TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS steps (
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    step_id TEXT NOT NULL, position INTEGER NOT NULL,
                    status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    result_json TEXT, error TEXT, idempotency_key TEXT NOT NULL,
                    started_at TEXT, completed_at TEXT,
                    PRIMARY KEY (run_id, step_id)
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    step_id TEXT, event TEXT NOT NULL,
                    details_json TEXT NOT NULL, created_at TEXT NOT NULL
                );
            """)

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(str(self.db_path), timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @contextmanager
    def _run_lock(self, run_id):
        import fcntl

        name = hashlib.sha256(run_id.encode("utf-8")).hexdigest() + ".lock"
        descriptor = os.open(self.lock_directory / name, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RunBusyError(f"Run {run_id!r} is already being executed") from exc
            with self._connect() as connection:
                table = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='a_runs'"
                ).fetchone()
                if table and connection.execute(
                        "SELECT 1 FROM a_runs WHERE run_id=?", (run_id,)).fetchone():
                    raise IdentityMismatchError("Run belongs to IR0.2; use AdvancedRuntime")
            if type(self) is Runtime:
                with self._connect() as connection:
                    table = connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='langgraph_epochs'"
                    ).fetchone()
                    if table and connection.execute(
                            "SELECT 1 FROM langgraph_epochs WHERE run_id=?", (run_id,)).fetchone():
                        raise IdentityMismatchError("Run belongs to LangGraph; use LangGraphRuntime")
            yield
        finally:
            # Closing, rather than explicitly unlocking, preserves exclusion if a
            # forked worker survives an abruptly terminated parent process.
            os.close(descriptor)

    @staticmethod
    def _event(connection, run_id, event, step_id=None, details=None):
        connection.execute(
            "INSERT INTO events (run_id,step_id,event,details_json,created_at) VALUES (?,?,?,?,?)",
            (run_id, step_id, event, _json(details or {}), _now()),
        )

    @staticmethod
    def _check_run_id(run_id):
        if not isinstance(run_id, str) or not run_id.strip() or len(run_id) > 256:
            raise ValueError("run_id must be a nonempty string of at most 256 characters")

    def run(self, workflow, inputs, run_id=None, resume=False) -> dict:
        """Execute; failed results are persisted and can be explicitly resumed.

        Validation and state/identity errors raise. Tool, reference, condition,
        and output evaluation errors return ``status='failed'`` with ``error``.
        """
        workflow = json.loads(_json(validate_workflow(workflow)))
        inputs = json.loads(_json(validate_inputs(workflow, inputs)))
        workflow_json, inputs_json = _json(workflow), _json(inputs)
        if resume and run_id is None:
            raise ValueError("resume requires run_id")
        run_id = str(uuid.uuid4()) if run_id is None else run_id
        self._check_run_id(run_id)
        with self._run_lock(run_id):
            with self._connect() as connection:
                existing = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
                if existing is not None:
                    if not resume:
                        raise ValueError("run_id already exists; use resume=True")
                    if (existing["workflow_json"], existing["inputs_json"], existing["workspace"]) != (
                        workflow_json, inputs_json, self.workspace
                    ):
                        raise IdentityMismatchError("Resume must use the original workflow, inputs, and workspace")
                    if existing["status"] == "running":
                        raise RecoveryRequiredError(
                            "Run was interrupted; reconcile effects, call recover(run_id, policy='retry'), then resume"
                        )
                    if existing["status"] == "completed":
                        return {"run_id": run_id, "status": "completed", "outputs": json.loads(existing["outputs_json"])}
                    connection.execute(
                        "UPDATE runs SET status='running', error=NULL, updated_at=? WHERE run_id=?",
                        (_now(), run_id),
                    )
                    self._event(connection, run_id, "run_resumed")
                else:
                    if resume:
                        raise ValueError(f"Cannot resume unknown run {run_id!r}")
                    now = _now()
                    connection.execute(
                        "INSERT INTO runs VALUES (?,?,?,?,?,'running',NULL,NULL,?,?)",
                        (run_id, workflow["id"], workflow_json, inputs_json, self.workspace, now, now),
                    )
                    for position, step in enumerate(workflow["steps"]):
                        key = hashlib.sha256(_json([run_id, step["id"]]).encode("utf-8")).hexdigest()
                        connection.execute(
                            "INSERT INTO steps (run_id,step_id,position,status,idempotency_key) VALUES (?,?,?,'pending',?)",
                            (run_id, step["id"], position, key),
                        )
                    self._event(connection, run_id, "run_started")

            results = {}
            active_step = None
            try:
                for step in workflow["steps"]:
                    active_step = step["id"]
                    with self._connect() as connection:
                        state = connection.execute(
                            "SELECT * FROM steps WHERE run_id=? AND step_id=?", (run_id, active_step)
                        ).fetchone()
                    if state["status"] in {"completed", "skipped"}:
                        results[active_step] = json.loads(state["result_json"]) if state["result_json"] else None
                        continue
                    if "when" in step:
                        left, right = [_resolve(value, inputs, results) for value in step["when"]["equals"]]
                        if not _equals(left, right):
                            with self._connect() as connection:
                                connection.execute(
                                    "UPDATE steps SET status='skipped',result_json='null',error=NULL,completed_at=? WHERE run_id=? AND step_id=?",
                                    (_now(), run_id, active_step),
                                )
                                self._event(connection, run_id, "step_skipped", active_step)
                            results[active_step] = None
                            continue
                    args = _resolve(step.get("args", {}), inputs, results)
                    registry = self.ai_tools if step["kind"] == "ai" else self.tools
                    if step["tool"] not in registry:
                        raise ValueError(f"Unregistered {step['kind']} tool: {step['tool']}")
                    context = TaskContext(
                        workspace=self.workspace, run_id=run_id, step_id=active_step,
                        idempotency_key=state["idempotency_key"],
                    )
                    retry = step.get("retry", {})
                    max_attempts = retry.get("max_attempts", 1)
                    for attempt in range(1, max_attempts + 1):
                        with self._connect() as connection:
                            connection.execute(
                                "UPDATE steps SET status='running',attempts=attempts+1,error=NULL,started_at=?,completed_at=NULL WHERE run_id=? AND step_id=?",
                                (_now(), run_id, active_step),
                            )
                            self._event(connection, run_id, "step_started", active_step, {"invocation_attempt": attempt})
                        try:
                            result = _execute(registry[step["tool"]], args, context, step.get("timeout_seconds", 30))
                        except Exception as exc:
                            error = f"{type(exc).__name__}: {exc}"
                            with self._connect() as connection:
                                connection.execute(
                                    "UPDATE steps SET status='failed',error=?,completed_at=? WHERE run_id=? AND step_id=?",
                                    (error, _now(), run_id, active_step),
                                )
                                self._event(connection, run_id, "step_failed", active_step, {"error": error})
                            if attempt == max_attempts:
                                raise
                            delay = retry.get("delay_seconds", 0)
                            with self._connect() as connection:
                                self._event(connection, run_id, "step_retry_scheduled", active_step,
                                            {"next_invocation_attempt": attempt + 1, "delay_seconds": delay})
                            time.sleep(delay)
                        else:
                            with self._connect() as connection:
                                connection.execute(
                                    "UPDATE steps SET status='completed',result_json=?,error=NULL,completed_at=? WHERE run_id=? AND step_id=?",
                                    (_json(result), _now(), run_id, active_step),
                                )
                                self._event(connection, run_id, "step_completed", active_step)
                            results[active_step] = result
                            break
                active_step = None
                outputs = _resolve(workflow.get("outputs", {}), inputs, results)
                with self._connect() as connection:
                    connection.execute(
                        "UPDATE runs SET status='completed',outputs_json=?,error=NULL,updated_at=? WHERE run_id=?",
                        (_json(outputs), _now(), run_id),
                    )
                    self._event(connection, run_id, "run_completed")
                return {"run_id": run_id, "status": "completed", "outputs": outputs}
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                with self._connect() as connection:
                    if active_step is not None:
                        connection.execute(
                            "UPDATE steps SET status='failed',error=?,completed_at=? WHERE run_id=? AND step_id=? AND status NOT IN ('completed','skipped')",
                            (error, _now(), run_id, active_step),
                        )
                    connection.execute(
                        "UPDATE runs SET status='failed',error=?,updated_at=? WHERE run_id=?",
                        (error, _now(), run_id),
                    )
                    self._event(connection, run_id, "run_failed", active_step, {"error": error})
                return {"run_id": run_id, "status": "failed", "error": error, "outputs": {}}

    def inspect(self, run_id) -> dict:
        self._check_run_id(run_id)
        with self._connect() as connection:
            run = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if run is None:
                raise KeyError(f"Unknown run: {run_id}")
            steps = connection.execute("SELECT * FROM steps WHERE run_id=? ORDER BY position", (run_id,)).fetchall()
        result = dict(run)
        result["workflow"] = json.loads(result.pop("workflow_json"))
        result["inputs"] = json.loads(result.pop("inputs_json"))
        outputs = result.pop("outputs_json")
        result["outputs"] = json.loads(outputs) if outputs is not None else None
        result["steps"] = []
        for row in steps:
            step = dict(row)
            value = step.pop("result_json")
            step["result"] = json.loads(value) if value is not None else None
            result["steps"].append(step)
        return result

    def events(self, run_id) -> list[dict]:
        self._check_run_id(run_id)
        with self._connect() as connection:
            if connection.execute("SELECT 1 FROM runs WHERE run_id=?", (run_id,)).fetchone() is None:
                raise KeyError(f"Unknown run: {run_id}")
            rows = connection.execute("SELECT * FROM events WHERE run_id=? ORDER BY id", (run_id,)).fetchall()
        result = []
        for row in rows:
            event = dict(row)
            event["details"] = json.loads(event.pop("details_json"))
            result.append(event)
        return result

    def recover(self, run_id, *, policy) -> dict:
        """Acknowledge uncertain effects before retrying an interrupted run.

        Only policy='retry' is supported. This changes state; execution requires
        a separate run(..., resume=True). Never call before reconciling effects
        unless the affected integration actually enforces idempotency keys.
        """
        self._check_run_id(run_id)
        if policy != "retry":
            raise ValueError("Recovery requires explicit policy='retry'")
        with self._run_lock(run_id):
            with self._connect() as connection:
                run = connection.execute("SELECT status FROM runs WHERE run_id=?", (run_id,)).fetchone()
                if run is None:
                    raise KeyError(f"Unknown run: {run_id}")
                if run["status"] != "running":
                    raise ValueError("Only an interrupted running run needs recovery")
                message = "Interrupted execution explicitly acknowledged for retry"
                connection.execute(
                    "UPDATE steps SET status='failed',error=?,completed_at=? WHERE run_id=? AND status='running'",
                    (message, _now(), run_id),
                )
                connection.execute(
                    "UPDATE runs SET status='failed',error=?,updated_at=? WHERE run_id=?", (message, _now(), run_id)
                )
                self._event(connection, run_id, "run_recovered", details={"policy": policy})
            return self.inspect(run_id)

    def close(self):
        """No-op: connections are opened per transaction and always closed."""
