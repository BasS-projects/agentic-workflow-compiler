"""Strict IR 0.2 validation: bounded structured control flow, no executable code."""
from __future__ import annotations

import copy
from . import ir

IR_VERSION = "0.2"
MAX_NESTING = 8
MAX_NODES = 1000


def _expression(value, inputs, prior, in_loop, path):
    if type(value) is list:
        for index, item in enumerate(value):
            _expression(item, inputs, prior, in_loop, f"{path}[{index}]")
    elif type(value) is dict:
        if "$ref" in value:
            if set(value) != {"$ref"} or type(value["$ref"]) is not str:
                ir._fail(path, "a reference must contain exactly one string $ref")
            parts = value["$ref"].split(".")
            if len(parts) < 2 or any(not p for p in parts):
                ir._fail(path, "reference requires nonempty dotted segments")
            if parts[0] == "inputs":
                if parts[1] not in inputs:
                    ir._fail(path, "reference to undeclared input")
            elif parts[0] == "steps":
                if parts[1] not in prior or len(parts) < 3:
                    ir._fail(path, "reference must address a prior step field")
            elif parts[0] == "loop":
                if not in_loop or parts[1] not in {"item", "index"}:
                    ir._fail(path, "loop.item and loop.index require an enclosing foreach")
                if parts[1] == "index" and len(parts) != 2:
                    ir._fail(path, "loop.index has no nested fields")
            else:
                ir._fail(path, "reference root must be inputs, steps, or loop")
        else:
            for key, item in value.items():
                _expression(item, inputs, prior, in_loop, f"{path}.{key}")


def _scope(nodes, inputs, outer, in_loop, depth, counter, path):
    if depth > MAX_NESTING:
        ir._fail(path, f"control flow nesting exceeds {MAX_NESTING}")
    if type(nodes) is not list or not 1 <= len(nodes) <= MAX_NODES:
        ir._fail(path, f"expected 1..{MAX_NODES} nodes")
    prior, local = set(outer), set()
    for index, node in enumerate(nodes):
        counter[0] += 1
        if counter[0] > MAX_NODES:
            ir._fail(path, f"workflow exceeds {MAX_NODES} static nodes")
        p = f"{path}[{index}]"
        if type(node) is not dict:
            ir._fail(p, "expected an object")
        kind = node.get("kind")
        if kind in ("tool", "ai"):
            required = {"id", "kind", "tool", "args"}
            optional = {"when", "retry", "timeout_seconds"}
        elif kind == "foreach":
            required, optional = {"id", "kind", "items", "steps", "max_items"}, {"when"}
        elif kind == "parallel":
            required, optional = {"id", "kind", "branches", "max_workers"}, {"when"}
        elif kind == "approval":
            required, optional = {"id", "kind", "prompt"}, {"when"}
        else:
            ir._fail(p, "kind must be tool, ai, foreach, parallel, or approval")
        ir._object(node, p, required, optional)
        ir._identifier(node["id"], p + ".id")
        if node["id"] in local:
            ir._fail(p + ".id", "duplicate sibling node identifier")
        if "when" in node:
            ir._object(node["when"], p + ".when", {"equals"}, set())
            operands = node["when"]["equals"]
            if type(operands) is not list or len(operands) != 2:
                ir._fail(p + ".when.equals", "expected exactly two operands")
            _expression(operands, inputs, prior, in_loop, p + ".when")
        if kind in ("tool", "ai"):
            # Delegate leaf-specific fields and bounds to the existing validator,
            # replacing expressions only in this validation-only synthetic node.
            if type(node["args"]) is not dict or "$ref" in node["args"]:
                ir._fail(p + ".args", "args must map names to expressions")
            _expression(node["args"], inputs, prior, in_loop, p + ".args")
            synthetic = {k: copy.deepcopy(v) for k, v in node.items() if k != "when"}
            synthetic["args"] = {}
            ir.validate_workflow({"ir_version": "0.1", "id": "leaf", "inputs": {},
                                  "steps": [synthetic], "outputs": {}})
        elif kind == "foreach":
            limit = node["max_items"]
            if type(limit) is not int or not 1 <= limit <= 1000:
                ir._fail(p + ".max_items", "expected an integer in 1..1000")
            _expression(node["items"], inputs, prior, in_loop, p + ".items")
            _scope(node["steps"], inputs, prior, True, depth + 1, counter, p + ".steps")
        elif kind == "parallel":
            limit = node["max_workers"]
            if type(limit) is not int or not 1 <= limit <= 8:
                ir._fail(p + ".max_workers", "expected an integer in 1..8")
            branches = node["branches"]
            if type(branches) is not dict or not 1 <= len(branches) <= 100:
                ir._fail(p + ".branches", "expected 1..100 named branches")
            for name, branch in branches.items():
                ir._identifier(name, p + ".branches")
                _scope(branch, inputs, prior, in_loop, depth + 1, counter, p + ".branches." + name)
        else:
            if type(node["prompt"]) is not str or not node["prompt"].strip() or len(node["prompt"]) > 10000:
                ir._fail(p + ".prompt", "expected nonblank text of at most 10000 characters")
        local.add(node["id"])
        prior.add(node["id"])
    return prior


def validate_workflow_v2(workflow):
    ir.validate_json(workflow, "workflow")
    ir._object(workflow, "workflow", {"ir_version", "id", "inputs", "steps", "outputs"}, set())
    if workflow["ir_version"] != IR_VERSION:
        ir._fail("workflow.ir_version", "only '0.2' is supported")
    # Reuse the complete input-definition validator without weakening v0.1.
    shell = {"ir_version": "0.1", "id": workflow["id"], "inputs": workflow["inputs"],
             "steps": [{"id": "dummy", "kind": "tool", "tool": "core.value", "args": {}}], "outputs": {}}
    ir.validate_workflow(shell)
    prior = _scope(workflow["steps"], set(workflow["inputs"]), set(), False, 1, [0], "workflow.steps")
    if type(workflow["outputs"]) is not dict or "$ref" in workflow["outputs"]:
        ir._fail("workflow.outputs", "expected an object mapping names to expressions")
    _expression(workflow["outputs"], set(workflow["inputs"]), prior, False, "workflow.outputs")
    return workflow


def validate_inputs_v2(workflow, inputs):
    validate_workflow_v2(workflow)
    shell = {"ir_version": "0.1", "id": workflow["id"], "inputs": workflow["inputs"],
             "steps": [{"id": "dummy", "kind": "tool", "tool": "core.value", "args": {}}], "outputs": {}}
    return ir.validate_inputs(shell, inputs)


def migrate_v1(workflow):
    """Lossless structural migration; v0.1 references retain their semantics."""
    result = copy.deepcopy(ir.validate_workflow(workflow))
    result["ir_version"] = IR_VERSION
    return validate_workflow_v2(result)
