"""Contract and rejection tests for untrusted workflow documents."""

import copy
import json
from pathlib import Path
import unittest

from agentic_workflow.adapters import BackendAdapter, BackendCapabilities, LOCAL_RUNTIME_CAPABILITIES
from agentic_workflow.ir import ValidationError, validate_inputs, validate_workflow
from agentic_workflow.parser import SemanticExtractor, compile_skill


def workflow():
    return {
        "ir_version": "0.1",
        "id": "document_pipeline",
        "inputs": {"path": {"type": "string"}, "use_ai": {"type": "boolean", "default": False}},
        "steps": [
            {"id": "read", "kind": "tool", "tool": "files.read_text", "args": {"path": {"$ref": "inputs.path"}}},
            {"id": "normalize", "kind": "tool", "tool": "text.normalize", "args": {"text": {"$ref": "steps.read.text"}},
             "retry": {"max_attempts": 2, "delay_seconds": 0}, "timeout_seconds": 5,
             "when": {"equals": [{"$ref": "inputs.use_ai"}, False]}},
        ],
        "outputs": {"text": {"$ref": "steps.normalize.text"}},
    }


class WorkflowValidationTests(unittest.TestCase):
    def test_valid_workflow_and_nested_literals(self):
        candidate = workflow()
        candidate["steps"][1]["args"]["nested"] = [None, 1, True, {"items": [{"$ref": "steps.read.metadata.title"}]}]
        self.assertIs(validate_workflow(candidate), candidate)

    def test_every_top_level_field_is_required(self):
        for key in workflow():
            with self.subTest(key=key):
                candidate = workflow()
                del candidate[key]
                with self.assertRaisesRegex(ValidationError, "missing required keys"):
                    validate_workflow(candidate)

    def test_unknown_keys_rejected_at_contract_boundaries(self):
        for where in ("workflow", "input", "step", "retry", "when"):
            with self.subTest(where=where):
                candidate = workflow()
                target = {"workflow": candidate, "input": candidate["inputs"]["path"], "step": candidate["steps"][1],
                          "retry": candidate["steps"][1]["retry"], "when": candidate["steps"][1]["when"]}[where]
                target["typo"] = True
                with self.assertRaisesRegex(ValidationError, "unknown keys"):
                    validate_workflow(candidate)

    def test_unsupported_version_kinds_and_identifiers(self):
        for field, bad in (("ir_version", "0.2"), ("id", "name-with-hyphen"), ("id", "1start")):
            with self.subTest(field=field, bad=bad):
                candidate = workflow()
                candidate[field] = bad
                with self.assertRaises(ValidationError):
                    validate_workflow(candidate)
        for kind in ("shell", "parallel", "loop", "approval", None, []):
            with self.subTest(kind=kind):
                candidate = workflow()
                candidate["steps"][0]["kind"] = kind
                with self.assertRaises(ValidationError):
                    validate_workflow(candidate)

    def test_unique_steps_and_only_prior_references(self):
        for ref in ("steps.read.text", "steps.normalize.text", "steps.missing.text"):
            with self.subTest(ref=ref):
                candidate = workflow()
                candidate["steps"][0]["args"]["path"] = {"$ref": ref}
                with self.assertRaisesRegex(ValidationError, "not a prior step"):
                    validate_workflow(candidate)
        candidate = workflow()
        candidate["steps"][1]["id"] = "read"
        with self.assertRaisesRegex(ValidationError, "duplicate step"):
            validate_workflow(candidate)

    def test_invalid_reference_forms(self):
        for ref in ("inputs.missing", "inputs.path.extra", "steps.read", "steps.read.", "steps.read..text", "env.SECRET", 1):
            with self.subTest(ref=ref):
                candidate = workflow()
                candidate["outputs"] = {"bad": {"$ref": ref}}
                with self.assertRaises(ValidationError):
                    validate_workflow(candidate)
        candidate = workflow()
        candidate["outputs"] = {"bad": {"$ref": "inputs.path", "default": "fallback"}}
        with self.assertRaisesRegex(ValidationError, "exactly"):
            validate_workflow(candidate)

    def test_only_equals_conditions_with_two_operands(self):
        for condition in ({"truthy": True}, {"equals": [1]}, {"equals": [1, 2, 3]}, {"equals": "yes"}, True):
            with self.subTest(condition=condition):
                candidate = workflow()
                candidate["steps"][1]["when"] = condition
                with self.assertRaises(ValidationError):
                    validate_workflow(candidate)

    def test_retry_and_timeout_bounds(self):
        for key, invalid_values in (("max_attempts", [0, -1, 11, 1.5, True, "2"]),
                                    ("delay_seconds", [-1, 3601, True, "0"]),
                                    ("timeout_seconds", [0, -1, 3601, True, "3"])):
            for value in invalid_values:
                with self.subTest(key=key, value=value):
                    candidate = workflow()
                    target = candidate["steps"][1] if key == "timeout_seconds" else candidate["steps"][1]["retry"]
                    target[key] = value
                    with self.assertRaises(ValidationError):
                        validate_workflow(candidate)
        candidate = workflow()
        candidate["steps"][1]["retry"] = {"max_attempts": 10, "delay_seconds": 3600}
        candidate["steps"][1]["timeout_seconds"] = 0.001
        validate_workflow(candidate)

    def test_non_json_and_non_finite_values_rejected_recursively(self):
        for value in (float("nan"), float("inf"), float("-inf"), {"a", "b"}, (1, 2), b"bytes", {1: "number key"}):
            with self.subTest(value=repr(value)):
                candidate = workflow()
                candidate["outputs"]["nested"] = [{"bad": value}]
                with self.assertRaises(ValidationError):
                    validate_workflow(candidate)

    def test_recursive_and_overdeep_data_rejected_without_recursion_error(self):
        candidate = workflow()
        candidate["outputs"]["cycle"] = candidate
        with self.assertRaisesRegex(ValidationError, "nesting"):
            validate_workflow(candidate)

    def test_steps_and_named_expression_maps(self):
        for value in ([], {}, "steps"):
            with self.subTest(value=value):
                candidate = workflow()
                candidate["steps"] = value
                with self.assertRaises(ValidationError):
                    validate_workflow(candidate)
        candidate = workflow()
        candidate["steps"][0]["args"] = {"$ref": "inputs.path"}
        with self.assertRaisesRegex(ValidationError, "argument names"):
            validate_workflow(candidate)

    def test_tool_names(self):
        for value in ("", " ", " name", "name\n", "bad\x00name", "x" * 257, 1):
            with self.subTest(value=value):
                candidate = workflow()
                candidate["steps"][0]["tool"] = value
                with self.assertRaises(ValidationError):
                    validate_workflow(candidate)


