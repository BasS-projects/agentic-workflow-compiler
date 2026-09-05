"""Version-aware boundaries; the original IR/runtime remain compatible."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from .ir import ValidationError, validate_inputs, validate_json, validate_workflow


def validate_any(workflow: dict) -> dict:
    validate_json(workflow)
    if not isinstance(workflow, dict):
        raise ValidationError("workflow must be a JSON object")
    version = workflow.get("ir_version")
    if version == "0.1":
        return validate_workflow(workflow)
    if version == "0.2":
        from .advanced_ir import validate_workflow_v2
        return validate_workflow_v2(workflow)
    raise ValidationError(f"unsupported ir_version: {version!r}")


def bind_inputs(workflow: dict, inputs: dict) -> dict:
    validate_any(workflow)
    if workflow["ir_version"] == "0.1":
        return validate_inputs(workflow, inputs)
    from .advanced_ir import validate_inputs_v2
    return validate_inputs_v2(workflow, inputs)


def make_runtime(workflow: dict, db_path, workspace, tools=None, ai_tools=None):
    validate_any(workflow)
    if workflow["ir_version"] == "0.1":
        from .runtime import Runtime
        return Runtime(db_path, workspace, tools=tools, ai_tools=ai_tools)
    from .advanced_runtime import AdvancedRuntime
    return AdvancedRuntime(db_path, workspace, tools=tools, ai_tools=ai_tools)


def recorded_backend(db_path, run_id):
    if not Path(db_path).is_file():
        return None
    with sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "langgraph_epochs" in tables and connection.execute(
                "SELECT 1 FROM langgraph_epochs WHERE run_id=?", (run_id,)).fetchone():
            return "langgraph"
        for table in ("a_runs", "runs"):
            if table in tables and connection.execute(
                    f"SELECT 1 FROM {table} WHERE run_id=?", (run_id,)).fetchone():
                return "python"
    return None


def runtime_for_record(db_path, run_id: str, workspace="."):
    if not Path(db_path).is_file():
        raise ValueError(f"state database does not exist: {db_path}")
    if recorded_backend(db_path, run_id) == "langgraph":
        from .backends.langgraph import LangGraphRuntime
        return LangGraphRuntime(db_path, workspace)
    from .advanced_runtime import AdvancedRuntime
    if AdvancedRuntime.contains_run(db_path, run_id):
        return AdvancedRuntime(db_path, workspace)
    from .runtime import Runtime
    return Runtime(db_path, workspace)


def registered_tools(config_path=None):
    from .worker import default_tools
    registry = default_tools()
    if config_path:
        import json
        from .plugins import load_tool_plugins
        config = json.loads(Path(config_path).read_text(encoding="utf-8"))
        additions = load_tool_plugins(config)
        overlap = registry.keys() & additions.keys()
        if overlap:
            raise ValueError("plugins cannot override built-in tools: " + ", ".join(sorted(overlap)))
        registry.update(additions)
    return registry
