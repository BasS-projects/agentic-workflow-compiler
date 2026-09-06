"""JSON CLI. Compilation never runs tasks or reads runtime secrets."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

from .dispatch import make_runtime, recorded_backend, registered_tools, runtime_for_record, validate_any
from .parser import StructuredFenceExtractor


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


def _provider(args, semantic=False):
    if semantic:
        from .compilation import SemanticCompileProvider as Provider
    else:
        from .providers import ChatCompletionsProvider as Provider
    if not args.endpoint or not args.model:
        raise ValueError("AI execution requires --endpoint and --model")
    api_key = None
    if args.api_key_env:
        api_key = os.environ.get(args.api_key_env)
        if not api_key:
            raise ValueError("The selected API key environment variable is missing or empty")
    return Provider(endpoint=args.endpoint, model=args.model,
                    api_key=api_key, timeout_seconds=args.request_timeout)


def _write_json(path, value):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(value, ensure_ascii=False, indent=2,
                                 allow_nan=False) + "\n", encoding="utf-8")


def _workflow(value):
    if isinstance(value, dict) and "workflow" in value and "provenance" in value:
        from .compilation import verify_bundle
        return verify_bundle(value)
    return validate_any(value)


def main(argv=None) -> int:
    effective = list(sys.argv[1:] if argv is None else argv)
    if effective and effective[0] == "serve":
        from .server import main as serve_main
        return serve_main(effective[1:])
    if effective and effective[0] == "worker":
        from .remote_worker import main as worker_main
        return worker_main(effective[1:])
    parser = argparse.ArgumentParser(prog="agentic-workflow")
    commands = parser.add_subparsers(dest="command", required=True)
    compile_cmd = commands.add_parser("compile", help="Extract and validate Skill.md into IR")
    compile_cmd.add_argument("skill")
    compile_cmd.add_argument("-o", "--output", required=True)
    compile_cmd.add_argument("--semantic", action="store_true",
                             help="Send Skill text to the configured AI endpoint for extraction")
    compile_cmd.add_argument("--bundle", action="store_true", help="Produce a reviewable compile bundle")
    compile_cmd.add_argument("--optimize", action="store_true", help="Apply conservative constant folding")
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
    run_cmd.add_argument("--plugins", help="Trusted local tool plugin configuration JSON")
    run_cmd.add_argument("--backend", choices=("python", "langgraph"), default="python")
    _ai_options(run_cmd)
    for command in ("inspect", "events", "recover", "approve", "cancel"):
        item = commands.add_parser(command)
        item.add_argument("run_id")
        item.add_argument("--db", default=".state/workflows.sqlite3")
        if command == "recover":
            item.add_argument("--retry-interrupted", action="store_true", required=True,
                              help="Acknowledge uncertain side effects before retrying")
        if command == "approve":
            item.add_argument("--step", required=True, help="Pending approval's exact step_path")
            item.add_argument("--actor", required=True)
            decision = item.add_mutually_exclusive_group(required=True)
            decision.add_argument("--approve", action="store_true")
            decision.add_argument("--reject", action="store_true")
    approve_cmd = commands.add_parser("approve-bundle", help="Bind a review acknowledgement to the exact compile bundle")
    approve_cmd.add_argument("bundle")
    approve_cmd.add_argument("--actor", required=True)
    approve_cmd.add_argument("-o", "--output")
    migrate_cmd = commands.add_parser("migrate", help="Migrate IR0.1 to IR0.2")
    migrate_cmd.add_argument("workflow")
    migrate_cmd.add_argument("-o", "--output", required=True)
    backend_cmd = commands.add_parser("backend-compile", help="Generate an executable backend artifact")
    backend_cmd.add_argument("workflow")
    backend_cmd.add_argument("--backend", choices=("langgraph",), default="langgraph")
    backend_cmd.add_argument("-o", "--output", required=True)
    eval_cmd = commands.add_parser("evaluate", help="Evaluate semantic extraction against cases using an actual endpoint")
    eval_cmd.add_argument("cases")
    eval_cmd.add_argument("-o", "--output", required=True)
    _ai_options(eval_cmd)
    commands.add_parser("serve", help="Start authenticated coordinator/API and web console")
    commands.add_parser("worker", help="Start an HTTP-connected worker with its own durable state")
    args = parser.parse_args(effective)
    runtime = None
    try:
        if args.command == "compile":
            source = Path(args.skill).read_text(encoding="utf-8")
            if args.semantic or args.bundle or args.optimize:
                from .compilation import compile_bundle
                extractor = _provider(args, semantic=True) if args.semantic else None
                bundle = compile_bundle(source, extractor=extractor, optimize=args.optimize)
                _write_json(args.output, bundle)
                _emit({"status": "compiled", "review_required": True, "path": args.output})
            else:
                workflow = validate_any(StructuredFenceExtractor().extract(source))
                _write_json(args.output, workflow)
                _emit({"status": "compiled", "workflow_id": workflow["id"], "path": args.output})
        elif args.command == "validate":
            workflow = _workflow(_read_json(args.workflow))
            _emit({"status": "valid", "workflow_id": workflow["id"]})
        elif args.command == "approve-bundle":
            from .compilation import approve_bundle
            approved = approve_bundle(_read_json(args.bundle), args.actor)
            output = args.output or args.bundle
            _write_json(output, approved)
            _emit({"status": "approved", "path": output})
        elif args.command == "migrate":
            from .advanced_ir import migrate_v1
            value = migrate_v1(_workflow(_read_json(args.workflow)))
            _write_json(args.output, value)
            _emit({"status": "migrated", "ir_version": "0.2", "path": args.output})
        elif args.command == "backend-compile":
            from .backends.langgraph import LangGraphBackend
            artifact = LangGraphBackend().compile(_workflow(_read_json(args.workflow)))
            target = Path(args.output).resolve()
            target.mkdir(parents=True, exist_ok=True)
            for name, content in artifact["files"].items():
                file = (target / name).resolve()
                if not file.is_relative_to(target) or file == target:
                    raise ValueError("backend artifact contains an invalid file path")
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text(content, encoding="utf-8")
            _write_json(target / "manifest.json", {k: v for k, v in artifact.items() if k != "files"})
            _emit({"status": "compiled", "backend": "langgraph", "path": str(target)})
        elif args.command == "evaluate":
            from .compilation import evaluate_cases
            cases = _read_json(args.cases)
            report = evaluate_cases(cases, _provider(args, semantic=True), mode="live")
            _write_json(args.output, report)
            _emit(report)
            return 0 if report["failed"] == 0 else 1
        else:
            if args.command == "run":
                if args.resume and not args.run_id:
                    raise ValueError("--resume requires --run-id")
                backend = recorded_backend(args.db, args.run_id) if args.resume else None
                if backend is not None and backend != args.backend:
                    raise ValueError(f"Resume requires the original --backend {backend}")
                ai_tools = {args.ai_tool: _provider(args)} if args.ai_tool else None
                workflow = _workflow(_read_json(args.workflow))
                tools = registered_tools(args.plugins)
                if args.backend == "langgraph":
                    from .backends.langgraph import LangGraphRuntime
                    runtime = LangGraphRuntime(args.db, args.workspace, tools=tools, ai_tools=ai_tools)
                else:
                    runtime = make_runtime(workflow, args.db, args.workspace, tools=tools, ai_tools=ai_tools)
                result = runtime.run(workflow, _read_json(args.inputs),
                                     run_id=args.run_id, resume=args.resume)
                _emit(result)
                return {"completed": 0, "waiting_approval": 3, "cancelled": 4}.get(result["status"], 1)
            runtime = runtime_for_record(args.db, args.run_id)
            if args.command == "inspect":
                _emit(runtime.inspect(args.run_id))
            elif args.command == "events":
                _emit(runtime.events(args.run_id))
            elif args.command == "recover":
                _emit(runtime.recover(args.run_id, policy="retry"))
            elif args.command == "approve":
                if not hasattr(runtime, "approve"):
                    raise ValueError("Approval nodes require IR0.2")
                _emit(runtime.approve(args.run_id, args.step, args.approve, args.actor))
            else:
                if not hasattr(runtime, "cancel"):
                    raise ValueError("Local cancellation command requires IR0.2; stop an IR0.1 process explicitly")
                _emit(runtime.cancel(args.run_id))
        return 0
    except (ValueError, OSError, KeyError, RuntimeError, sqlite3.Error) as exc:
        _emit({"status": "error", "error": str(exc)}, stream=sys.stderr)
        return 2
    finally:
        if runtime is not None:
            runtime.close()
