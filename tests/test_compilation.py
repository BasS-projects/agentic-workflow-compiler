import copy
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import threading
import unittest

from agentic_workflow.compilation import (
    CompilationError, SemanticCompileProvider, approve_bundle, compile_bundle,
    evaluate_cases, optimize_workflow, verify_bundle,
)
from agentic_workflow.providers import ChatCompletionsProvider, ProviderError


def spec():
    return {"ir_version": "0.1", "id": "normalize_document", "inputs": {}, "steps": [
        {"id": "normalize", "kind": "tool", "tool": "text.normalize", "args": {"text": " A  B "}}
    ], "outputs": {"text": {"$ref": "steps.normalize.text"}}}


def source(workflow=None):
    return "Normalize the document.\n```workflow-ir\n" + json.dumps(workflow or spec()) + "\n```\n"


class Extractor:
    def extract(self, text):
        if text == "unsupported":
            raise CompilationError("Semantic extraction rejected: unsupported")
        return spec()


class CompilationTests(unittest.TestCase):
    def test_pending_review_blocks_execution_and_approval_is_isolated(self):
        bundle = compile_bundle(source())
        with self.assertRaisesRegex(CompilationError, "review"):
            verify_bundle(bundle)
        approved = approve_bundle(bundle, "reviewer")
        self.assertEqual(bundle["review"], {"status": "pending"})
        self.assertEqual(verify_bundle(approved), spec())
        candidate = verify_bundle(approved)
        candidate["id"] = "changed"
        self.assertEqual(verify_bundle(approved)["id"], "normalize_document")

    def test_tampering_source_ir_provenance_diagnostics_or_review_rejected(self):
        approved = approve_bundle(compile_bundle(source()), "reviewer")
        changes = [
            lambda value: value.update(source=value["source"] + " edited"),
            lambda value: value["workflow"]["steps"][0]["args"].update(text="different"),
            lambda value: value["provenance"].update(compiler_version="forged"),
            lambda value: value["diagnostics"].clear(),
            lambda value: value["review"].update(ir_sha256="0" * 64),
        ]
        for change in changes:
            with self.subTest(change=change):
                changed = copy.deepcopy(approved)
                change(changed)
                with self.assertRaises(CompilationError):
                    verify_bundle(changed)

    def test_hash_update_still_requires_new_approval(self):
        old = approve_bundle(compile_bundle(source()), "reviewer")
        new_workflow = spec()
        new_workflow["id"] = "new_document"
        rebuilt = compile_bundle(source(new_workflow))
        rebuilt["review"] = old["review"]
        with self.assertRaisesRegex(CompilationError, "Approval"):
            verify_bundle(rebuilt)

    def test_provider_credentials_and_url_secrets_not_in_provenance(self):
        class FixtureProvider(ChatCompletionsProvider):
            def extract(self, text):
                return spec()
        provider = FixtureProvider("https://api.example.test/SECRET-PATH?token=QUERY-SECRET", "fixture-model", "KEY-SECRET")
        bundle = compile_bundle("normalize text", provider)
        serialized = json.dumps(bundle)
        for secret in ("SECRET-PATH", "QUERY-SECRET", "KEY-SECRET"):
            self.assertNotIn(secret, serialized)
        self.assertEqual(bundle["provenance"]["provider"]["origin"], "https://api.example.test")

    def test_custom_extractor_error_is_sanitized_and_not_successful_rejection(self):
        class Broken:
            api_key = "SECRET-KEY"
            def extract(self, text):
                raise RuntimeError("Authorization SECRET-KEY")
        with self.assertRaises(CompilationError) as caught:
            compile_bundle("something", Broken())
        self.assertNotIn("SECRET", str(caught.exception))
        result = evaluate_cases([{"id": "reject", "source": "something", "expect": "reject"}], Broken())
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["cases"][0]["actual"], "error")

    def test_optimizer_retains_order_effects_and_boolean_number_difference(self):
        workflow = spec()
        workflow["inputs"] = {"enabled": {"type": "boolean", "default": True}}
        workflow["steps"][0]["when"] = {"equals": [{"a": [1, True]}, {"a": [1, True]}]}
        workflow["steps"].extend([
            {"id": "false", "kind": "tool", "tool": "files.write_text", "args": {"path": "never.txt", "text": "bad"}, "when": {"equals": [True, 1]}},
            {"id": "dynamic", "kind": "tool", "tool": "core.value", "args": {"value": 1}, "when": {"equals": [{"$ref": "inputs.enabled"}, True]}},
        ])
        optimized, diagnostics = optimize_workflow(workflow)
        self.assertEqual([step["id"] for step in optimized["steps"]], ["normalize", "false", "dynamic"])
        self.assertNotIn("when", optimized["steps"][0])
        self.assertEqual(optimized["steps"][1]["when"], {"equals": [False, True]})
        self.assertEqual(optimized["steps"][2], workflow["steps"][2])
        self.assertEqual(len(diagnostics), 2)
        self.assertIn("when", workflow["steps"][0])

    def test_independent_semantic_oracle_catches_schema_valid_wrong_result(self):
        wrong = spec()
        wrong["steps"][0]["args"]["text"] = "wrong"
        cases = [
            {"id": "positive", "source": "normalize", "expect": "accept", "expected_workflow": spec()},
            {"id": "negative", "source": "unsupported", "expect": "reject"},
            {"id": "semantic_mismatch", "source": "normalize", "expect": "accept", "expected_workflow": wrong},
        ]
        report = evaluate_cases(cases, Extractor())
        self.assertEqual((report["passed"], report["failed"]), (2, 1))
        self.assertEqual(report["live_endpoint"], "not_tested")
        with self.assertRaisesRegex(ValueError, "oracle"):
            evaluate_cases([{"id": "no-oracle", "source": "normalize", "expect": "accept"}], Extractor())

    def test_structured_v02_nested_optimization_and_review(self):
        workflow = {"ir_version": "0.2", "id": "bounded", "inputs": {}, "steps": [
            {"id": "loop", "kind": "foreach", "items": [" a "], "max_items": 2,
             "steps": [{"id": "value", "kind": "tool", "tool": "core.value",
                        "args": {"value": {"$ref": "loop.item"}}, "when": {"equals": [1, 1]}}]},
        ], "outputs": {"count": {"$ref": "steps.loop.count"}}}
        approved = approve_bundle(compile_bundle(source(workflow), optimize=True), "reviewer")
        actual = verify_bundle(approved)
        self.assertEqual(actual["ir_version"], "0.2")
        self.assertNotIn("when", actual["steps"][0]["steps"][0])
        self.assertEqual(actual["steps"][0]["steps"][0]["args"], {"value": {"$ref": "loop.item"}})

    def test_http_transport_and_explicit_refusal_are_separate(self):
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append(payload)
                prompt = payload["messages"][1]["content"]
                if prompt == "outage":
                    self.send_response(503)
                    self.end_headers()
                    return
                result = {"rejected": True, "reason_code": "ambiguous"} if prompt == "ambiguous" else spec()
                body = json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(result)}}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *args):
                pass
        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            provider = SemanticCompileProvider(f"http://127.0.0.1:{server.server_port}/chat", "fixture")
            report = evaluate_cases([
                {"id": "accept", "source": "normalize", "expect": "accept", "expected_workflow": spec()},
                {"id": "ambiguous", "source": "ambiguous", "expect": "reject"},
                {"id": "outage", "source": "outage", "expect": "reject"},
            ], provider)
            self.assertEqual((report["passed"], report["failed"]), (2, 1))
            self.assertEqual(report["cases"][2]["actual"], "error")
            self.assertIn("do NOT guess", requests[0]["messages"][0]["content"])
            self.assertEqual(report["live_endpoint"], "not_tested")
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
