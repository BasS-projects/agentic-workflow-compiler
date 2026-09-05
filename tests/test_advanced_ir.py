import copy
import unittest

from agentic_workflow.advanced_ir import migrate_v1, validate_inputs_v2, validate_workflow_v2
from agentic_workflow.ir import ValidationError


def leaf(identifier="value", value=1):
    return {"id": identifier, "kind": "tool", "tool": "core.value", "args": {"value": value}}


def workflow(steps=None, outputs=None):
    return {"ir_version": "0.2", "id": "advanced", "inputs": {},
            "steps": steps or [leaf()], "outputs": outputs or {}}


class AdvancedIRTests(unittest.TestCase):
    def test_lossless_migration_and_defaults(self):
        old = workflow([leaf()], {"x": {"$ref": "steps.value.value"}})
        old["ir_version"] = "0.1"
        old["inputs"] = {"x": {"type": "array", "default": [1]}}
        migrated = migrate_v1(old)
        self.assertEqual(migrated["steps"], old["steps"])
        self.assertEqual(old["ir_version"], "0.1")
        self.assertEqual(validate_inputs_v2(migrated, {}), {"x": [1]})

    def test_nested_field_and_index_references(self):
        w = workflow([{"id": "loop", "kind": "foreach", "items": [{"text": ["a"]}], "max_items": 4,
                       "steps": [leaf(value={"$ref": "loop.item.text.0"}),
                                 leaf("index", {"$ref": "loop.index"})]}],
                     {"first": {"$ref": "steps.loop.items.0.value.value"}})
        self.assertIs(validate_workflow_v2(w), w)

    def test_bounds_and_invalid_references(self):
        cases = [workflow([leaf(value={"$ref": "loop.item"})]),
                 workflow([leaf(value={"$ref": "steps.next.value"}), leaf("next")]),
                 workflow([{"id": "p", "kind": "parallel", "branches": {"a": [leaf()]}, "max_workers": 9}]),
                 workflow([{"id": "f", "kind": "foreach", "items": [], "steps": [leaf()], "max_items": True}]),
                 workflow([leaf(), leaf()])]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(ValidationError):
                validate_workflow_v2(case)

    def test_sibling_isolation(self):
        w = workflow([{"id": "p", "kind": "parallel", "max_workers": 2,
                       "branches": {"a": [leaf("a")], "b": [leaf("b", {"$ref": "steps.a.value"})]}}])
        with self.assertRaises(ValidationError):
            validate_workflow_v2(w)

    def test_lexical_prior_outer_capture(self):
        w = workflow([leaf("outer"), {"id": "p", "kind": "parallel", "max_workers": 1,
                     "branches": {"a": [leaf(value={"$ref": "steps.outer.value"})]}}])
        validate_workflow_v2(w)

    def test_depth_bound_and_unknown_fields(self):
        nodes = [leaf()]
        for i in range(8):
            nodes = [{"id": "f" + str(i), "kind": "foreach", "items": [], "max_items": 1, "steps": nodes}]
        with self.assertRaises(ValidationError):
            validate_workflow_v2(workflow(nodes))
        w = workflow([{"id": "a", "kind": "approval", "prompt": "Review", "timeout_seconds": 1}])
        with self.assertRaises(ValidationError):
            validate_workflow_v2(w)

    def test_input_validation_unchanged(self):
        w = workflow()
        w["inputs"] = {"count": {"type": "integer"}}
        with self.assertRaises(ValidationError):
            validate_inputs_v2(w, {"count": True})


if __name__ == "__main__":
    unittest.main()
