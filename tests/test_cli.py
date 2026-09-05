"""End-to-end checks through the public command-line boundary."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name)
        self.db = self.workspace / "state.sqlite3"
        self.inputs = self.workspace / "inputs.json"
        self.inputs.write_text('{"text":"hello"}', encoding="utf-8")
        self.workflow = {
            "ir_version": "0.1", "id": "cli_demo",
            "inputs": {"text": {"type": "string"}},
            "steps": [{"id": "write", "kind": "tool", "tool": "files.write_text",
                       "args": {"path": "result.txt", "text": {"$ref": "inputs.text"}}}],
            "outputs": {"path": {"$ref": "steps.write.path"}},
        }
        self.skill = self.workspace / "SKILL.md"
        self.skill.write_text("# Demo\n\n```workflow-ir\n" + json.dumps(self.workflow)
                              + "\n```\n", encoding="utf-8")
        self.ir = self.workspace / "build" / "workflow.json"

    def cli(self, *args, expected=0):
        env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
        result = subprocess.run([sys.executable, "-m", "agentic_workflow", *map(str, args)],
                                cwd=self.workspace, env=env, text=True,
                                capture_output=True, timeout=20)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return json.loads(result.stdout if result.stdout else result.stderr)

    def run_args(self):
        return ("run", self.ir, "--inputs", self.inputs, "--workspace", self.workspace,
                "--db", self.db, "--run-id", "cli-run")

    def test_compile_run_inspect_events_and_resume(self):
        self.cli("compile", self.skill, "-o", self.ir)
        self.assertFalse((self.workspace / "result.txt").exists())
        self.assertFalse(self.db.exists())
        self.assertEqual(self.cli("validate", self.ir)["status"], "valid")
        result = self.cli(*self.run_args())
        self.assertEqual(result["status"], "completed")
        output = self.workspace / "result.txt"
        self.assertEqual(output.read_text(encoding="utf-8"), "hello")
        original_mtime = output.stat().st_mtime_ns
        self.assertEqual(self.cli("inspect", "cli-run", "--db", self.db)["status"], "completed")
        self.assertTrue(self.cli("events", "cli-run", "--db", self.db))
        self.cli(*self.run_args(), "--resume")
        self.assertEqual(output.stat().st_mtime_ns, original_mtime)

    def test_invalid_ir_and_duplicate_json_do_not_execute(self):
        self.ir.parent.mkdir()
        self.ir.write_text('{"id":"x","id":"y"}', encoding="utf-8")
        error = self.cli("validate", self.ir, expected=2)
        self.assertIn("duplicate", error["error"])
        self.assertFalse((self.workspace / "result.txt").exists())

    def test_bad_inputs_return_json_error_and_no_effect(self):
        self.cli("compile", self.skill, "-o", self.ir)
        self.inputs.write_text('{"text": false}', encoding="utf-8")
        self.cli(*self.run_args(), expected=2)
        self.assertFalse((self.workspace / "result.txt").exists())

    def test_failure_returns_nonzero_and_is_inspectable(self):
        self.workflow["steps"][0] = {
            "id": "missing", "kind": "tool", "tool": "files.require_exists",
            "args": {"path": "nonexistent.txt"},
        }
        self.workflow["outputs"] = {}
        self.ir.parent.mkdir()
        self.ir.write_text(json.dumps(self.workflow), encoding="utf-8")
        result = self.cli(*self.run_args(), expected=1)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.cli("inspect", "cli-run", "--db", self.db)["status"], "failed")

    def test_semantic_compile_requires_explicit_configuration(self):
        error = self.cli("compile", self.skill, "-o", self.ir, "--semantic", expected=2)
        self.assertIn("--endpoint and --model", error["error"])
        self.assertFalse(self.ir.exists())

    def test_deep_json_returns_structured_error(self):
        self.ir.parent.mkdir()
        self.ir.write_text("[" * 2000 + "0" + "]" * 2000, encoding="utf-8")
        error = self.cli("validate", self.ir, expected=2)
        self.assertIn("nesting", error["error"])


if __name__ == "__main__":
    unittest.main()
