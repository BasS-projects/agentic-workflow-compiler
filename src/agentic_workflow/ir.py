"""Strict, dependency-free validation for the version 0.1 workflow IR.

The IR contains JSON data, never executable Python. Input defaults are literal
JSON values; references are recognized only in step arguments, conditions, and
workflow outputs. These rules are also published in schemas/workflow-ir.schema.json.
"""

from __future__ import annotations

import copy
import math
import re
from typing import Any


IR_VERSION = "0.1"
MAX_STEPS = 1000
MAX_ATTEMPTS = 10
MAX_DELAY_SECONDS = 3600
MAX_TIMEOUT_SECONDS = 3600
MAX_JSON_DEPTH = 64
_ID = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_INPUT_TYPES = {"string", "number", "integer", "boolean", "object", "array"}


class ValidationError(ValueError):
    """A workflow, expression, or supplied input violates the IR contract."""


def _fail(path: str, message: str) -> None:
    raise ValidationError(f"{path}: {message}")


def _json(value: Any, path: str, depth: int = 0) -> None:
    if depth > MAX_JSON_DEPTH:
        _fail(path, f"JSON nesting exceeds {MAX_JSON_DEPTH} levels")
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float:
        if not math.isfinite(value):
            _fail(path, "numbers must be finite (NaN and Infinity are forbidden)")
        return
    if type(value) is list:
        for index, item in enumerate(value):
            _json(item, f"{path}[{index}]", depth + 1)
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                _fail(path, "JSON object keys must be strings")
            _json(item, f"{path}.{key}", depth + 1)
        return
    _fail(path, f"expected JSON data, got {type(value).__name__}")


def validate_json(value: Any, path: str = "value") -> None:
    """Reject non-JSON Python objects, non-finite numbers, and excessive nesting."""
    _json(value, path)


def _object(value: Any, path: str, required: set[str], optional: set[str]) -> None:
    if type(value) is not dict:
        _fail(path, "expected an object")
    missing = required - value.keys()
    unknown = value.keys() - required - optional
    if missing:
        _fail(path, f"missing required keys: {', '.join(sorted(missing))}")
    if unknown:
        _fail(path, f"unknown keys: {', '.join(sorted(unknown))}")


def _identifier(value: Any, path: str) -> None:
    if type(value) is not str or not _ID.fullmatch(value):
        _fail(path, "expected an identifier matching [A-Za-z_][A-Za-z0-9_]*")


def _matches_type(value: Any, declared: str) -> bool:
    if declared == "number":
        return type(value) in (int, float)
    if declared == "integer":
        return type(value) is int
    return type(value) is {
        "string": str,
        "boolean": bool,
        "object": dict,
        "array": list,
    }[declared]


def _expression(value: Any, inputs: set[str], prior_steps: set[str], path: str) -> None:
    if type(value) is list:
        for index, item in enumerate(value):
            _expression(item, inputs, prior_steps, f"{path}[{index}]")
    elif type(value) is dict:
        if "$ref" in value:
            if set(value) != {"$ref"}:
                _fail(path, "a reference must contain exactly the $ref key")
            ref = value["$ref"]
            if type(ref) is not str:
                _fail(f"{path}.$ref", "expected a string")
            parts = ref.split(".")
            if len(parts) == 2 and parts[0] == "inputs":
                if parts[1] not in inputs:
                    _fail(path, f"reference to undeclared input {parts[1]!r}")
            elif len(parts) >= 3 and parts[0] == "steps":
                if parts[1] not in prior_steps:
                    _fail(path, f"step {parts[1]!r} is unknown or is not a prior step")
                if any(not part for part in parts[2:]):
                    _fail(path, "step reference field segments must not be empty")
            else:
                _fail(path, "reference must be inputs.NAME or steps.ID.FIELD[.FIELD...]")
        else:
            for key, item in value.items():
                _expression(item, inputs, prior_steps, f"{path}.{key}")


def _bounded_number(value: Any, path: str, maximum: float, *, positive: bool = False) -> None:
    if type(value) not in (int, float):
        _fail(path, "expected a number (booleans are not numbers)")
    if (value <= 0 if positive else value < 0) or value > maximum:
        operator = "0 < value" if positive else "0 <= value"
        _fail(path, f"must satisfy {operator} <= {maximum}")


