"""Durable structured execution with spawned process isolation and bounded parallelism.

SQLite is local runtime state, not a distributed queue. Every invocation owns an
exclusive executor lock. Children retain shared effect locks so a crashed parent
cannot be recovered while its tools are still running. Only locally registered,
trusted callables are serialized; IR and remote transport remain JSON.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import signal
import sqlite3
import tempfile
import time
import uuid

import cloudpickle

from .advanced_ir import validate_inputs_v2, validate_workflow_v2
from .ir import validate_json
from .runtime import IdentityMismatchError, RecoveryRequiredError, RunBusyError, _equals, _json, _now
from .worker import TaskContext, default_tools


class ApprovalDeniedError(RuntimeError):
    pass


class _Waiting(Exception):
    def __init__(self, approvals):
        self.approvals = approvals


class _Cancelled(Exception):
    pass


def _resolve(expression, inputs, results, loop):
    if isinstance(expression, dict):
        if set(expression) == {"$ref"}:
            ref = expression["$ref"]
            parts = ref.split(".")
            if parts[0] == "inputs":
                value = inputs[parts[1]]
            elif parts[0] == "steps":
                value = results[parts[1]]
                if value is None:  # Preserve skipped v0.1 step-field semantics.
                    return None
            else:
                value = loop[parts[1]]
            for field in parts[2:]:
                if isinstance(value, dict) and field in value:
                    value = value[field]
                elif isinstance(value, list) and field.isdecimal() and int(field) < len(value):
                    value = value[int(field)]
                else:
                    raise ValueError(f"Reference has no field or index: {ref}")
            return value
        return {k: _resolve(v, inputs, results, loop) for k, v in expression.items()}
    if isinstance(expression, list):
        return [_resolve(v, inputs, results, loop) for v in expression]
    return expression


def _spawned_entry(serialized_tool, args, context, result_path, db_path, token, effect_lock):
    """Safe spawn entry point. Never consumes pickle from workflow/API content."""
    import fcntl

    os.setsid()
    descriptor = os.open(effect_lock, os.O_RDWR | os.O_CREAT, 0o600) if effect_lock else None
    try:
        if descriptor is not None:
            fcntl.flock(descriptor, fcntl.LOCK_SH)
        if db_path is not None:
            with sqlite3.connect(db_path, timeout=30) as connection:
                row = connection.execute(
                    "SELECT execution_token,cancel_requested,status FROM a_runs WHERE run_id=?", (context.run_id,)
                ).fetchone()
            if row is None or row[0] != token or row[1] or row[2] != "running":
                raise RuntimeError("Execution fenced before tool invocation")
        tool = cloudpickle.loads(serialized_tool)
        result = tool(args, context)
        if type(result) is not dict:
            raise TypeError("Tool output must be a JSON object")
        validate_json(result, "tool output")
        payload = {"ok": True, "result": result}
    except BaseException as exc:
        payload = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        # Keep the effect lock until after result writing. Descendant processes
        # inherit this FD only if the trusted tool explicitly forks them.
        try:
            temporary = result_path + ".partial"
            Path(temporary).write_text(_json(payload), encoding="utf-8")
            os.replace(temporary, result_path)
        finally:
            if descriptor is not None:
                os.close(descriptor)


async def _stop_process(process):
    if process.pid is None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        if process.is_alive():
            process.terminate()
    deadline = time.monotonic() + 0.2
    while process.is_alive() and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        if process.is_alive():
            process.kill()
    # Poll instead of waiting on inherited sentinel FDs from tool descendants.
    while process.is_alive():
        await asyncio.sleep(0.01)
    process.join(timeout=0)
    process.close()


def execute_isolated(tool, args, context, timeout):
    """Run a trusted callable in a spawned process; usable by backend adapters.

    This low-level helper has timeout isolation but no durable checkpointing.
    Its caller owns persistence and any run-level cancellation policy.
    """
    async def execute():
        with tempfile.TemporaryDirectory(prefix="agentic-isolated-") as directory:
            result_path = str(Path(directory) / "result.json")
            process = multiprocessing.get_context("spawn").Process(
                target=_spawned_entry,
                args=(cloudpickle.dumps(tool), args, context, result_path, None, None, None),
            )
            try:
                process.start()
                deadline = time.monotonic() + timeout
                while process.is_alive():
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"Step exceeded timeout of {timeout:g} seconds")
                    await asyncio.sleep(0.02)
                if process.exitcode != 0 or not Path(result_path).is_file():
                    raise RuntimeError(f"Worker exited without a result (exit code {process.exitcode})")
                payload = json.loads(Path(result_path).read_text(encoding="utf-8"))
                if not payload["ok"]:
                    raise RuntimeError(payload["error"])
                return payload["result"]
            finally:
                await _stop_process(process)
    return asyncio.run(execute())


class AdvancedRuntime:
    """IR0.2 runtime. A new instance may inspect/approve/cancel an active run."""

    def __init__(self, db_path, workspace, tools=None, ai_tools=None):
        if os.name != "posix":
            raise RuntimeError("AdvancedRuntime requires POSIX (Linux/macOS)")
        if str(db_path) == ":memory:":
            raise ValueError("Use a filesystem SQLite database for durable checkpoints")
        self.db_path = Path(db_path).expanduser().resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.workspace = str(Path(workspace).expanduser().resolve())
        Path(self.workspace).mkdir(parents=True, exist_ok=True)
        self.tools = default_tools() if tools is None else dict(tools)
        self.ai_tools = {} if ai_tools is None else dict(ai_tools)
        self.lock_directory = Path(str(self.db_path) + ".advanced-locks")
        self.lock_directory.mkdir(parents=True, exist_ok=True)
        self.identity_lock_directory = Path(str(self.db_path) + ".locks")
        self.identity_lock_directory.mkdir(parents=True, exist_ok=True)
        with self._connect() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript('''
                CREATE TABLE IF NOT EXISTS a_runs (
                    run_id TEXT PRIMARY KEY, workflow_json TEXT NOT NULL,
                    inputs_json TEXT NOT NULL, workspace TEXT NOT NULL,
                    status TEXT NOT NULL, outputs_json TEXT, error TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    execution_token TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS a_steps (
                    position INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES a_runs(run_id),
                    step_path TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0, result_json TEXT,
                    error TEXT, idempotency_key TEXT NOT NULL, prompt TEXT,
                    started_at TEXT, completed_at TEXT,
                    UNIQUE(run_id, step_path)
                );
                CREATE TABLE IF NOT EXISTS a_decisions (
                    run_id TEXT NOT NULL, step_path TEXT NOT NULL,
                    approved INTEGER NOT NULL, actor TEXT NOT NULL, decided_at TEXT NOT NULL,
                    PRIMARY KEY(run_id,step_path),
                    FOREIGN KEY(run_id,step_path) REFERENCES a_steps(run_id,step_path)
                );
                CREATE TABLE IF NOT EXISTS a_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES a_runs(run_id), step_path TEXT,
                    event TEXT NOT NULL, details_json TEXT NOT NULL, created_at TEXT NOT NULL
                );
            ''')

    @staticmethod
    def contains_run(db_path, run_id):
        path = Path(db_path).expanduser().resolve()
        if not path.is_file():
            return False
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=30) as c:
            if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='a_runs'").fetchone():
                return False
            return c.execute("SELECT 1 FROM a_runs WHERE run_id=?", (run_id,)).fetchone() is not None

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

    @staticmethod
    def _check_id(run_id):
        if not isinstance(run_id, str) or not run_id.strip() or len(run_id) > 256:
            raise ValueError("run_id must be nonblank text of at most 256 characters")

    def _lock_paths(self, run_id):
        digest = hashlib.sha256(run_id.encode()).hexdigest()
        return self.lock_directory / (digest + ".executor"), self.lock_directory / (digest + ".effects")

    @contextmanager
    def _lock(self, run_id):
        import fcntl

        runner, effects = self._lock_paths(run_id)
        identity = self.identity_lock_directory / (hashlib.sha256(run_id.encode()).hexdigest() + ".lock")
        descriptors = []
        try:
            # All runtime/backends use the original lock namespace for run ID
            # ownership. Their per-engine locks can add further protection.
            for path in (identity, runner, effects):
                descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
                descriptors.append(descriptor)
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise RunBusyError("Executor or surviving tool still owns this run") from exc
            # Parent and spawned tools all hold shared effects locks; a future
            # recovery first requires exclusive access, fencing orphan tools.
            fcntl.flock(descriptors[2], fcntl.LOCK_SH)
            with self._connect() as connection:
                for table in ("runs", "langgraph_epochs"):
                    exists = connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
                    ).fetchone()
                    if exists and connection.execute(
                        f"SELECT 1 FROM {table} WHERE run_id=?", (run_id,)
                    ).fetchone():
                        raise IdentityMismatchError("Run ID belongs to the v0.1 runtime/backend; use its original engine")
            yield str(effects)
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    @staticmethod
    def _event(c, run_id, event, step_path=None, details=None):
        c.execute("INSERT INTO a_events(run_id,step_path,event,details_json,created_at) VALUES(?,?,?,?,?)",
                  (run_id, step_path, event, _json(details or {}), _now()))

    def _check_cancel(self, run_id):
        with self._connect() as c:
            row = c.execute("SELECT cancel_requested FROM a_runs WHERE run_id=?", (run_id,)).fetchone()
        if row[0]:
            raise _Cancelled()

    def _state(self, run_id, path, node):
        with self._connect() as c:
            key = hashlib.sha256(_json([run_id, path]).encode()).hexdigest()
            c.execute("INSERT OR IGNORE INTO a_steps(run_id,step_path,kind,status,idempotency_key,prompt) VALUES(?,?,?,'pending',?,?)",
                      (run_id, path, node["kind"], key, node.get("prompt")))
            return dict(c.execute("SELECT * FROM a_steps WHERE run_id=? AND step_path=?", (run_id, path)).fetchone())

    def _mark(self, run_id, path, status, result=None, error=None, attempt=False):
        with self._connect() as c:
            c.execute("UPDATE a_steps SET status=?,result_json=?,error=?,attempts=attempts+?,started_at=CASE WHEN ?='running' THEN ? ELSE started_at END,completed_at=? WHERE run_id=? AND step_path=?",
                      (status, _json(result) if status in {"completed", "skipped"} else None, error,
                       int(attempt), status, _now(), None if status == "running" else _now(), run_id, path))
            self._event(c, run_id, "step_" + status, path, {"error": error} if error else {})

    async def _execute(self, tool, args, context, timeout, execution):
        async with execution["pool"]:
            self._check_cancel(context.run_id)
            with tempfile.TemporaryDirectory(prefix="agentic-v2-") as directory:
                result_path = str(Path(directory) / "result.json")
                process = multiprocessing.get_context("spawn").Process(
                    target=_spawned_entry,
                    args=(cloudpickle.dumps(tool), args, context, result_path, str(self.db_path),
                          execution["token"], execution["effects"]),
                )
                try:
                    process.start()
                    deadline = time.monotonic() + timeout
                    while process.is_alive():
                        self._check_cancel(context.run_id)
                        if time.monotonic() >= deadline:
                            raise TimeoutError(f"Step exceeded timeout of {timeout:g} seconds")
                        await asyncio.sleep(0.02)
                    self._check_cancel(context.run_id)
                    if process.exitcode != 0 or not Path(result_path).is_file():
                        raise RuntimeError(f"Worker exited without a result (exit code {process.exitcode})")
                    payload = json.loads(Path(result_path).read_text(encoding="utf-8"))
                    if not payload["ok"]:
                        raise RuntimeError(payload["error"])
                    return payload["result"]
                finally:
                    await _stop_process(process)

    async def _leaf(self, node, inputs, results, loop, path, execution, state):
        run_id = execution["run_id"]
        args = _resolve(node["args"], inputs, results, loop)
        registry = self.ai_tools if node["kind"] == "ai" else self.tools
        if node["tool"] not in registry:
            raise ValueError(f"Unregistered {node['kind']} tool: {node['tool']}")
        context = TaskContext(self.workspace, run_id, path, state["idempotency_key"])
        retry = node.get("retry", {})
        attempts = retry.get("max_attempts", 1)
        for attempt in range(attempts):
            self._check_cancel(run_id)
            self._mark(run_id, path, "running", attempt=True)
            try:
                return await self._execute(registry[node["tool"]], args, context,
                                           node.get("timeout_seconds", 30), execution)
            except (_Cancelled, asyncio.CancelledError):
                raise
            except Exception as exc:
                self._mark(run_id, path, "failed", error=f"{type(exc).__name__}: {exc}")
                if attempt + 1 == attempts:
                    raise
                delay = retry.get("delay_seconds", 0)
                with self._connect() as c:
                    self._event(c, run_id, "step_retry_scheduled", path,
                                {"next_invocation_attempt": attempt + 2, "delay_seconds": delay})
                deadline = time.monotonic() + delay
                while time.monotonic() < deadline:
                    self._check_cancel(run_id)
                    await asyncio.sleep(min(0.05, max(0, deadline - time.monotonic())))

    async def _scope(self, nodes, inputs, outer, loop, prefix, execution):
        results, local = dict(outer), {}
        for node in nodes:
            path = prefix + node["id"]
            value = await self._node(node, inputs, results, loop, path, execution)
            results[node["id"]] = local[node["id"]] = value
        return local

    async def _node(self, node, inputs, results, loop, path, execution):
        run_id = execution["run_id"]
        self._check_cancel(run_id)
        state = self._state(run_id, path, node)
        if state["status"] in {"completed", "skipped"}:
            return json.loads(state["result_json"])
        try:
            if "when" in node:
                left, right = [_resolve(v, inputs, results, loop) for v in node["when"]["equals"]]
                if not _equals(left, right):
                    self._mark(run_id, path, "skipped")
                    return None
            kind = node["kind"]
            if kind in {"tool", "ai"}:
                value = await self._leaf(node, inputs, results, loop, path, execution, state)
            elif kind == "approval":
                with self._connect() as c:
                    decision = c.execute("SELECT approved,actor FROM a_decisions WHERE run_id=? AND step_path=?", (run_id, path)).fetchone()
                if decision is None:
                    raise _Waiting([{"step_path": path, "prompt": node["prompt"]}])
                if not decision["approved"]:
                    raise ApprovalDeniedError(f"Approval denied for {path} by {decision['actor']}")
                value = {"approved": True, "actor": decision["actor"]}
            elif kind == "foreach":
                self._mark(run_id, path, "running")
                items = _resolve(node["items"], inputs, results, loop)
                if type(items) is not list:
                    raise ValueError(f"{path}.items must resolve to an array")
                if len(items) > node["max_items"]:
                    raise ValueError(f"{path} has {len(items)} items, exceeding max_items={node['max_items']}")
                outputs = []
                for index, item in enumerate(items):
                    self._check_cancel(run_id)
                    outputs.append(await self._scope(node["steps"], inputs, results,
                                   {"item": item, "index": index}, f"{path}/{index}/", execution))
                value = {"items": outputs, "count": len(outputs)}
            else:
                self._mark(run_id, path, "running")
                semaphore = asyncio.Semaphore(node["max_workers"])

                async def branch(name, steps):
                    async with semaphore:
                        self._check_cancel(run_id)
                        return await self._scope(steps, inputs, results, loop, f"{path}/{name}/", execution)

                names = list(node["branches"])
                joined = await asyncio.gather(*(branch(name, node["branches"][name]) for name in names),
                                              return_exceptions=True)
                waiting = []
                for result in joined:
                    if isinstance(result, _Cancelled):
                        raise result
                    if isinstance(result, _Waiting):
                        waiting.extend(result.approvals)
                    elif isinstance(result, BaseException):
                        raise result
                if waiting:
                    raise _Waiting(waiting)
                value = {"branches": dict(zip(names, joined))}
            self._check_cancel(run_id)
            self._mark(run_id, path, "completed", result=value)
            return value
        except _Waiting:
            self._mark(run_id, path, "waiting_approval")
            raise
        except _Cancelled:
            self._mark(run_id, path, "cancelled", error="Cancellation requested")
            raise
        except Exception as exc:
            self._mark(run_id, path, "failed", error=f"{type(exc).__name__}: {exc}")
            raise

    def run(self, workflow, inputs, run_id=None, resume=False, decisions=None):
        workflow = json.loads(_json(validate_workflow_v2(workflow)))
        inputs = json.loads(_json(validate_inputs_v2(workflow, inputs)))
        if resume and run_id is None:
            raise ValueError("resume requires run_id")
        run_id = str(uuid.uuid4()) if run_id is None else run_id
        self._check_id(run_id)
        workflow_json, inputs_json = _json(workflow), _json(inputs)
        with self._lock(run_id) as effects:
            token = uuid.uuid4().hex
            with self._connect() as c:
                row = c.execute("SELECT * FROM a_runs WHERE run_id=?", (run_id,)).fetchone()
                if row:
                    if not resume:
                        raise ValueError("run_id already exists; use resume=True")
                    if (row["workflow_json"], row["inputs_json"], row["workspace"]) != (workflow_json, inputs_json, self.workspace):
                        raise IdentityMismatchError("Resume requires identical workflow, inputs, and workspace")
                    if row["status"] == "running":
                        raise RecoveryRequiredError("Interrupted run requires explicit recover(policy='retry')")
                    if row["status"] in {"completed", "cancelled"}:
                        return self._result(run_id)
                else:
                    if resume:
                        raise ValueError("Cannot resume unknown run")
                    c.execute("INSERT INTO a_runs VALUES(?,?,?,?,'pending',NULL,NULL,0,?,?,?)",
                              (run_id, workflow_json, inputs_json, self.workspace, token, _now(), _now()))
                    self._event(c, run_id, "run_created")
            if decisions:
                self._apply_decisions(run_id, decisions)
            with self._connect() as c:
                c.execute("UPDATE a_runs SET status='running',execution_token=?,error=NULL,updated_at=? WHERE run_id=? AND cancel_requested=0",
                          (token, _now(), run_id))
                self._event(c, run_id, "run_resumed" if resume else "run_started")

            async def execute():
                execution = {"run_id": run_id, "token": token, "effects": effects,
                             "pool": asyncio.Semaphore(8)}
                values = await self._scope(workflow["steps"], inputs, {}, None, "", execution)
                return _resolve(workflow["outputs"], inputs, values, None)

            try:
                outputs = asyncio.run(execute())
                with self._connect() as c:
                    # A cancellation racing the final output commit wins.
                    changed = c.execute("UPDATE a_runs SET status='completed',outputs_json=?,error=NULL,updated_at=? WHERE run_id=? AND cancel_requested=0",
                                        (_json(outputs), _now(), run_id)).rowcount
                    if not changed:
                        raise _Cancelled()
                    self._event(c, run_id, "run_completed")
            except _Waiting as waiting:
                with self._connect() as c:
                    c.execute("UPDATE a_runs SET status=CASE WHEN cancel_requested=1 THEN 'cancelled' ELSE 'waiting_approval' END,updated_at=? WHERE run_id=?", (_now(), run_id))
                    self._event(c, run_id, "run_waiting_approval", details={"approvals": waiting.approvals})
            except _Cancelled:
                with self._connect() as c:
                    c.execute("UPDATE a_runs SET status='cancelled',error='Cancellation requested',updated_at=? WHERE run_id=?", (_now(), run_id))
                    self._event(c, run_id, "run_cancelled")
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                with self._connect() as c:
                    c.execute("UPDATE a_runs SET status=CASE WHEN cancel_requested=1 THEN 'cancelled' ELSE 'failed' END,error=?,updated_at=? WHERE run_id=?", (error, _now(), run_id))
                    self._event(c, run_id, "run_failed", details={"error": error})
            return self._result(run_id)

    def _result(self, run_id):
        state = self.inspect(run_id)
        result = {"run_id": run_id, "status": state["status"], "outputs": state["outputs"] or {}}
        if state["error"]:
            result["error"] = state["error"]
        if state["status"] == "waiting_approval":
            result["approvals"] = state["approvals"]
        return result

    def inspect(self, run_id):
        self._check_id(run_id)
        with self._connect() as c:
            row = c.execute("SELECT * FROM a_runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(f"Unknown run: {run_id}")
            steps = c.execute("SELECT * FROM a_steps WHERE run_id=? ORDER BY position", (run_id,)).fetchall()
            decisions = c.execute("SELECT * FROM a_decisions WHERE run_id=? ORDER BY step_path", (run_id,)).fetchall()
        result = dict(row)
        result["workflow"] = json.loads(result.pop("workflow_json"))
        result["inputs"] = json.loads(result.pop("inputs_json"))
        raw = result.pop("outputs_json")
        result["outputs"] = json.loads(raw) if raw is not None else None
        result["cancel_requested"] = bool(result["cancel_requested"])
        result.pop("execution_token")
        result["steps"] = []
        result["decisions"] = [{**dict(d), "approved": bool(d["approved"])} for d in decisions]
        result["approvals"] = []
        decided = {d["step_path"] for d in decisions}
        for row in steps:
            step = dict(row)
            step["step_id"] = step["step_path"]
            raw = step.pop("result_json")
            step["result"] = json.loads(raw) if raw is not None else None
            result["steps"].append(step)
            if step["kind"] == "approval" and step["status"] == "waiting_approval" and step["step_path"] not in decided:
                result["approvals"].append({"step_path": step["step_path"], "prompt": step["prompt"]})
        return result

    def events(self, run_id):
        self.inspect(run_id)
        with self._connect() as c:
            rows = c.execute("SELECT * FROM a_events WHERE run_id=? ORDER BY id", (run_id,)).fetchall()
        result = []
        for row in rows:
            event = dict(row)
            event["step_id"] = event["step_path"]
            event["details"] = json.loads(event.pop("details_json"))
            result.append(event)
        return result

    def _apply_decisions(self, run_id, decisions):
        if isinstance(decisions, dict):
            values = [{**value, "step_path": path} for path, value in decisions.items()]
        elif isinstance(decisions, list):
            values = decisions
        else:
            raise ValueError("decisions must be a mapping or array")
        with self._connect() as c:
            for decision in values:
                if type(decision) is not dict or set(decision) != {"step_path", "approved", "actor"}:
                    raise ValueError("Each decision requires step_path, approved, and actor")
                path, approved, actor = decision["step_path"], decision["approved"], decision["actor"]
                if type(path) is not str or type(approved) is not bool or type(actor) is not str or not actor.strip() or len(actor) > 256:
                    raise ValueError("Decision requires a step path, boolean approved, and nonblank actor")
                row = c.execute("SELECT * FROM a_steps WHERE run_id=? AND step_path=?", (run_id, path)).fetchone()
                if row is None or row["kind"] != "approval":
                    raise ValueError("Approval must address an existing approval checkpoint")
                existing = c.execute("SELECT approved,actor FROM a_decisions WHERE run_id=? AND step_path=?", (run_id, path)).fetchone()
                if existing:
                    if (bool(existing["approved"]), existing["actor"]) != (approved, actor):
                        raise ValueError("A recorded approval decision is immutable")
                    continue
                run = c.execute("SELECT status,cancel_requested FROM a_runs WHERE run_id=?", (run_id,)).fetchone()
                if run["cancel_requested"] or run["status"] in {"cancelled", "completed"}:
                    raise ValueError("Cannot decide an approval for a terminal run")
                if row["status"] != "waiting_approval":
                    raise ValueError("Approval checkpoint is not waiting for a decision")
                c.execute("INSERT INTO a_decisions VALUES(?,?,?,?,?)", (run_id, path, int(approved), actor, _now()))
                self._event(c, run_id, "approval_decided", path, {"approved": approved, "actor": actor})

    def approve(self, run_id, step_path, approved, actor):
        self._check_id(run_id)
        self._apply_decisions(run_id, [{"step_path": step_path, "approved": approved, "actor": actor}])
        return self.inspect(run_id)

    def cancel(self, run_id):
        self._check_id(run_id)
        with self._connect() as c:
            row = c.execute("SELECT status FROM a_runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(f"Unknown run: {run_id}")
            if row["status"] not in {"completed", "cancelled"}:
                c.execute("UPDATE a_runs SET status='cancelled',cancel_requested=1,error='Cancellation requested',updated_at=? WHERE run_id=?", (_now(), run_id))
                self._event(c, run_id, "cancellation_requested")
        return self.inspect(run_id)

    def recover(self, run_id, policy="retry"):
        self._check_id(run_id)
        if policy != "retry":
            raise ValueError("Only explicit recovery policy='retry' is supported")
        with self._lock(run_id):
            with self._connect() as c:
                row = c.execute("SELECT status FROM a_runs WHERE run_id=?", (run_id,)).fetchone()
                if row is None:
                    raise KeyError(f"Unknown run: {run_id}")
                if row["status"] != "running":
                    raise ValueError("Only an interrupted running run requires recovery")
                error = "Uncertain effects explicitly acknowledged for retry"
                c.execute("UPDATE a_steps SET status='failed',error=?,completed_at=? WHERE run_id=? AND status='running'", (error, _now(), run_id))
                c.execute("UPDATE a_runs SET status='failed',execution_token=?,error=?,updated_at=? WHERE run_id=?", (uuid.uuid4().hex, error, _now(), run_id))
                self._event(c, run_id, "run_recovered", details={"policy": policy})
        return self.inspect(run_id)

    def close(self):
        """No persistent connection; each transaction closes its connection."""
