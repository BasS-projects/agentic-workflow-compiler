import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest

from agentic_workflow.advanced_runtime import AdvancedRuntime
from agentic_workflow.runtime import IdentityMismatchError, RecoveryRequiredError, RunBusyError, Runtime
from agentic_workflow.worker import default_tools


def leaf(identifier="value", value=1):
    return {"id": identifier, "kind": "tool", "tool": "core.value", "args": {"value": value}}


def workflow(steps=None, outputs=None):
    return {"ir_version": "0.2", "id": "advanced", "inputs": {},
            "steps": steps or [leaf()], "outputs": outputs or {}}


def timed_tool(args, context):
    start = time.monotonic()
    Path(context.workspace, args["name"] + ".started").write_text(str(os.getpid()))
    time.sleep(args.get("delay", 0.3))
    if args.get("finish_file"):
        Path(context.workspace, args["finish_file"]).write_text("should not exist after cancel")
    return {"start": start, "end": time.monotonic(), "pid": os.getpid(), "key": context.idempotency_key}


def flaky_tool(args, context):
    path = Path(context.workspace, "attempts.json")
    records = json.loads(path.read_text()) if path.exists() else []
    records.append(context.idempotency_key)
    path.write_text(json.dumps(records))
    if len(records) < 2:
        raise RuntimeError("outage")
    return {"attempts": len(records)}


def orphan_orchestrator(db, workspace, workflow):
    runtime = AdvancedRuntime(db, workspace, tools={**default_tools(), "test.timed": timed_tool})
    runtime.run(workflow, {}, run_id="orphan")


class AdvancedRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.db = self.root / "runtime.sqlite3"
        self.workspace = self.root / "workspace"
        self.tools = {**default_tools(), "test.timed": timed_tool, "test.flaky": flaky_tool}
        self.runtime = AdvancedRuntime(self.db, self.workspace, tools=self.tools)

    def tearDown(self):
        self.runtime.close()
        self.directory.cleanup()

    def timed(self, name, delay=0.3, **extra):
        return {"id": name, "kind": "tool", "tool": "test.timed", "args": {"name": name, "delay": delay, **extra}}

    def wait_file(self, path, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path.exists():
                return
            time.sleep(0.02)
        self.fail(f"file never appeared: {path}")

    def test_nested_foreach_and_capture(self):
        w = workflow([leaf("captured", "outer"),
            {"id": "normalize", "kind": "foreach", "max_items": 4, "items": [{"text": "  a  "}, {"text": " b "}],
             "steps": [{"id": "text", "kind": "tool", "tool": "text.normalize", "args": {"text": {"$ref": "loop.item.text"}}},
                       leaf("outer", {"$ref": "steps.captured.value"})]}],
            {"text": {"$ref": "steps.normalize.items.1.text.text"}, "count": {"$ref": "steps.normalize.count"}})
        result = self.runtime.run(w, {}, run_id="nested")
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(result["outputs"], {"text": "b", "count": 2})
        state = self.runtime.inspect("nested")
        self.assertIn("normalize/1/text", {s["step_path"] for s in state["steps"]})

    def test_parallel_actual_overlap_and_bound(self):
        w = workflow([{"id": "p", "kind": "parallel", "max_workers": 2,
                        "branches": {name: [self.timed(name, 0.5)] for name in ("a", "b", "c", "d")}}],
                     {"branches": {"$ref": "steps.p.branches"}})
        result = self.runtime.run(w, {}, run_id="parallel")
        self.assertEqual(result["status"], "completed", result)
        timings = [branch[name] for name, branch in result["outputs"]["branches"].items()]
        edges = sorted([(row["start"], 1) for row in timings] + [(row["end"], -1) for row in timings])
        current = maximum = 0
        for _, delta in edges:
            current += delta
            maximum = max(maximum, current)
        self.assertEqual(maximum, 2, timings)
        self.assertEqual(len({row["pid"] for row in timings}), 4)

    def test_approval_restart_retains_completed_steps(self):
        w = workflow([self.timed("before", 0), {"id": "review", "kind": "approval", "prompt": "Publish?"},
                      leaf("after", {"$ref": "steps.before.key"})], {"key": {"$ref": "steps.after.value"}})
        first = self.runtime.run(w, {}, run_id="review")
        self.assertEqual(first["status"], "waiting_approval", first)
        self.assertEqual(first["approvals"], [{"step_path": "review", "prompt": "Publish?"}])
        reopened = AdvancedRuntime(self.db, self.workspace, tools=self.tools)
        reopened.approve("review", "review", True, "auditor")
        second = reopened.run(w, {}, run_id="review", resume=True)
        self.assertEqual(second["status"], "completed", second)
        states = reopened.inspect("review")["steps"]
        self.assertEqual(next(s for s in states if s["step_path"] == "before")["attempts"], 1)
        self.assertTrue(AdvancedRuntime.contains_run(self.db, "review"))
        with self.assertRaises(ValueError):
            reopened.approve("review", "review", False, "auditor")

    def test_parallel_approvals_collect_and_denial_blocks_downstream(self):
        approval = {"id": "review", "kind": "approval", "prompt": "Accept?"}
        w = workflow([{"id": "p", "kind": "parallel", "max_workers": 2,
                       "branches": {"a": [approval], "b": [approval]}}, self.timed("after", 0)])
        first = self.runtime.run(w, {}, run_id="deny")
        self.assertEqual({a["step_path"] for a in first["approvals"]}, {"p/a/review", "p/b/review"})
        second = self.runtime.run(w, {}, run_id="deny", resume=True, decisions={
            "p/a/review": {"approved": True, "actor": "a"}, "p/b/review": {"approved": False, "actor": "b"}})
        self.assertEqual(second["status"], "failed")
        self.assertIn("ApprovalDeniedError", second["error"])
        self.assertFalse((self.workspace / "after.started").exists())

    def test_retry_key_stable_and_completed_resume_idempotent(self):
        w = workflow([{"id": "retry", "kind": "tool", "tool": "test.flaky", "args": {},
                      "retry": {"max_attempts": 2}}], {"attempts": {"$ref": "steps.retry.attempts"}})
        result = self.runtime.run(w, {}, run_id="retry")
        self.assertEqual(result["outputs"], {"attempts": 2}, result)
        keys = json.loads((self.workspace / "attempts.json").read_text())
        self.assertEqual(keys[0], keys[1])
        self.assertEqual(self.runtime.run(w, {}, run_id="retry", resume=True), result)

    def test_cancel_active_spawn_and_terminal_resume(self):
        w = workflow([self.timed("slow", 5, finish_file="finished")])
        result = {}
        thread = threading.Thread(target=lambda: result.update(self.runtime.run(w, {}, run_id="cancel")))
        thread.start()
        self.wait_file(self.workspace / "slow.started")
        started = time.monotonic()
        AdvancedRuntime(self.db, self.workspace).cancel("cancel")
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(result["status"], "cancelled", result)
        self.assertFalse((self.workspace / "finished").exists())
        self.assertEqual(self.runtime.run(w, {}, run_id="cancel", resume=True)["status"], "cancelled")

    def test_bound_failure_before_body_effects(self):
        w = workflow([{"id": "f", "kind": "foreach", "max_items": 1, "items": [1, 2], "steps": [self.timed("never", 0)]}])
        result = self.runtime.run(w, {}, run_id="bound")
        self.assertEqual(result["status"], "failed")
        self.assertIn("max_items", result["error"])
        self.assertFalse((self.workspace / "never.started").exists())

    def test_identity_change_refused_and_explicit_recovery(self):
        w = workflow([{"id": "a", "kind": "approval", "prompt": "Review"}])
        self.runtime.run(w, {}, run_id="recover")
        changed = workflow([leaf()])
        with self.assertRaises(IdentityMismatchError):
            self.runtime.run(changed, {}, run_id="recover", resume=True)
        with sqlite3.connect(self.db) as c:
            c.execute("UPDATE a_runs SET status='running' WHERE run_id='recover'")
        with self.assertRaises(RecoveryRequiredError):
            self.runtime.run(w, {}, run_id="recover", resume=True)
        self.runtime.recover("recover", policy="retry")
        self.assertEqual(self.runtime.run(w, {}, run_id="recover", resume=True)["status"], "waiting_approval")

    def test_cross_version_run_identity_is_reserved(self):
        original = Runtime(self.db, self.workspace)
        old = workflow([leaf()])
        old["ir_version"] = "0.1"
        self.assertEqual(original.run(old, {}, run_id="shared")["status"], "completed")
        with self.assertRaises(IdentityMismatchError):
            self.runtime.run(workflow(), {}, run_id="shared")
        self.assertFalse(AdvancedRuntime.contains_run(self.db, "shared"))
        advanced = workflow([{"id": "review", "kind": "approval", "prompt": "Continue?"}])
        self.assertEqual(self.runtime.run(advanced, {}, run_id="advanced_owned")["status"], "waiting_approval")
        with self.assertRaises(IdentityMismatchError):
            original.run(old, {}, run_id="advanced_owned")

    def test_cross_version_engine_uses_same_executor_lock(self):
        original = Runtime(self.db, self.workspace)
        with original._run_lock("exclusive"):
            with self.assertRaises(RunBusyError):
                self.runtime.run(workflow(), {}, run_id="exclusive")

    def test_timeout_and_closure_tool_support(self):
        prefix = "closed-over:"
        self.runtime.tools["closure"] = lambda args, context: {"value": prefix + args["text"]}
        w = workflow([{"id": "closure", "kind": "tool", "tool": "closure", "args": {"text": "ok"}}],
                     {"value": {"$ref": "steps.closure.value"}})
        self.assertEqual(self.runtime.run(w, {})["outputs"], {"value": "closed-over:ok"})
        slow = self.timed("timed_out", 2)
        slow["timeout_seconds"] = 0.1
        failed = self.runtime.run(workflow([slow]), {})
        self.assertEqual(failed["status"], "failed")
        self.assertIn("TimeoutError", failed["error"])

    def test_skipped_container_has_no_child_effects(self):
        node = {"id": "p", "kind": "parallel", "max_workers": 1,
                "when": {"equals": [True, 1]}, "branches": {"branch": [self.timed("never", 0)]}}
        result = self.runtime.run(workflow([node], {"value": {"$ref": "steps.p.branches.branch.value"}}), {})
        self.assertEqual(result["outputs"], {"value": None})
        self.assertFalse((self.workspace / "never.started").exists())

    def test_parent_crash_retains_effect_lock_until_orphan_exits(self):
        w = workflow([leaf("checkpoint"), self.timed("orphan_work", 0.6, finish_file="orphan_finished")])
        parent = multiprocessing.get_context("spawn").Process(
            target=orphan_orchestrator, args=(self.db, self.workspace, w))
        parent.start()
        try:
            self.wait_file(self.workspace / "orphan_work.started")
            parent.terminate()
            parent.join(3)
            self.assertFalse(parent.is_alive())
            with self.assertRaises(RunBusyError):
                self.runtime.recover("orphan", policy="retry")
            self.wait_file(self.workspace / "orphan_finished")
            deadline = time.monotonic() + 3
            while True:
                try:
                    self.runtime.recover("orphan", policy="retry")
                    break
                except RunBusyError:
                    if time.monotonic() > deadline:
                        raise
                    time.sleep(0.02)
            state = self.runtime.inspect("orphan")
            self.assertEqual(state["status"], "failed")
            before = next(s for s in state["steps"] if s["step_path"] == "checkpoint")
            self.assertEqual(before["status"], "completed")
            old_key = next(s for s in state["steps"] if s["step_path"] == "orphan_work")["idempotency_key"]
            result = self.runtime.run(w, {}, run_id="orphan", resume=True)
            self.assertEqual(result["status"], "completed", result)
            state = self.runtime.inspect("orphan")
            self.assertEqual(next(s for s in state["steps"] if s["step_path"] == "checkpoint")["attempts"], 1)
            after = next(s for s in state["steps"] if s["step_path"] == "orphan_work")
            self.assertEqual(after["attempts"], 2)
            self.assertEqual(after["idempotency_key"], old_key)
        finally:
            if parent.is_alive():
                parent.terminate()
                parent.join(3)
            parent.close()


if __name__ == "__main__":
    unittest.main()
