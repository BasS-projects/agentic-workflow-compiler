"""Behavioral tests for durable execution, failures, and process exclusion."""

import copy
import json
import multiprocessing
import os
from pathlib import Path
import signal
import sqlite3
import tempfile
import time
import unittest

from agentic_workflow.ir import ValidationError
from agentic_workflow.runtime import (
    IdentityMismatchError, RecoveryRequiredError, RunBusyError, Runtime,
)


def record(args, context):
    path = Path(context.workspace) / args.get("log", "calls.jsonl")
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"step": context.step_id, "key": context.idempotency_key}) + "\n")
    return {"nested": {"value": args.get("value", "recorded")}, "key": context.idempotency_key}


def require_gate(args, context):
    result = record(args, context)
    if not (Path(context.workspace) / "gate").exists():
        raise RuntimeError("gate is closed")
    return result


def delayed_side_effect(args, context):
    time.sleep(0.45)
    (Path(context.workspace) / "late-effect").write_text("should not exist")
    return {"done": True}


def ignores_termination(args, context):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    return delayed_side_effect(args, context)


def wait_for_release(args, context):
    (Path(context.workspace) / "entered").write_text("ready")
    while not (Path(context.workspace) / "release").exists():
        time.sleep(0.01)
    return {"done": True}


def invalid_result(args, context):
    return {"value": float("nan")}


def tuple_result(args, context):
    return {"value": (1, 2)}


def nonstring_key_result(args, context):
    return {1: "invalid"}


def spawn_background_effect(args, context):
    if os.fork() == 0:
        try:
            time.sleep(0.3)
            (Path(context.workspace) / "background-effect").write_text("should not exist")
        finally:
            os._exit(0)
    return {"started": True}


def execute_waiting(db_path, workspace, spec):
    runtime = Runtime(db_path, workspace, tools={"test.wait": wait_for_release})
    runtime.run(spec, {}, run_id="concurrent")


def workflow(steps, inputs=None, outputs=None):
    return {
        "ir_version": "0.1", "id": "test_flow", "inputs": inputs or {},
        "steps": steps, "outputs": outputs or {},
    }


def step(step_id, tool, **overrides):
    result = {"id": step_id, "kind": "tool", "tool": tool, "args": {}}
    result.update(overrides)
    return result


