import copy
from http.server import BaseHTTPRequestHandler, HTTPServer
import importlib.util
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from agentic_workflow.backends.langgraph import LangGraphBackend, LangGraphRuntime
from agentic_workflow.ir import ValidationError
from agentic_workflow.runtime import IdentityMismatchError, RecoveryRequiredError, RunBusyError, Runtime
from agentic_workflow.providers import ChatCompletionsProvider


def step(name, tool="core.value", args=None, **kwargs):
    return {"id": name, "kind": "tool", "tool": tool, "args": args or {"value": name}, **kwargs}


def workflow(steps, outputs=None):
    return {"ir_version": "0.1", "id": "parity", "inputs": {}, "steps": steps, "outputs": outputs or {}}


def record(args, context):
    path = Path(context.workspace) / "effects.jsonl"
    with path.open("a") as file:
        file.write(json.dumps({"id": context.step_id, "key": context.idempotency_key}) + "\n")
    return {"value": args.get("value", "recorded")}


def fail_until_ready(args, context):
    if not (Path(context.workspace) / "ready").exists():
        raise RuntimeError("service unavailable")
    return {"value": "recovered"}


def flaky(args, context):
    path = Path(context.workspace) / "attempts.txt"
    count = int(path.read_text()) if path.exists() else 0
    path.write_text(str(count + 1))
    if count == 0:
        raise RuntimeError("temporary outage")
    return {"value": "retried"}


def waits(args, context):
    time.sleep(1)
    return {"value": "late"}


def orphan_effect(args, context):
    root = Path(context.workspace)
    if (root / "effect_done").exists():
        return {"value": "done"}
    (root / "effect_started").write_text(str(os.getpid()))
    deadline = time.monotonic() + 10
    while not (root / "effect_release").exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    (root / "effect_done").write_text("done")
    return {"value": "done"}


def run_orphan(db_path, workspace, spec):
    LangGraphRuntime(db_path, workspace, tools={"test.orphan": orphan_effect}).run(spec, {}, run_id="orphan")


