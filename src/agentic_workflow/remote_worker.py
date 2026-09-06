"""HTTP leased worker with private local checkpoints and supervised execution.

Coordinator credentials stay in the supervisor; workers never open central DBs.
Effects interrupted by cancellation or loss of a lease are not silently retried.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler

from .dispatch import make_runtime, registered_tools


class WorkerProtocolError(RuntimeError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class CheckpointRecoveryError(RuntimeError):
    """Safe operator-facing explanation of missing durable approval context."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class CoordinatorClient:
    def __init__(self, url, token, timeout=10):
        try:
            parts = urlsplit(url)
            parts.port
        except ValueError:
            raise ValueError("Coordinator requires a valid HTTP(S) URL") from None
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment or any(c.isspace() for c in url):
            raise ValueError("Coordinator requires HTTP(S) without URL credentials, query, or fragment")
        if not isinstance(token, str) or not token or any(c.isspace() for c in token):
            raise ValueError("Worker token must be nonempty without whitespace")
        self.url, self.token, self.timeout = url.rstrip("/"), token, timeout
        self.opener = build_opener(_NoRedirect())

    def post(self, path, body):
        data = json.dumps(body, allow_nan=False).encode()
        request = Request(self.url + path, data, {"Authorization": "Bearer " + self.token, "Content-Type": "application/json"}, method="POST")
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read(4 * 1024 * 1024 + 1)
                if len(raw) > 4 * 1024 * 1024:
                    raise WorkerProtocolError("Coordinator response is too large")
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise WorkerProtocolError("Coordinator response must be an object")
                return result
        except HTTPError as exc:
            # Do not print server body, URL, or Authorization data.
            raise WorkerProtocolError(f"Coordinator rejected request (HTTP {exc.code})", exc.code) from None
        except (URLError, TimeoutError, OSError, ValueError):
            raise WorkerProtocolError("Coordinator request failed") from None


def _atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".partial")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def _ai_tools(config_path):
    if not config_path:
        return None
    from .providers import ChatCompletionsProvider
    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError("AI config must map registered tool names to provider config")
    result = {}
    for name, provider in config.items():
        if not isinstance(provider, dict):
            raise ValueError("AI provider config must be an object")
        key_env = provider.get("api_key_env")
        key = os.environ.get(key_env) if key_env else None
        if key_env and not key:
            raise ValueError("AI provider credential environment variable is not set")
        result[name] = ChatCompletionsProvider(provider["endpoint"], provider["model"], api_key=key, timeout_seconds=provider.get("timeout_seconds", 30))
    return result


def _execute_job(request_path, result_path):
    request = json.loads(Path(request_path).read_text(encoding="utf-8"))
    job = request["job"]
    root = Path(request["directory"])
    runtime = None
    try:
        runtime = make_runtime(job["workflow"], root / "runtime.db", root / "workspace", tools=registered_tools(request.get("plugins")), ai_tools=_ai_tools(request.get("ai_config")))
        try:
            previous = runtime.inspect(job["run_id"])
        except KeyError:
            previous = None
        if previous is None and job.get("decisions"):
            raise CheckpointRecoveryError("Checkpoint missing with recorded approvals; submit a new run_id and request fresh approval")
        if previous is None and job.get("resume") and not job.get("recovery_requested"):
            raise CheckpointRecoveryError("Worker checkpoint is missing; operator recovery must acknowledge repeated effects")
        if previous and previous["status"] == "running":
            if not job.get("recovery_requested"):
                raise RuntimeError("Local execution was interrupted and requires explicit recovery")
            runtime.recover(job["run_id"], policy="retry")
        kwargs = {"run_id": job["run_id"], "resume": previous is not None}
        if job["workflow"]["ir_version"] == "0.2":
            decisions = job.get("decisions", {})
            known = {step["step_path"] for step in (previous or {}).get("steps", []) if step["kind"] == "approval"}
            if decisions.keys() - known:
                raise CheckpointRecoveryError("Approval checkpoints are missing; submit a new run_id and request fresh approval")
            kwargs["decisions"] = decisions
        result = runtime.run(job["workflow"], job["inputs"], **kwargs)
        _atomic_json(result_path, {"ok": True, "result": result})
        return 0
    except BaseException as exc:
        # Exception text can include credentials from a trusted third-party plugin.
        error = str(exc) if isinstance(exc, CheckpointRecoveryError) else f"Executor {type(exc).__name__}; inspect local checkpoints"
        _atomic_json(result_path, {"ok": False, "error": error})
        return 1
    finally:
        if runtime:
            runtime.close()