def validate_workflow(workflow: dict) -> dict:
    """Validate IR structure and dependency order; return the supplied dictionary.

    Tool existence and tool-specific arguments are checked by the runtime. Field
    presence within a previous tool's result can only be checked at execution.
    """
    _json(workflow, "workflow")
    _object(workflow, "workflow", {"ir_version", "id", "inputs", "steps", "outputs"}, set())
    if workflow["ir_version"] != IR_VERSION:
        _fail("workflow.ir_version", f"only {IR_VERSION!r} is supported")
    _identifier(workflow["id"], "workflow.id")

    inputs = workflow["inputs"]
    if type(inputs) is not dict:
        _fail("workflow.inputs", "expected an object")
    for name, definition in inputs.items():
        path = f"workflow.inputs.{name}"
        _identifier(name, path)
        _object(definition, path, {"type"}, {"default"})
        declared = definition["type"]
        if type(declared) is not str or declared not in _INPUT_TYPES:
            _fail(f"{path}.type", f"must be one of {', '.join(sorted(_INPUT_TYPES))}")
        if "default" in definition and not _matches_type(definition["default"], declared):
            _fail(f"{path}.default", f"expected {declared}")

    steps = workflow["steps"]
    if type(steps) is not list or not 1 <= len(steps) <= MAX_STEPS:
        _fail("workflow.steps", f"expected an array containing 1..{MAX_STEPS} steps")
    prior_steps: set[str] = set()
    input_names = set(inputs)
    for index, step in enumerate(steps):
        path = f"workflow.steps[{index}]"
        _object(step, path, {"id", "kind", "tool", "args"}, {"retry", "timeout_seconds", "when"})
        _identifier(step["id"], f"{path}.id")
        if step["id"] in prior_steps:
            _fail(f"{path}.id", f"duplicate step ID {step['id']!r}")
        if step["kind"] not in ("tool", "ai"):
            _fail(f"{path}.kind", "only 'tool' and 'ai' are supported")
        tool = step["tool"]
        if type(tool) is not str or not tool or len(tool) > 256 or tool != tool.strip():
            _fail(f"{path}.tool", "expected a nonblank tool/provider name of at most 256 characters")
        if any(ord(character) < 32 for character in tool):
            _fail(f"{path}.tool", "tool/provider name must not contain control characters")
        if type(step["args"]) is not dict:
            _fail(f"{path}.args", "expected an object")
        # Args are named arguments, not a reference replacing the entire map.
        if "$ref" in step["args"]:
            _fail(f"{path}.args", "args must map argument names to expressions")
        _expression(step["args"], input_names, prior_steps, f"{path}.args")
        if "when" in step:
            condition = step["when"]
            _object(condition, f"{path}.when", {"equals"}, set())
            operands = condition["equals"]
            if type(operands) is not list or len(operands) != 2:
                _fail(f"{path}.when.equals", "expected exactly two operands")
            _expression(operands, input_names, prior_steps, f"{path}.when.equals")
        if "retry" in step:
            retry = step["retry"]
            _object(retry, f"{path}.retry", {"max_attempts"}, {"delay_seconds"})
            attempts = retry["max_attempts"]
            if type(attempts) is not int or not 1 <= attempts <= MAX_ATTEMPTS:
                _fail(f"{path}.retry.max_attempts", f"expected an integer in 1..{MAX_ATTEMPTS}")
            if "delay_seconds" in retry:
                _bounded_number(retry["delay_seconds"], f"{path}.retry.delay_seconds", MAX_DELAY_SECONDS)
        if "timeout_seconds" in step:
            _bounded_number(step["timeout_seconds"], f"{path}.timeout_seconds", MAX_TIMEOUT_SECONDS, positive=True)
        prior_steps.add(step["id"])

    if type(workflow["outputs"]) is not dict or "$ref" in workflow["outputs"]:
        _fail("workflow.outputs", "expected an object mapping output names to expressions")
    _expression(workflow["outputs"], input_names, prior_steps, "workflow.outputs")
    return workflow


def validate_inputs(workflow: dict, inputs: dict) -> dict:
    """Apply literal defaults and validate a run's inputs, returning an isolated copy.

    An input is required exactly when its definition has no default. A Python
    bool does not satisfy number/integer, and integer requires an actual int.
    """
    validate_workflow(workflow)
    _json(inputs, "inputs")
    if type(inputs) is not dict:
        _fail("inputs", "expected an object")
    definitions = workflow["inputs"]
    unknown = inputs.keys() - definitions.keys()
    if unknown:
        _fail("inputs", f"undeclared inputs: {', '.join(sorted(unknown))}")
    resolved = {}
    for name, definition in definitions.items():
        if name in inputs:
            value = inputs[name]
        elif "default" in definition:
            value = definition["default"]
        else:
            _fail(f"inputs.{name}", "required input is missing")
        if not _matches_type(value, definition["type"]):
            _fail(f"inputs.{name}", f"expected {definition['type']}")
        resolved[name] = copy.deepcopy(value)
    return resolved