class InputValidationTests(unittest.TestCase):
    def test_missing_unknown_and_wrong_type(self):
        for supplied, message in (({}, "required"), ({"path": "a", "extra": 1}, "undeclared"), ({"path": 1}, "string")):
            with self.subTest(supplied=supplied):
                with self.assertRaisesRegex(ValidationError, message):
                    validate_inputs(workflow(), supplied)

    def test_defaults_and_caller_values_are_copied(self):
        candidate = workflow()
        candidate["inputs"]["options"] = {"type": "object", "default": {"nested": []}}
        supplied = {"path": "input.txt"}
        result = validate_inputs(candidate, supplied)
        self.assertEqual(result["use_ai"], False)
        result["options"]["nested"].append("change")
        self.assertEqual(candidate["inputs"]["options"]["default"], {"nested": []})
        self.assertEqual(supplied, {"path": "input.txt"})

    def test_input_types_and_boolean_numeric_separation(self):
        for kind, good, bad in (("number", 1.5, True), ("integer", 2, 2.0), ("boolean", False, 0),
                                ("string", "yes", None), ("object", {}, []), ("array", [], {})):
            with self.subTest(kind=kind):
                candidate = workflow()
                candidate["inputs"]["value"] = {"type": kind}
                self.assertEqual(validate_inputs(candidate, {"path": "x", "value": good})["value"], good)
                with self.assertRaises(ValidationError):
                    validate_inputs(candidate, {"path": "x", "value": bad})
                candidate["inputs"]["value"]["default"] = bad
                with self.assertRaisesRegex(ValidationError, "default"):
                    validate_workflow(candidate)

    def test_input_defaults_are_literals_not_interpreted_references(self):
        candidate = workflow()
        candidate["inputs"]["literal"] = {"type": "object", "default": {"$ref": "not.a.reference"}}
        self.assertEqual(validate_inputs(candidate, {"path": "x"})["literal"], {"$ref": "not.a.reference"})


