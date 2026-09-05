"""Executable LangGraph backend for IR 0.1.

StateGraph schedules one native node per IR step. SQLite persists the same leaf
states and identities as the local runtime. No LangGraph checkpointer is needed:
resume traverses the graph and restores completed/skipped nodes from SQLite.
"""

from __future__ import annotations

from dataclasses import asdict
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
from typing import TypedDict
import uuid

from ..adapters import LOCAL_RUNTIME_CAPABILITIES
from ..ir import validate_inputs, validate_workflow
from ..runtime import (
    Runtime, IdentityMismatchError, RecoveryRequiredError, RunBusyError,
    _equals, _json, _now, _resolve,
)
from ..worker import TaskContext


class _GraphState(TypedDict):
    results: dict
    failed: bool
    error: str


def _langgraph_api():
    try:
        from langgraph.graph import StateGraph, START, END
    except ImportError:
        raise RuntimeError("LangGraph backend requires pip install 'agentic-workflow-compiler[langgraph]'") from None
    return StateGraph, START, END


def _build_graph(workflow, callback):
    StateGraph, START, END = _langgraph_api()
    graph = StateGraph(_GraphState)
    # Prefix IDs to avoid conflicts with StateGraph reserved IDs/state channels.
    names = ["step__" + step["id"] for step in workflow["steps"]]
    for step, name in zip(workflow["steps"], names):
        def node(state, current=step):
            return callback(current, state)
        graph.add_node(name, node)
    graph.add_edge(START, names[0])
    for index, name in enumerate(names):
        following = names[index + 1] if index + 1 < len(names) else END
        def route(state, next_node=following):
            return END if state["failed"] else next_node
        graph.add_conditional_edges(name, route)
    return graph.compile()