def _descendants(pid):
    """Map Linux /proc host PIDs back to this process's PID namespace.

    Some containers mount an ancestor namespace's /proc. ``ps`` then reports
    host PIDs even though ``kill`` expects local namespace PIDs. Restrict the
    graph to our own descendants before translating, so unrelated namespaces
    with identical local PID numbers can never be signalled.
    """
    if sys.platform.startswith("linux"):
        try:
            own_fields = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines() if ":" in line)
            own_host = int(own_fields["Pid"])
            depth = len(own_fields.get("NSpid", own_fields["Pid"]).split()) - 1
            records = {}
            for entry in Path("/proc").iterdir():
                if not entry.name.isdigit():
                    continue
                try:
                    fields = dict(line.split(":", 1) for line in (entry / "status").read_text().splitlines() if ":" in line)
                    ids = [int(value) for value in fields.get("NSpid", fields["Pid"]).split()]
                    records[int(fields["Pid"])] = (int(fields["PPid"]), ids)
                except (OSError, ValueError, KeyError):
                    continue
            ours = {own_host}
            while True:
                updated = ours | {host for host, (parent, _) in records.items() if parent in ours}
                if updated == ours:
                    break
                ours = updated
            target = next((host for host in ours if host in records and len(records[host][1]) > depth and records[host][1][depth] == pid), None)
            if target is None:
                return []
            found = {target}
            while True:
                updated = found | {host for host, (parent, _) in records.items() if parent in found}
                if updated == found:
                    break
                found = updated
            return [records[host][1][depth] for host in found - {target} if len(records[host][1]) > depth]
        except (OSError, ValueError, KeyError):
            return []
    try:
        result = subprocess.run(["ps", "-eo", "pid=,ppid="], check=True, capture_output=True, text=True, timeout=2)
        pairs = [tuple(map(int, line.split())) for line in result.stdout.splitlines() if len(line.split()) == 2]
    except (OSError, ValueError, subprocess.SubprocessError):
        return []
    found = {pid}
    while True:
        updated = found | {child for child, parent in pairs if parent in found}
        if updated == found:
            break
        found = updated
    return sorted(found - {pid}, reverse=True)


def _stop_executor(process):
    if process.poll() is not None:
        return
    descendants = _descendants(process.pid)
    # Freeze supervisor first so it cannot launch another child during cleanup.
    try:
        os.kill(process.pid, signal.SIGSTOP)
    except ProcessLookupError:
        pass
    descendants = list(set(descendants + _descendants(process.pid)))
    for child in descendants:
        try:
            os.kill(child, signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        os.killpg(process.pid, signal.SIGTERM)
        os.kill(process.pid, signal.SIGCONT)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=0.3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)
    for child in descendants:
        try:
            os.kill(child, signal.SIGKILL)
        except ProcessLookupError:
            pass


