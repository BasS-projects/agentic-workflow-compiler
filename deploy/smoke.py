"""Exercise the running API and worker using freshly generated deployment tokens.

Use --docker to additionally inspect output bytes inside the Compose worker.
This script never builds an image or provisions a cloud service by itself.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import uuid


ROOT = Path(__file__).resolve().parents[1]


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    parser.add_argument("--tokens", type=Path, default=ROOT / ".state" / "deploy" / "tokens.json")
    parser.add_argument("--output", type=Path, default=ROOT / ".state" / "docker-smoke.json")
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("--docker", action="store_true", help="Verify the actual file in the Compose worker container")
    args = parser.parse_args()
    parsed = urlsplit(args.url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        parser.error("--url must be an HTTP(S) origin without credentials")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    checks = []
    run_id = "docker-smoke-" + uuid.uuid4().hex
    report = {"status": "running", "run_id": run_id, "checks": checks, "container_file_check": "not_run"}
    opener = build_opener(NoRedirect())

    def request(path, role=None, method="GET", body=None):
        headers = {}
        if role:
            headers["Authorization"] = "Bearer " + tokens[role]
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = Request(args.url.rstrip("/") + path, data=data, headers=headers, method=method)
        try:
            with opener.open(req, timeout=5) as response:
                raw = response.read()
                return response.status, json.loads(raw) if raw else {}
        except HTTPError as error:
            # Response error bodies and request headers are never logged.
            return error.code, {}

    def check(name, condition):
        checks.append({"name": name, "status": "passed" if condition else "failed"})
        if not condition:
            raise AssertionError(name)

    def wait_status(target):
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            code, current = request("/v1/runs/" + quote(run_id, safe=""), "viewer")
            if code != 200:
                raise RuntimeError("Run inspection failed")
            status = current.get("status")
            if status == target:
                return current
            if status in {"failed", "cancelled", "needs_recovery"}:
                raise RuntimeError("Run reached " + status + " while waiting for " + target)
            time.sleep(0.25)
        raise TimeoutError("Worker did not reach " + target)

    try:
        tokens = json.loads(args.tokens.read_text(encoding="utf-8"))
        deadline = time.monotonic() + args.timeout
        while True:
            try:
                code, _ = request("/healthz")
                if code == 200:
                    break
            except (URLError, TimeoutError, OSError):
                pass
            if time.monotonic() >= deadline:
                raise TimeoutError("API did not become healthy")
            time.sleep(0.5)
        check("api_health", True)
        code, _ = request("/v1/runs")
        check("anonymous_read_denied", code == 401)
        expected = "Vendor quote\nReady for review"
        workflow = {
            "ir_version": "0.2", "id": "container_document_approval", "inputs": {"text": {"type": "string"}},
            "steps": [
                {"id": "normalize", "kind": "tool", "tool": "text.normalize", "args": {"text": {"$ref": "inputs.text"}}},
                {"id": "review", "kind": "approval", "prompt": "Container SIT: approve the normalized document?"},
                {"id": "write", "kind": "tool", "tool": "files.write_text", "args": {"path": "output/proved.txt", "text": {"$ref": "steps.normalize.text"}}},
            ],
            "outputs": {"text": {"$ref": "steps.normalize.text"}, "path": {"$ref": "steps.write.path"}, "bytes": {"$ref": "steps.write.bytes"}},
        }
        submission = {"run_id": run_id, "workflow": workflow, "inputs": {"text": "\n  Vendor quote  \n  Ready for review  \n\n"}}
        code, _ = request("/v1/runs", "viewer", "POST", submission)
        check("viewer_submit_denied", code == 403)
        code, _ = request("/v1/runs", "operator", "POST", submission)
        check("operator_submit_accepted", code == 201)
        waiting = wait_status("waiting_approval")
        check("worker_reached_approval", waiting.get("worker_id") == "worker-1" and any(item.get("step_path") == "review" for item in waiting.get("approvals", [])))
        approval = {"step_path": "review", "approved": True}
        code, _ = request("/v1/runs/" + run_id + "/approve", "operator", "POST", approval)
        check("operator_cannot_approve", code == 403)
        code, _ = request("/v1/runs/" + run_id + "/approve", "approver", "POST", approval)
        check("approver_decision_accepted", code == 200)
        complete = wait_status("completed")
        outputs = (complete.get("result") or {}).get("outputs", {})
        check("worker_output_matches_business_result", outputs.get("text") == expected and outputs.get("bytes") == len(expected.encode("utf-8")))
        code, history = request("/v1/runs/" + run_id + "/events", "viewer")
        check("audit_history_available", code == 200 and len(history.get("events", [])) >= 4)
        if args.docker:
            workspace_key = hashlib.sha256(run_id.encode()).hexdigest()
            artifact = "/data/worker/runs/" + workspace_key + "/workspace/output/proved.txt"
            script = "import hashlib,pathlib,sys; p=pathlib.Path(sys.argv[1]); assert hashlib.sha256(p.read_bytes()).hexdigest()==sys.argv[2]"
            result = subprocess.run(["docker", "compose", "exec", "-T", "worker", "python", "-c", script, artifact, hashlib.sha256(expected.encode()).hexdigest()], cwd=ROOT, capture_output=True, text=True, timeout=20)
            check("actual_container_file_matches", result.returncode == 0)
            report["container_file_check"] = "passed"
        report["status"] = "passed"
    except Exception as error:
        report["status"] = "failed"
        # Only locally controlled assertion names are included, not server data.
        report["error"] = str(error) if isinstance(error, (AssertionError, TimeoutError, RuntimeError)) else type(error).__name__
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"Deployment smoke: {report['status']} ({len([c for c in checks if c['status'] == 'passed'])} checks passed)")
    print(f"Report: {args.output}")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
