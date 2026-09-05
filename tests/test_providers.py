"""Mocked HTTP contract tests. These tests never contact an AI service."""

import io
import json
import unittest
from unittest.mock import patch
import urllib.error

from agentic_workflow.ir import ValidationError
from agentic_workflow.parser import compile_skill
from agentic_workflow.providers import ChatCompletionsProvider, ProviderError, _NoRedirect
from agentic_workflow.worker import TaskContext


def workflow():
    return {
        "ir_version": "0.1", "id": "example", "inputs": {},
        "steps": [{"id": "value", "kind": "tool", "tool": "core.value", "args": {"value": 42}}],
        "outputs": {"answer": {"$ref": "steps.value.value"}},
    }


def envelope(content, **choice_fields):
    return json.dumps({"choices": [{"message": {"role": "assistant", "content": content}, **choice_fields}]}).encode()


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.opener_patch = patch("agentic_workflow.providers.urllib.request.build_opener")
        self.opener_builder = self.opener_patch.start()
        self.addCleanup(self.opener_patch.stop)
        self.opener = self.opener_builder.return_value
        self.provider = ChatCompletionsProvider("http://localhost:11434/v1/chat/completions", "local-model", timeout_seconds=12)
        self.context = TaskContext(workspace="/private/workspace", run_id="run", step_id="ai", idempotency_key="key")

    def respond(self, body):
        self.opener.open.return_value = io.BytesIO(body)

    def request_payload(self):
        return json.loads(self.opener.open.call_args.args[0].data)

    def test_constructor_makes_no_network_request(self):
        self.opener.open.assert_not_called()
        self.assertIsInstance(self.opener_builder.call_args.args[0], _NoRedirect)

    def test_semantic_extraction_payload_and_ir_validation(self):
        self.respond(envelope(json.dumps(workflow())))
        result = compile_skill("Return the number 42.", extractor=self.provider)
        self.assertEqual(result, workflow())
        request = self.opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "http://localhost:11434/v1/chat/completions")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(self.opener.open.call_args.kwargs, {"timeout": 12})
        payload = self.request_payload()
        self.assertEqual(payload["model"], "local-model")
        self.assertEqual(payload["messages"][1], {"role": "user", "content": "Return the number 42."})
        self.assertIn("ir_version", payload["messages"][0]["content"])
        self.assertIn("max_attempts", payload["messages"][0]["content"])
        self.assertNotIn("response_format", payload)
        self.assertIsNone(request.get_header("Authorization"))
        self.assertIsNone(request.get_header("Idempotency-key"))

    def test_optional_auth_and_instruction_without_context_leak(self):
        provider = ChatCompletionsProvider("https://ai.example.invalid/v1/chat/completions", "model", api_key="test-secret")
        self.respond(envelope("A concise summary."))
        self.assertEqual(provider({"text": "Document content", "instruction": "Summarize in two sentences."}, self.context), {"text": "A concise summary."})
        request = self.opener.open.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer test-secret")
        self.assertEqual(request.get_header("Idempotency-key"), self.context.idempotency_key)
        payload = self.request_payload()
        self.assertEqual(payload["messages"][0]["content"], "Summarize in two sentences.")
        self.assertEqual(payload["messages"][1]["content"], "Document content")
        self.assertNotIn("/private/workspace", request.data.decode())
        self.assertNotIn("test-secret", request.data.decode())

    def test_default_ai_text_task(self):
        self.respond(envelope("Hello."))
        self.assertEqual(self.provider({"text": "Say hello."}, self.context), {"text": "Hello."})

    def test_invalid_idempotency_header_fails_before_network(self):
        context = TaskContext(workspace="/workspace", run_id="run", step_id="ai", idempotency_key="bad\nheader")
        with self.assertRaisesRegex(ValidationError, "invalid idempotency"):
            self.provider({"text": "Input"}, context)
        self.opener.open.assert_not_called()

    def test_extraction_accepts_only_complete_json_or_one_fence(self):
        valid = json.dumps(workflow())
        for content in (valid, "```json\n" + valid + "\n```", "~~~workflow-ir\n" + valid + "\n~~~", "```\n" + valid + "\n```"):
            with self.subTest(content=content[:20]):
                self.respond(envelope(content))
                self.assertEqual(self.provider.extract("Return 42"), workflow())
        for content in ("Here is the JSON: " + valid, "```json\n" + valid + "\n```\n```json\n{}\n```", "[1, 2]", "{broken}"):
            with self.subTest(content=content[:20]):
                self.respond(envelope(content))
                with self.assertRaisesRegex(ValidationError, "invalid workflow IR"):
                    self.provider.extract("Return 42")

    def test_untrusted_extraction_rejects_invalid_ir(self):
        candidates = []
        candidate = workflow()
        candidate["extra"] = "forbidden"
        candidates.append(json.dumps(candidate))
        candidate = workflow()
        candidate["steps"][0]["kind"] = "shell"
        candidates.append(json.dumps(candidate))
        candidate = workflow()
        candidate["steps"][0]["args"] = {"value": {"$ref": "steps.value.value"}}
        candidates.append(json.dumps(candidate))
        candidates.extend(['{"id":"first","id":"duplicate"}', '{"value": NaN}', '{"value": 1e999}'])
        for content in candidates:
            with self.subTest(content=content[:50]):
                self.respond(envelope(content))
                with self.assertRaises(ValidationError):
                    self.provider.extract("Skill")

    def test_invalid_endpoint_never_exposes_credentials(self):
        for endpoint in ("file:///etc/passwd", "ftp://example.invalid/file", "https://user:secret@example.invalid/v1", "https://@example.invalid/v1",
                         "https://example.invalid/v1#fragment", "https://example.invalid:99999/v1", "https://example.invalid/\nheader", "relative/path", None):
            with self.subTest(endpoint=endpoint):
                with self.assertRaises(ValueError) as raised:
                    ChatCompletionsProvider(endpoint, "model")
                self.assertNotIn("secret", str(raised.exception))
        self.opener.open.assert_not_called()

    def test_invalid_configuration(self):
        for timeout in (0, -1, 3601, True, "30", float("nan"), float("inf")):
            with self.subTest(timeout=timeout):
                with self.assertRaises(ValueError):
                    ChatCompletionsProvider("https://example.invalid/v1", "model", timeout_seconds=timeout)
        for model in ("", " ", "bad\nmodel", None):
            with self.subTest(model=model):
                with self.assertRaises(ValueError):
                    ChatCompletionsProvider("https://example.invalid/v1", model)
        for key in ("", " ", "bad\nsecret", 123):
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    ChatCompletionsProvider("https://example.invalid/v1", "model", api_key=key)

    def test_invalid_task_arguments_fail_before_network(self):
        for args in ({}, {"text": None}, {"text": " "}, {"text": "x", "instruction": 1}, {"text": "x", "temperature": 0.5}, []):
            with self.subTest(args=args):
                with self.assertRaises(ValidationError):
                    self.provider(args, self.context)
        for text in (None, "", " "):
            with self.subTest(text=text):
                with self.assertRaises(ValidationError):
                    self.provider.extract(text)
        self.opener.open.assert_not_called()

    def test_errors_are_sanitized_and_requests_not_retried(self):
        secret_url = "https://example.invalid/v1?token=url-secret"
        for error in (urllib.error.HTTPError(secret_url, 401, "api-secret", {}, io.BytesIO(b"response-secret")),
                      urllib.error.URLError("api-secret at " + secret_url), TimeoutError("api-secret")):
            with self.subTest(error=type(error).__name__):
                self.opener.open.reset_mock()
                self.opener.open.side_effect = error
                with self.assertRaises(ProviderError) as raised:
                    self.provider({"text": "User data"}, self.context)
                self.assertNotIn("secret", str(raised.exception))
                self.assertNotIn("example.invalid", str(raised.exception))
                self.assertIsNone(raised.exception.__cause__)
                self.opener.open.assert_called_once()

    def test_redirects_are_refused(self):
        handler = _NoRedirect()
        self.assertIsNone(handler.redirect_request(None, None, 302, "redirect", {}, "https://another.invalid"))

    def test_malformed_empty_and_truncated_responses(self):
        bodies = [b"not-json-response-secret", b"\xff", b"[]", b"{}", b'{"error":{"message":"secret"}}',
                  envelope(None), envelope(" "), envelope("Partial", finish_reason="length"),
                  envelope("Filtered", finish_reason="content_filter"),
                  b'{"choices":[{"message":{"content":"secret", "refusal":"no"}}]}',
                  b'{"choices":[], "choices":[]}', b'{"extra": NaN}']
        for body in bodies:
            with self.subTest(body=body[:40]):
                self.respond(body)
                with self.assertRaises(ProviderError) as raised:
                    self.provider({"text": "Input"}, self.context)
                self.assertNotIn("secret", str(raised.exception))

    def test_response_size_limit(self):
        with patch("agentic_workflow.providers.MAX_RESPONSE_BYTES", 20):
            self.respond(b"x" * 21)
            with self.assertRaisesRegex(ProviderError, "size limit"):
                self.provider({"text": "Input"}, self.context)


if __name__ == "__main__":
    unittest.main()