@unittest.skipUnless(importlib.util.find_spec("langgraph"), "optional real langgraph dependency not installed")
class LangGraphTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def runtime(self, engine=LangGraphRuntime, label="graph", **kwargs):
        return engine(self.root / (label + ".db"), self.root / label, **kwargs)

    def test_native_stategraph_drives_nodes_and_file_output_parity(self):
        from langgraph.graph.state import CompiledStateGraph
        spec = workflow([
            step("normalize", "text.normalize", {"text": "  Alpha \n beta  "}),
            step("write", "files.write_text", {"path": "result.txt", "text": {"$ref": "steps.normalize.text"}}),
        ], {"text": {"$ref": "steps.normalize.text"}})
        local = self.runtime(Runtime, "local").run(spec, {}, run_id="business")
        actual_invoke = CompiledStateGraph.invoke
        visits = []
        def observe(graph, *args, **kwargs):
            visits.extend(graph.nodes)
            return actual_invoke(graph, *args, **kwargs)
        with patch.object(CompiledStateGraph, "invoke", observe):
            graph = self.runtime().run(spec, {}, run_id="business")
        self.assertEqual(graph, local)
        self.assertIn("step__normalize", visits)
        self.assertIn("step__write", visits)
        self.assertEqual((self.root / "graph/result.txt").read_text(), (self.root / "local/result.txt").read_text())

    def test_failure_stops_graph_and_resume_keeps_completed_effects(self):
        spec = workflow([step("record", "test.record"), step("gate", "test.gate"), step("last", "test.record")],
                        {"value": {"$ref": "steps.gate.value"}})
        observed = []
        for engine, label in ((Runtime, "local"), (LangGraphRuntime, "graph")):
            runtime = self.runtime(engine, label, tools={"test.record": record, "test.gate": fail_until_ready})
            first = runtime.run(spec, {}, run_id="resume")
            self.assertEqual(first["status"], "failed")
            self.assertEqual([row["status"] for row in runtime.inspect("resume")["steps"]], ["completed", "failed", "pending"])
            path = self.root / label
            self.assertEqual(len((path / "effects.jsonl").read_text().splitlines()), 1)
            (path / "ready").touch()
            final = runtime.run(spec, {}, run_id="resume", resume=True)
            observed.append(final)
            self.assertEqual(len((path / "effects.jsonl").read_text().splitlines()), 2)
            self.assertEqual([row["attempts"] for row in runtime.inspect("resume")["steps"]], [1, 2, 1])
            self.assertEqual(runtime.run(spec, {}, run_id="resume", resume=True), final)
        self.assertEqual(observed[0], observed[1])

    def test_retry_condition_skip_null_and_idempotency_parity(self):
        spec = workflow([
            step("service", "test.flaky", retry={"max_attempts": 2}),
            step("skip", "test.record", when={"equals": [True, 1]}),
            step("record", "test.record", when={"equals": [{"$ref": "steps.service.value"}, "retried"]}),
        ], {"value": {"$ref": "steps.service.value"}, "skipped": {"$ref": "steps.skip.any"}})
        states = []
        for engine, label in ((Runtime, "local"), (LangGraphRuntime, "graph")):
            runtime = self.runtime(engine, label, tools={"test.flaky": flaky, "test.record": record})
            result = runtime.run(spec, {}, run_id="retry")
            self.assertEqual(result["outputs"], {"value": "retried", "skipped": None})
            states.append([(s["status"], s["attempts"], s["idempotency_key"]) for s in runtime.inspect("retry")["steps"]])
            self.assertEqual((self.root / label / "attempts.txt").read_text(), "2")
        self.assertEqual(states[0], states[1])

    def test_timeout_and_output_resolution_failure_remain_failures(self):
        runtime = self.runtime(tools={"test.wait": waits})
        result = runtime.run(workflow([step("wait", "test.wait", timeout_seconds=0.1)]), {}, run_id="timeout")
        self.assertEqual(result["status"], "failed")
        self.assertIn("timeout", result["error"].lower())
        result = self.runtime(label="fields").run(workflow([step("first")], {"x": {"$ref": "steps.first.missing"}}), {}, run_id="fields")
        self.assertEqual(result["status"], "failed")
        self.assertIn("Reference has no field", result["error"])

    def test_resume_identity_and_interruption_recovery(self):
        runtime = self.runtime(tools={"test.gate": fail_until_ready})
        spec = workflow([step("gate", "test.gate")])
        runtime.run(spec, {}, run_id="recover")
        changed = copy.deepcopy(spec)
        changed["steps"][0]["args"]["new"] = True
        with self.assertRaises(IdentityMismatchError):
            runtime.run(changed, {}, run_id="recover", resume=True)
        with sqlite3.connect(self.root / "graph.db") as connection:
            connection.execute("UPDATE runs SET status='running' WHERE run_id='recover'")
            connection.execute("UPDATE steps SET status='running' WHERE run_id='recover'")
        with self.assertRaises(RecoveryRequiredError):
            runtime.run(spec, {}, run_id="recover", resume=True)
        runtime.recover("recover", policy="retry")
        (self.root / "graph/ready").touch()
        self.assertEqual(runtime.run(spec, {}, run_id="recover", resume=True)["status"], "completed")

    def test_compiled_source_is_importable_and_builds_real_graph(self):
        artifact = LangGraphBackend().compile(workflow([step("first")], {"value": {"$ref": "steps.first.value"}}))
        scope = {}
        exec(compile(artifact["source"], "run_workflow.py", "exec"), scope)
        result = scope["run"]({}, self.root / "artifact.db", self.root / "artifact")
        self.assertEqual(result["outputs"], {"value": "first"})
        self.assertEqual(artifact["manifest"]["engine"], "langgraph.graph.StateGraph")
        self.assertEqual(set(artifact["files"]), {"workflow.json", "run_workflow.py"})

    def test_backend_rejects_v02_and_ai_registry_is_explicit(self):
        unsupported = workflow([step("first")])
        unsupported["ir_version"] = "0.2"
        with self.assertRaises(ValidationError):
            LangGraphBackend().compile(unsupported)
        spec = workflow([step("ai", "fixture.ai", kind="ai")], {"value": {"$ref": "steps.ai.value"}})
        missing = self.runtime().run(spec, {}, run_id="missing")
        self.assertEqual(missing["status"], "failed")
        self.assertIn("Unregistered ai tool", missing["error"])
        result = self.runtime(ai_tools={"fixture.ai": record}).run(spec, {}, run_id="ai")
        self.assertEqual(result["outputs"], {"value": "ai"})

    def test_parent_crash_cannot_recover_over_surviving_effect(self):
        spec = workflow([step("orphan", "test.orphan")], {"value": {"$ref": "steps.orphan.value"}})
        runtime = self.runtime(tools={"test.orphan": orphan_effect})
        root = self.root / "graph"
        process = multiprocessing.get_context("spawn").Process(
            target=run_orphan, args=(self.root / "graph.db", root, spec)
        )
        process.start()
        try:
            deadline = time.monotonic() + 8
            while not (root / "effect_started").exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue((root / "effect_started").exists())
            process.kill()
            process.join(2)
            with self.assertRaises(RunBusyError):
                runtime.recover("orphan", policy="retry")
            (root / "effect_release").touch()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    runtime.recover("orphan", policy="retry")
                except RunBusyError:
                    time.sleep(0.02)
                else:
                    break
            self.assertTrue((root / "effect_done").exists())
            result = runtime.run(spec, {}, run_id="orphan", resume=True)
            self.assertEqual(result["outputs"], {"value": "done"})
        finally:
            (root / "effect_release").touch()
            if process.is_alive():
                process.kill()
                process.join(2)
            process.close()

    def test_real_http_provider_operates_in_spawned_graph_leaf(self):
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                if self.headers.get("Authorization") != "Bearer TEST-ONLY-spawn-key":
                    self.send_response(401)
                    self.end_headers()
                    return
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append({"body": body, "key": self.headers.get("Idempotency-Key")})
                response = json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": "generated-report"}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)
            def log_message(self, *args):
                pass
        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            provider = ChatCompletionsProvider(f"http://127.0.0.1:{server.server_port}/chat", "http-fixture", "TEST-ONLY-spawn-key")
            runtime = self.runtime(ai_tools={"fixture.ai": provider})
            spec = workflow([step("ai", "fixture.ai", {"text": "summarize"}, kind="ai")],
                            {"text": {"$ref": "steps.ai.text"}})
            result = runtime.run(spec, {}, run_id="http-ai")
            self.assertEqual(result["status"], "completed", result)
            self.assertEqual(result["outputs"], {"text": "generated-report"})
            self.assertEqual(requests[0]["key"], runtime.inspect("http-ai")["steps"][0]["idempotency_key"])
            self.assertNotIn("TEST-ONLY-spawn-key", json.dumps(runtime.inspect("http-ai")))
            self.assertNotIn("TEST-ONLY-spawn-key", json.dumps(runtime.events("http-ai")))
            from agentic_workflow.advanced_runtime import AdvancedRuntime
            advanced = AdvancedRuntime(self.root / "advanced-http.db", self.root / "advanced-http",
                                       ai_tools={"fixture.ai": provider})
            v02 = copy.deepcopy(spec)
            v02["ir_version"] = "0.2"
            result = advanced.run(v02, {}, run_id="http-ai-advanced")
            self.assertEqual(result["status"], "completed", result)
            self.assertEqual(result["outputs"], {"text": "generated-report"})
            self.assertEqual(len(requests), 2)
            self.assertEqual(len(requests[1]["key"]), 64)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