class ParserTests(unittest.TestCase):
    def test_structured_fence_with_surrounding_prose(self):
        candidate = workflow()
        text = "# Normalize a document\n\nProse is documentation.\n\n```workflow-ir\n" + json.dumps(candidate) + "\n```\n"
        self.assertEqual(compile_skill(text), candidate)

    def test_tilde_fence_and_longer_closing_fence(self):
        self.assertEqual(compile_skill("~~~workflow-ir\n" + json.dumps(workflow()) + "\n~~~~"), workflow())

    def test_exactly_one_explicit_fence_no_prose_guessing(self):
        valid = "```workflow-ir\n" + json.dumps(workflow()) + "\n```"
        for text in ("Read, normalize, and save a file", json.dumps(workflow()), valid + "\n" + valid,
                     "```json\n" + json.dumps(workflow()) + "\n```", "```workflow-ir\n{}"):
            with self.subTest(text=text[:40]):
                with self.assertRaises(ValidationError):
                    compile_skill(text)

    def test_quoted_markdown_example_is_not_a_workflow(self):
        text = "````markdown\n```workflow-ir\n" + json.dumps(workflow()) + "\n```\n````"
        with self.assertRaisesRegex(ValidationError, "found 0"):
            compile_skill(text)

    def test_invalid_json_duplicate_keys_and_constants(self):
        for payload in ('{"id": "a", "id": "b"}', '{"value": NaN}', '{"value": Infinity}', '{bad}', '[]'):
            with self.subTest(payload=payload):
                with self.assertRaises(ValidationError):
                    compile_skill("```workflow-ir\n" + payload + "\n```")
        candidate = workflow()
        payload = json.dumps(candidate).replace('"outputs": {', '"outputs": {"overflow": 1e999,')
        with self.assertRaisesRegex(ValidationError, "finite"):
            compile_skill("```workflow-ir\n" + payload + "\n```")

    def test_semantic_extractor_is_an_explicit_always_validated_boundary(self):
        class Extractor:
            def __init__(self, result):
                self.result = result
                self.received = None

            def extract(self, text):
                self.received = text
                return self.result

        extractor = Extractor(workflow())
        self.assertIsInstance(extractor, SemanticExtractor)
        self.assertEqual(compile_skill("Caller-owned semantic interpretation", extractor), workflow())
        self.assertEqual(extractor.received, "Caller-owned semantic interpretation")
        with self.assertRaises(ValidationError):
            compile_skill("Anything", Extractor({"arbitrary": "unvalidated"}))
        with self.assertRaisesRegex(ValidationError, "implement extract"):
            compile_skill("Anything", object())

    def test_bad_text_type(self):
        with self.assertRaisesRegex(ValidationError, "expected text"):
            compile_skill(None)

    def test_excessive_integer_digits_have_a_validation_error(self):
        # Python 3.11+ bounds integer conversion to avoid excessive CPU usage.
        import sys
        limit = sys.get_int_max_str_digits()
        if not limit:
            self.skipTest("integer conversion limit is disabled")
        with self.assertRaisesRegex(ValidationError, "numeric literal"):
            compile_skill('```workflow-ir\n{"large": ' + "1" * (limit + 1) + '}\n```')


class SchemaAndAdapterTests(unittest.TestCase):
    def test_schema_is_json_and_declares_contract_version(self):
        schema = json.loads((Path(__file__).resolve().parents[1] / "schemas/workflow-ir.schema.json").read_text())
        self.assertEqual(schema["properties"]["ir_version"], {"const": "0.1"})
        self.assertFalse(schema["additionalProperties"])

    def test_backend_contract_does_not_claim_remote_implementations(self):
        self.assertFalse(BackendCapabilities().human_approval)
        self.assertFalse(LOCAL_RUNTIME_CAPABILITIES.parallel_steps)
        self.assertTrue(LOCAL_RUNTIME_CAPABILITIES.hard_timeouts)

        class FutureAdapter:
            name = "test-only"
            capabilities = BackendCapabilities()

            def compile(self, workflow):
                return copy.deepcopy(workflow)

        self.assertIsInstance(FutureAdapter(), BackendAdapter)


if __name__ == "__main__":
    unittest.main()