class LangGraphRuntime(Runtime):
    """Durable IR 0.1 executor whose scheduling is driven by StateGraph."""

    def __init__(self, db_path, workspace, tools=None, ai_tools=None):
        super().__init__(db_path, workspace, tools=tools, ai_tools=ai_tools)
        self.effect_directory = Path(str(self.db_path) + ".langgraph-effects")
        self.effect_directory.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS langgraph_epochs (run_id TEXT PRIMARY KEY, epoch TEXT NOT NULL)")

    def _effect_path(self, run_id):
        return self.effect_directory / (hashlib.sha256(run_id.encode()).hexdigest() + ".lock")

    @contextmanager
    def _effect_guard(self, run_id):
        import fcntl
        descriptor = os.open(self._effect_path(run_id), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RunBusyError("A surviving LangGraph tool still holds the run's effect lock") from None
            yield
        finally:
            os.close(descriptor)

    def _fenced_tool(self, tool, run_id):
        # Serialize only trusted tool code and simple configuration into spawn.
        # The spawned process holds this shared effect lock across the tool call.
        import fcntl
        with self._connect() as connection:
            epoch = connection.execute("SELECT epoch FROM langgraph_epochs WHERE run_id=?", (run_id,)).fetchone()[0]
        db_path, effect_path = str(self.db_path), str(self._effect_path(run_id))
        def fenced(args, context):
            descriptor = os.open(effect_path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_SH)
                with sqlite3.connect(db_path) as connection:
                    row = connection.execute(
                        "SELECT e.epoch,r.status FROM langgraph_epochs e JOIN runs r USING(run_id) WHERE e.run_id=?", (run_id,)
                    ).fetchone()
                if row != (epoch, "running"):
                    raise RuntimeError("LangGraph execution fenced before tool invocation")
                return tool(args, context)
            finally:
                os.close(descriptor)
        return fenced

    def _prepare(self, workflow, inputs, run_id, resume):
        workflow_json, inputs_json = _json(workflow), _json(inputs)
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
                    raise RecoveryRequiredError("Run was interrupted; reconcile effects, call recover(run_id, policy='retry'), then resume")
                if existing["status"] == "completed":
                    return {"run_id": run_id, "status": "completed", "outputs": json.loads(existing["outputs_json"])}
                connection.execute("UPDATE runs SET status='running',error=NULL,updated_at=? WHERE run_id=?", (_now(), run_id))
                self._event(connection, run_id, "run_resumed")
            else:
                if resume:
                    raise ValueError(f"Cannot resume unknown run {run_id!r}")
                now = _now()
                connection.execute("INSERT INTO runs VALUES (?,?,?,?,?,'running',NULL,NULL,?,?)",
                                   (run_id, workflow["id"], workflow_json, inputs_json, self.workspace, now, now))
                for position, step in enumerate(workflow["steps"]):
                    key = hashlib.sha256(_json([run_id, step["id"]]).encode("utf-8")).hexdigest()
                    connection.execute("INSERT INTO steps (run_id,step_id,position,status,idempotency_key) VALUES (?,?,?,'pending',?)",
                                       (run_id, step["id"], position, key))
                self._event(connection, run_id, "run_started")
            self._event(connection, run_id, "backend_started", details={"backend": "langgraph", "engine": "StateGraph"})
            connection.execute("INSERT INTO langgraph_epochs VALUES (?,?) ON CONFLICT(run_id) DO UPDATE SET epoch=excluded.epoch",
                               (run_id, str(uuid.uuid4())))
        return None

    def _leaf(self, step, inputs, results, run_id):
        from ..advanced_runtime import execute_isolated
        step_id = step["id"]
        with self._connect() as connection:
            state = connection.execute("SELECT * FROM steps WHERE run_id=? AND step_id=?", (run_id, step_id)).fetchone()
            self._event(connection, run_id, "graph_node_visited", step_id, {"backend": "langgraph"})
        if state["status"] in {"completed", "skipped"}:
            return json.loads(state["result_json"]) if state["result_json"] else None
        if "when" in step:
            left, right = [_resolve(value, inputs, results) for value in step["when"]["equals"]]
            if not _equals(left, right):
                with self._connect() as connection:
                    connection.execute("UPDATE steps SET status='skipped',result_json='null',error=NULL,completed_at=? WHERE run_id=? AND step_id=?",
                                       (_now(), run_id, step_id))
                    self._event(connection, run_id, "step_skipped", step_id)
                return None
        args = _resolve(step.get("args", {}), inputs, results)
        registry = self.ai_tools if step["kind"] == "ai" else self.tools
        if step["tool"] not in registry:
            raise ValueError(f"Unregistered {step['kind']} tool: {step['tool']}")
        context = TaskContext(workspace=self.workspace, run_id=run_id, step_id=step_id,
                              idempotency_key=state["idempotency_key"])
        retry = step.get("retry", {})
        maximum = retry.get("max_attempts", 1)
        for attempt in range(1, maximum + 1):
            with self._connect() as connection:
                connection.execute("UPDATE steps SET status='running',attempts=attempts+1,error=NULL,started_at=?,completed_at=NULL WHERE run_id=? AND step_id=?",
                                   (_now(), run_id, step_id))
                self._event(connection, run_id, "step_started", step_id, {"invocation_attempt": attempt})
            try:
                tool = self._fenced_tool(registry[step["tool"]], run_id)
                result = execute_isolated(tool, args, context, step.get("timeout_seconds", 30))
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                with self._connect() as connection:
                    connection.execute("UPDATE steps SET status='failed',error=?,completed_at=? WHERE run_id=? AND step_id=?",
                                       (error, _now(), run_id, step_id))
                    self._event(connection, run_id, "step_failed", step_id, {"error": error})
                if attempt == maximum:
                    raise
                delay = retry.get("delay_seconds", 0)
                with self._connect() as connection:
                    self._event(connection, run_id, "step_retry_scheduled", step_id,
                                {"next_invocation_attempt": attempt + 1, "delay_seconds": delay})
                time.sleep(delay)
            else:
                with self._connect() as connection:
                    connection.execute("UPDATE steps SET status='completed',result_json=?,error=NULL,completed_at=? WHERE run_id=? AND step_id=?",
                                       (_json(result), _now(), run_id, step_id))
                    self._event(connection, run_id, "step_completed", step_id)
                return result

    def _fail(self, run_id, error, step_id=None):
        with self._connect() as connection:
            if step_id is not None:
                connection.execute("UPDATE steps SET status='failed',error=?,completed_at=? WHERE run_id=? AND step_id=? AND status NOT IN ('completed','skipped')",
                                   (error, _now(), run_id, step_id))
            connection.execute("UPDATE runs SET status='failed',error=?,updated_at=? WHERE run_id=?", (error, _now(), run_id))
            self._event(connection, run_id, "run_failed", step_id, {"error": error})

    def run(self, workflow, inputs, run_id=None, resume=False):
        workflow = json.loads(_json(validate_workflow(workflow)))
        inputs = json.loads(_json(validate_inputs(workflow, inputs)))
        # Fail dependency validation before creating a durable run.
        _langgraph_api()
        if resume and run_id is None:
            raise ValueError("resume requires run_id")
        run_id = str(uuid.uuid4()) if run_id is None else run_id
        self._check_run_id(run_id)
        with self._run_lock(run_id):
            with self._effect_guard(run_id):
                previous = self._prepare(workflow, inputs, run_id, resume)
            if previous is not None:
                return previous

            def execute_node(step, state):
                results = dict(state["results"])
                try:
                    results[step["id"]] = self._leaf(step, inputs, results, run_id)
                except Exception as exc:
                    error = f"{type(exc).__name__}: {exc}"
                    self._fail(run_id, error, step["id"])
                    return {"results": results, "failed": True, "error": error}
                return {"results": results, "failed": False, "error": ""}

            try:
                graph = _build_graph(workflow, execute_node)
                state = graph.invoke({"results": {}, "failed": False, "error": ""},
                                     config={"recursion_limit": len(workflow["steps"]) + 2, "max_concurrency": 1})
                if state["failed"]:
                    return {"run_id": run_id, "status": "failed", "error": state["error"], "outputs": {}}
                outputs = _resolve(workflow["outputs"], inputs, state["results"])
                with self._connect() as connection:
                    connection.execute("UPDATE runs SET status='completed',outputs_json=?,error=NULL,updated_at=? WHERE run_id=?",
                                       (_json(outputs), _now(), run_id))
                    self._event(connection, run_id, "run_completed")
                return {"run_id": run_id, "status": "completed", "outputs": outputs}
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                self._fail(run_id, error)
                return {"run_id": run_id, "status": "failed", "error": error, "outputs": {}}

    def recover(self, run_id, *, policy):
        """Fence delayed workers; refuse recovery while a surviving effect runs."""
        self._check_run_id(run_id)
        if policy != "retry":
            raise ValueError("Recovery requires explicit policy='retry'")
        with self._run_lock(run_id), self._effect_guard(run_id):
            with self._connect() as connection:
                row = connection.execute("SELECT status FROM runs WHERE run_id=?", (run_id,)).fetchone()
                if row is None:
                    raise KeyError(f"Unknown run: {run_id}")
                if row[0] != "running":
                    raise ValueError("Only an interrupted running run needs recovery")
                message = "Interrupted execution explicitly acknowledged for retry"
                connection.execute("UPDATE langgraph_epochs SET epoch=? WHERE run_id=?", (str(uuid.uuid4()), run_id))
                connection.execute("UPDATE steps SET status='failed',error=?,completed_at=? WHERE run_id=? AND status='running'",
                                   (message, _now(), run_id))
                connection.execute("UPDATE runs SET status='failed',error=?,updated_at=? WHERE run_id=?", (message, _now(), run_id))
                self._event(connection, run_id, "run_recovered", details={"policy": policy, "backend": "langgraph"})
            return self.inspect(run_id)


class LangGraphBackend:
    name = "langgraph"
    capabilities = LOCAL_RUNTIME_CAPABILITIES

    def compile(self, workflow):
        """Emit importable Python source and a capability manifest; never execute."""
        workflow = json.loads(_json(validate_workflow(workflow)))
        serialized = _json(workflow)
        source = (
            '"""Generated executable LangGraph artifact. Requires project + langgraph extra."""\n'
            "import json\n"
            "from agentic_workflow.backends.langgraph import LangGraphRuntime, _build_graph\n\n"
            f"WORKFLOW = json.loads({serialized!r})\n\n"
            "def build_graph(callback):\n"
            "    return _build_graph(WORKFLOW, callback)\n\n"
            "def run(inputs, db_path, workspace, *, run_id=None, resume=False, tools=None, ai_tools=None):\n"
            "    runtime = LangGraphRuntime(db_path, workspace, tools=tools, ai_tools=ai_tools)\n"
            "    try:\n"
            "        return runtime.run(WORKFLOW, inputs, run_id=run_id, resume=resume)\n"
            "    finally:\n"
            "        runtime.close()\n"
            "\nif __name__ == '__main__':\n"
            "    import argparse\n"
            "    from pathlib import Path\n"
            "    parser = argparse.ArgumentParser(description='Execute generated LangGraph workflow')\n"
            "    parser.add_argument('--inputs', required=True)\n"
            "    parser.add_argument('--db', required=True)\n"
            "    parser.add_argument('--workspace', required=True)\n"
            "    parser.add_argument('--run-id')\n"
            "    parser.add_argument('--resume', action='store_true')\n"
            "    args = parser.parse_args()\n"
            "    result = run(json.loads(Path(args.inputs).read_text()), args.db, args.workspace, run_id=args.run_id, resume=args.resume)\n"
            "    print(json.dumps(result, indent=2))\n"
            "    raise SystemExit(0 if result['status'] == 'completed' else 1)\n"
        )
        capabilities = asdict(self.capabilities)
        capabilities["step_kinds"] = sorted(capabilities["step_kinds"])
        capabilities["ir_versions"] = list(capabilities["ir_versions"])
        return {"backend": self.name, "artifact_version": "1.0", "language": "python", "source": source,
                "workflow": workflow, "ir_sha256": hashlib.sha256(serialized.encode()).hexdigest(),
                "files": {"workflow.json": json.dumps(workflow, indent=2) + "\n", "run_workflow.py": source},
                "capabilities": capabilities,
                "manifest": {"engine": "langgraph.graph.StateGraph", "dependency": "langgraph>=1.2.11,<2",
                             "persistence": "SQLite leaf checkpoints", "capabilities": capabilities,
                             "nodes": ["step__" + step["id"] for step in workflow["steps"]]}}

    @staticmethod
    def run(workflow, inputs, db_path, workspace, run_id=None, resume=False, tools=None, ai_tools=None):
        runtime = LangGraphRuntime(db_path, workspace, tools=tools, ai_tools=ai_tools)
        try:
            return runtime.run(workflow, inputs, run_id=run_id, resume=resume)
        finally:
            runtime.close()