@unittest.skipUnless(os.name == "posix", "Runtime requires POSIX process isolation")
class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name) / "workspace"
        self.workspace.mkdir()
        self.db = Path(self.temp.name) / "runs.sqlite3"

    def runtime(self, **kwargs):
        return Runtime(self.db, self.workspace, **kwargs)

    def calls(self):
        return [json.loads(line) for line in (self.workspace / "calls.jsonl").read_text().splitlines()]

    def test_references_defaults_skipped_steps_and_completed_reuse(self):
        spec = workflow(
            [
                step("first", "core.value", args={"value": {"$ref": "inputs.name"}}),
                step("optional", "core.value", args={"value": "unused"},
                     when={"equals": [{"$ref": "inputs.enabled"}, True]}),
                step("last", "core.value", args={"value": [
                    {"$ref": "steps.first.value"}, {"$ref": "steps.optional.value"}
                ]}),
            ],
            inputs={"name": {"type": "string"}, "enabled": {"type": "boolean", "default": False}},
            outputs={"value": {"$ref": "steps.last.value"}},
        )
        result = self.runtime().run(spec, {"name": "hello"}, run_id="reuse")
        self.assertEqual(result["outputs"], {"value": ["hello", None]})
        state = self.runtime().inspect("reuse")
        self.assertEqual([s["status"] for s in state["steps"]], ["completed", "skipped", "completed"])
        self.assertEqual(state["inputs"], {"name": "hello", "enabled": False})
        events_before = self.runtime().events("reuse")
        # Providing the explicit default has the same normalized input identity.
        resumed = self.runtime(tools={}).run(spec, {"name": "hello", "enabled": False}, run_id="reuse", resume=True)
        self.assertEqual(resumed, result)
        self.assertEqual(self.runtime().events("reuse"), events_before)

    def test_failure_resume_reuses_completed_and_preserves_idempotency_key(self):
        spec = workflow(
            [step("first", "test.record"), step("second", "test.gate")],
            outputs={"result": {"$ref": "steps.second.nested.value"}},
        )
        runtime = self.runtime(tools={"test.record": record, "test.gate": require_gate})
        self.assertEqual(runtime.run(spec, {}, run_id="resume")["status"], "failed")
        (self.workspace / "gate").touch()
        self.assertEqual(runtime.run(spec, {}, run_id="resume", resume=True)["status"], "completed")
        calls = self.calls()
        self.assertEqual([call["step"] for call in calls], ["first", "second", "second"])
        self.assertEqual(calls[1]["key"], calls[2]["key"])
        self.assertNotEqual(calls[0]["key"], calls[1]["key"])
        self.assertEqual([s["attempts"] for s in runtime.inspect("resume")["steps"]], [1, 2])
        self.assertIn("run_resumed", [event["event"] for event in runtime.events("resume")])

    def test_retries_are_bounded_and_explicit_resume_starts_new_budget(self):
        spec = workflow([step("gate", "test.gate", retry={"max_attempts": 3, "delay_seconds": 0})])
        runtime = self.runtime(tools={"test.gate": require_gate})
        self.assertEqual(runtime.run(spec, {}, run_id="retry")["status"], "failed")
        self.assertEqual(len(self.calls()), 3)
        self.assertEqual(len({call["key"] for call in self.calls()}), 1)
        events = runtime.events("retry")
        self.assertEqual(sum(event["event"] == "step_retry_scheduled" for event in events), 2)
        (self.workspace / "gate").touch()
        self.assertEqual(runtime.run(spec, {}, run_id="retry", resume=True)["status"], "completed")
        self.assertEqual(runtime.inspect("retry")["steps"][0]["attempts"], 4)

    def test_resume_rejects_workflow_inputs_and_workspace_changes(self):
        spec = workflow([step("first", "core.value", args={"value": {"$ref": "inputs.value"}})],
                        inputs={"value": {"type": "string"}})
        runtime = self.runtime()
        runtime.run(spec, {"value": "original"}, run_id="identity")
        with self.assertRaises(IdentityMismatchError):
            runtime.run(spec, {"value": "changed"}, run_id="identity", resume=True)
        changed = copy.deepcopy(spec)
        changed["steps"][0]["args"]["value"] = "changed"
        with self.assertRaises(IdentityMismatchError):
            runtime.run(changed, {"value": "original"}, run_id="identity", resume=True)
        other = Runtime(self.db, Path(self.temp.name) / "other")
        with self.assertRaises(IdentityMismatchError):
            other.run(spec, {"value": "original"}, run_id="identity", resume=True)

    def test_invalid_inputs_fail_before_run_is_created(self):
        spec = workflow([step("first", "core.value")], inputs={"count": {"type": "integer"}})
        runtime = self.runtime()
        for invalid in ({}, {"count": True}, {"count": 1, "surprise": 2}):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                runtime.run(spec, invalid, run_id="invalid")
        with self.assertRaises(KeyError):
            runtime.inspect("invalid")

    def test_hard_timeout_kills_worker_even_if_it_ignores_sigterm(self):
        spec = workflow([step("slow", "test.slow", timeout_seconds=0.08)])
        runtime = self.runtime(tools={"test.slow": ignores_termination})
        start = time.monotonic()
        result = runtime.run(spec, {}, run_id="timeout")
        self.assertEqual(result["status"], "failed")
        self.assertIn("TimeoutError", result["error"])
        self.assertLess(time.monotonic() - start, 1.5)
        time.sleep(0.5)
        self.assertFalse((self.workspace / "late-effect").exists())
        self.assertEqual(runtime.inspect("timeout")["steps"][0]["status"], "failed")

    def test_concurrent_same_run_is_excluded(self):
        spec = workflow([step("wait", "test.wait", timeout_seconds=5)])
        runtime = self.runtime(tools={"test.wait": wait_for_release})
        process = multiprocessing.get_context("fork").Process(
            target=execute_waiting, args=(str(self.db), str(self.workspace), spec)
        )
        process.start()
        try:
            deadline = time.monotonic() + 3
            while not (self.workspace / "entered").exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue((self.workspace / "entered").exists())
            with self.assertRaises(RunBusyError):
                runtime.run(spec, {}, run_id="concurrent", resume=True)
            with self.assertRaises(RunBusyError):
                runtime.recover("concurrent", policy="retry")
        finally:
            (self.workspace / "release").touch()
            process.join(4)
            if process.is_alive():
                process.kill()
                process.join()
            process.close()
        self.assertEqual(runtime.inspect("concurrent")["status"], "completed")

    def test_returning_worker_cannot_leave_background_effects_or_run_locks(self):
        spec = workflow([step("spawn", "test.spawn")])
        runtime = self.runtime(tools={"test.spawn": spawn_background_effect})
        result = runtime.run(spec, {}, run_id="background")
        self.assertEqual(result["status"], "completed")
        # A surviving forked child would hold the run lock, even after success.
        self.assertEqual(runtime.run(spec, {}, run_id="background", resume=True), result)
        time.sleep(0.35)
        self.assertFalse((self.workspace / "background-effect").exists())

    def test_interrupted_run_requires_explicit_recovery(self):
        spec = workflow([step("gate", "test.gate")])
        runtime = self.runtime(tools={"test.gate": require_gate})
        runtime.run(spec, {}, run_id="interrupted")
        # Emulate the durable state after the parent vanished during a tool call.
        with sqlite3.connect(self.db) as connection:
            connection.execute("UPDATE runs SET status='running' WHERE run_id='interrupted'")
            connection.execute("UPDATE steps SET status='running' WHERE run_id='interrupted'")
        with self.assertRaises(RecoveryRequiredError):
            runtime.run(spec, {}, run_id="interrupted", resume=True)
        with self.assertRaises(ValueError):
            runtime.recover("interrupted", policy="guess")
        state = runtime.recover("interrupted", policy="retry")
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["steps"][0]["status"], "failed")
        (self.workspace / "gate").touch()
        self.assertEqual(runtime.run(spec, {}, run_id="interrupted", resume=True)["status"], "completed")

    def test_ai_registry_is_explicit_and_separate(self):
        spec = workflow([step("ai", "test.provider", kind="ai")], outputs={"value": {"$ref": "steps.ai.nested.value"}})
        missing = self.runtime(tools={"test.provider": record}).run(spec, {}, run_id="no-ai")
        self.assertEqual(missing["status"], "failed")
        self.assertIn("Unregistered ai tool", missing["error"])
        result = self.runtime(ai_tools={"test.provider": record}).run(spec, {}, run_id="ai")
        self.assertEqual(result["outputs"], {"value": "recorded"})

    def test_invalid_output_and_missing_reference_fail_visibly(self):
        invalid = workflow([step("bad", "test.invalid")])
        for index, provider in enumerate((invalid_result, tuple_result, nonstring_key_result)):
            with self.subTest(provider=provider.__name__):
                runtime = self.runtime(tools={"test.invalid": provider})
                result = runtime.run(invalid, {}, run_id=f"invalid-output-{index}")
                self.assertEqual(result["status"], "failed")
        missing = workflow([step("value", "core.value", args={"value": "ok"})],
                           outputs={"absent": {"$ref": "steps.value.absent"}})
        result = self.runtime().run(missing, {}, run_id="missing-field")
        self.assertEqual(result["status"], "failed")
        self.assertIn("Reference has no field", result["error"])
        self.assertEqual(self.runtime().inspect("missing-field")["steps"][0]["status"], "completed")


if __name__ == "__main__":
    unittest.main()
