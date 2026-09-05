"""JSON CLI. Compilation never runs tasks or reads runtime secrets."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .ir import validate_workflow
from .parser import compile_skill


def _read_json(path: str) -> dict:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def invalid(value):
        raise ValueError(f"non-finite JSON number: {value}")

    try:
        return json.loads(Path(path).read_text(encoding="utf-8"),
                          object_pairs_hook=pairs, parse_constant=invalid)
    except RecursionError as exc:
        raise ValueError("JSON nesting is too deep") from exc


def _emit(value, stream=None):
    print(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False),
          file=stream or sys.stdout)


def _ai_options(command):
    command.add_argument("--endpoint", help="Full OpenAI-compatible chat completions URL")
    command.add_argument("--model", help="Model ID served by the chosen endpoint")
    command.add_argument("--api-key-env", help="Environment variable containing the API key")
    command.add_argument("--request-timeout", type=float, default=30)


def _provider(args):
    from .providers import ChatCompletionsProvider
    if not args.endpoint or not args.model:
        raise ValueError("AI execution requires --endpoint and --model")
    api_key = None
    if args.api_key_env:
        api_key = os.environ.get(args.api_key_env)
        if not api_key:
            raise ValueError("The selected API key environment variable is missing or empty")
    return ChatCompletionsProvider(endpoint=args.endpoint, model=args.model,
                                   api_key=api_key, timeout_seconds=args.request_timeout)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="agentic-workflow")
    commands = parser.add_subparsers(dest="command", required=True)
    compile_cmd = commands.add_parser("compile", help="Extract and validate Skill.md into IR")
    compile_cmd.add_argument("skill")
    compile_cmd.add_argument("-o", "--output", required=True)
    compile_cmd.add_argument("--semantic", action="store_true",
                             help="Send Skill text to the configured AI endpoint for extraction")
    _ai_options(compile_cmd)
    validate_cmd = commands.add_parser("validate", help="Validate IR without executing it")
    validate_cmd.add_argument("workflow")
    run_cmd = commands.add_parser("run", help="Execute validated IR with persistent state")
    run_cmd.add_argument("workflow")
    run_cmd.add_argument("--inputs", required=True)
    run_cmd.add_argument("--workspace", required=True)
    run_cmd.add_argument("--db", default=".state/workflows.sqlite3")
    run_cmd.add_argument("--run-id")
    run_cmd.add_argument("--resume", action="store_true")
    run_cmd.add_argument("--ai-tool", help="Register the selected AI provider with this tool name")
    _ai_options(run_cmd)
    for command in ("inspect", "events", "recover"):
        item = commands.add_parser(command)
        item.add_argument("run_id")
        item.add_argument("--db", default=".state/workflows.sqlite3")
        if command == "recover":
            item.add_argument("--retry-interrupted", action="store_true", required=True,
                              help="Acknowledge uncertain side effects before retrying")
    args = parser.parse_args(argv)
    runtime = None
    try:
        if args.command == "compile":
            extractor = _provider(args) if args.semantic else None
            workflow = compile_skill(Path(args.skill).read_text(encoding="utf-8"), extractor=extractor)
            output = Path(args.output)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(workflow, ensure_ascii=False, indent=2,
                                         allow_nan=False) + "\n", encoding="utf-8")
            _emit({"status": "compiled", "workflow_id": workflow["id"], "path": str(output)})
        elif args.command == "validate":
            workflow = validate_workflow(_read_json(args.workflow))
            _emit({"status": "valid", "workflow_id": workflow["id"]})
        else:
            from .runtime import Runtime
            if args.command == "run":
                if args.resume and not args.run_id:
                    raise ValueError("--resume requires --run-id")
                ai_tools = {args.ai_tool: _provider(args)} if args.ai_tool else None
                runtime = Runtime(args.db, args.workspace, ai_tools=ai_tools)
                result = runtime.run(_read_json(args.workflow), _read_json(args.inputs),
                                     run_id=args.run_id, resume=args.resume)
                _emit(result)
                return 0 if result["status"] == "completed" else 1
            if not Path(args.db).is_file():
                raise ValueError(f"state database does not exist: {args.db}")
            runtime = Runtime(args.db, ".")
            if args.command == "inspect":
                _emit(runtime.inspect(args.run_id))
            elif args.command == "events":
                _emit(runtime.events(args.run_id))
            else:
                _emit(runtime.recover(args.run_id, policy="retry"))
        return 0
    except (ValueError, OSError, KeyError, RuntimeError) as exc:
        _emit({"status": "error", "error": str(exc)}, stream=sys.stderr)
        return 2
    finally:
        if runtime is not None:
            runtime.close()