class RemoteWorker:
    def __init__(self, url, token, worker_id, workspace, lease_seconds=60, poll_seconds=1, plugins=None, ai_config=None, health_file=None, token_env=None):
        if not isinstance(worker_id, str) or not worker_id.strip() or len(worker_id) > 256:
            raise ValueError("worker_id must be a nonempty string up to 256 characters")
        if isinstance(lease_seconds, bool) or not math.isfinite(lease_seconds) or not 0 < lease_seconds <= 3600:
            raise ValueError("lease_seconds must be >0 and <=3600")
        if isinstance(poll_seconds, bool) or not math.isfinite(poll_seconds) or not 0 < poll_seconds <= 3600:
            raise ValueError("poll_seconds must be >0 and <=3600")
        self.client = CoordinatorClient(url, token, timeout=min(10, max(0.1, lease_seconds / 3)))
        self.worker_id = worker_id
        self.workspace = Path(workspace).expanduser().resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.lease_seconds, self.poll_seconds = lease_seconds, poll_seconds
        self.plugins = str(Path(plugins).resolve()) if plugins else None
        self.ai_config = str(Path(ai_config).resolve()) if ai_config else None
        self.health_file = health_file
        self.token_env = token_env
        self.stop_requested = False
        self.active_process = None

    def _health(self, status):
        if self.health_file:
            _atomic_json(self.health_file, {"worker_id": self.worker_id, "status": status, "updated_at": time.time(), "pid": os.getpid()})

    def stop(self, *_):
        self.stop_requested = True

    def run_once(self):
        claimed = self.client.post("/v1/jobs/claim", {"worker_id": self.worker_id, "lease_seconds": self.lease_seconds})
        self._health("idle")
        job = claimed.get("job")
        if job is None:
            return None
        if not isinstance(job, dict):
            raise WorkerProtocolError("Claimed job must be an object")
        key = hashlib.sha256(job["run_id"].encode()).hexdigest()
        directory = self.workspace / "runs" / key
        directory.mkdir(parents=True, exist_ok=True)
        request_path, result_path = directory / "request.json", directory / "result.json"
        result_path.unlink(missing_ok=True)
        _atomic_json(request_path, {"job": job, "directory": str(directory), "plugins": self.plugins, "ai_config": self.ai_config})
        env = os.environ.copy()
        if self.token_env:
            env.pop(self.token_env, None)
        args = [sys.executable, "-m", "agentic_workflow.remote_worker", "--execute-job", str(request_path), "--result", str(result_path)]
        # Tool stdout is isolated in a private local log, never echoed with API creds.
        log = (directory / "executor.log").open("ab")
        process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env, start_new_session=True)
        self.active_process = process
        body = {"worker_id": self.worker_id, "lease_token": job["lease_token"]}
        endpoint = "/v1/jobs/" + quote(job["run_id"], safe="")
        interval = min(5, self.lease_seconds / 3)
        next_heartbeat = time.monotonic() + interval
        self._health("executing")
        try:
            while process.poll() is None:
                if self.stop_requested:
                    _stop_executor(process)
                    self.client.post(endpoint + "/fail", {**body, "error": "Worker stopped during execution; reconcile effects before recovery"})
                    return {"run_id": job["run_id"], "status": "needs_recovery"}
                if time.monotonic() >= next_heartbeat:
                    self.client.post(endpoint + "/heartbeat", {**body, "lease_seconds": self.lease_seconds})
                    self._health("executing")
                    next_heartbeat = time.monotonic() + interval
                time.sleep(min(0.05, interval / 3))
            if not result_path.is_file():
                return self.client.post(endpoint + "/fail", {**body, "error": "Executor exited without a durable result"})
            payload = json.loads(result_path.read_text(encoding="utf-8"))
            if not payload.get("ok"):
                return self.client.post(endpoint + "/fail", {**body, "error": payload.get("error", "Executor failed")})
            # Refresh before completion; an expired lease cannot be resurrected.
            self.client.post(endpoint + "/heartbeat", {**body, "lease_seconds": self.lease_seconds})
            return self.client.post(endpoint + "/complete", {**body, "result": payload["result"]})
        except WorkerProtocolError as exc:
            _stop_executor(process)
            if exc.status == 409:
                return {"run_id": job["run_id"], "status": "lease_lost"}
            raise
        finally:
            _stop_executor(process)
            log.close()
            self.active_process = None
            self._health("idle")

    def run(self, once=False):
        while not self.stop_requested:
            try:
                result = self.run_once()
                if result:
                    print(json.dumps({"run_id": result["run_id"], "status": result["status"]}), flush=True)
                if once:
                    return 0
            except WorkerProtocolError as exc:
                self._health("disconnected")
                print(str(exc), file=sys.stderr, flush=True)
                if once or exc.status in {401, 403}:
                    return 1
            deadline = time.monotonic() + self.poll_seconds
            while not self.stop_requested and time.monotonic() < deadline:
                time.sleep(min(0.1, max(0, deadline - time.monotonic())))
        self._health("stopped")
        return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url")
    parser.add_argument("--token-env", default="AWC_WORKER_TOKEN")
    parser.add_argument("--worker-id")
    parser.add_argument("--workspace", default=".state/remote-worker")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--lease-seconds", type=float, default=60)
    parser.add_argument("--poll-seconds", type=float, default=1)
    parser.add_argument("--plugins")
    parser.add_argument("--ai-config")
    parser.add_argument("--health-file")
    parser.add_argument("--execute-job", help=argparse.SUPPRESS)
    parser.add_argument("--result", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.execute_job:
        if not args.result:
            parser.error("Internal executor result path required")
        return _execute_job(args.execute_job, args.result)
    if not args.url or not args.worker_id:
        parser.error("--url and --worker-id are required")
    try:
        worker = RemoteWorker(args.url, os.environ.get(args.token_env), args.worker_id, args.workspace, args.lease_seconds, args.poll_seconds, args.plugins, args.ai_config, args.health_file, args.token_env)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, worker.stop)
    return worker.run(args.once)


if __name__ == "__main__":
    raise SystemExit(main())
