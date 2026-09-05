"""Authenticated coordinator API and same-origin operational console."""
from __future__ import annotations

import argparse
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
from urllib.parse import unquote, urlsplit, parse_qs

from .coordinator import Coordinator, ConflictError
from .ir import validate_json

ROLES = {"viewer", "operator", "approver", "admin", "worker"}
READ = {"viewer", "operator", "approver", "admin"}
OPERATE = {"operator", "admin"}
APPROVE = {"approver", "admin"}
WORK = {"worker", "admin"}
BODY_LIMIT = 2 * 1024 * 1024


def _auth_config(config):
    if not isinstance(config, dict):
        raise ValueError("auth_config must map tokens to actor/role identities")
    checked = {}
    for token, identity in config.items():
        if not isinstance(token, str) or len(token) < 16 or any(c.isspace() for c in token):
            raise ValueError("API tokens require at least 16 non-whitespace characters")
        if not isinstance(identity, dict) or identity.get("role") not in ROLES:
            raise ValueError("Each token requires a valid role")
        if not isinstance(identity.get("actor"), str) or not identity["actor"].strip() or len(identity["actor"]) > 256:
            raise ValueError("Each token requires a nonempty actor")
        checked[token] = {"actor": identity["actor"], "role": identity["role"]}
    return checked


class APIError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message


class CoordinatorHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = True
    allow_reuse_address = True

    def server_close(self):
        if hasattr(self, "tick_stop"):
            self.tick_stop.set()
        super().server_close()
        if getattr(self, "tick_thread", None):
            self.tick_thread.join(timeout=2)


class Handler(BaseHTTPRequestHandler):
    server_version = "AgenticWorkflow"

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, format, *args):
        # Access URLs and request bodies may carry workflow data. No token logs.
        pass

    def _send(self, status, payload, content_type="application/json; charset=utf-8"):
        body = json.dumps(payload, allow_nan=False).encode() if content_type.startswith("application/json") else payload.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _identity(self, roles):
        authorization = self.headers.get("Authorization", "")
        if not authorization.startswith("Bearer "):
            raise APIError(401, "Bearer authentication required")
        supplied = authorization[7:]
        identity = None
        for token, candidate in self.server.auth_config.items():
            if hmac.compare_digest(supplied.encode(), token.encode()):
                identity = candidate
        if identity is None:
            raise APIError(401, "Invalid API token")
        if identity["role"] not in roles:
            raise APIError(403, "Role is not permitted for this operation")
        return identity

    def _body(self):
        if self.headers.get("Transfer-Encoding"):
            raise APIError(400, "Transfer-Encoding is not supported")
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except ValueError:
            raise APIError(400, "Invalid Content-Length") from None
        if length < 0 or length > BODY_LIMIT:
            raise APIError(413, "JSON request body exceeds 2 MiB")
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise APIError(400, "Incomplete request body")
        def unique_object(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("Duplicate JSON key")
                result[key] = value
            return result
        try:
            body = json.loads(raw or b"{}", object_pairs_hook=unique_object,
                              parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Invalid JSON number")))
            validate_json(body, "request")
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError, RecursionError):
            raise APIError(400, "Invalid JSON request body") from None
        if not isinstance(body, dict):
            raise APIError(400, "JSON body must be an object")
        return body

    def _worker(self, body, identity):
        worker_id = body.get("worker_id", identity["actor"])
        if worker_id != identity["actor"]:
            raise APIError(403, "worker_id must match authenticated actor")
        return worker_id

    def _route(self, method):
        parsed = urlsplit(self.path)
        parts = [unquote(p) for p in parsed.path.strip("/").split("/") if p]
        co = self.server.coordinator
        if method == "GET" and parts == ["healthz"]:
            return 200, {"status": "ok"}
        if method == "GET" and not parts:
            path = Path(__file__).parent / "web" / "index.html"
            html = path.read_text(encoding="utf-8") if path.exists() else "<!doctype html><title>Workflow coordinator</title><p>Workflow coordinator API is running.</p>"
            return 200, html, "text/html; charset=utf-8"
        if method == "GET" and parts == ["metrics"]:
            self._identity(READ)
            metrics = co.metrics()
            lines = ["# TYPE agentic_runs gauge"]
            for status, count in sorted(metrics["runs"].items()):
                lines.append(f'agentic_runs{{status="{status}"}} {count}')
            lines.extend(f"agentic_{key} {metrics[key]}" for key in ("runs_total", "schedules_total", "events_total"))
            return 200, "\n".join(lines) + "\n", "text/plain; version=0.0.4; charset=utf-8"
        if parts == ["v1", "runs"]:
            if method == "GET":
                self._identity(READ)
                limit = int(parse_qs(parsed.query).get("limit", [100])[0])
                return 200, {"runs": co.list_runs(limit)}
            identity = self._identity(OPERATE)
            body = self._body()
            return 201, co.submit(body["workflow"], body.get("inputs", {}), body.get("run_id"), actor=identity["actor"], replay_safe=body.get("replay_safe", False))
        if len(parts) >= 3 and parts[:2] == ["v1", "runs"]:
            run_id = parts[2]
            if method == "GET" and len(parts) == 3:
                self._identity(READ)
                return 200, co.get(run_id)
            if method == "GET" and len(parts) == 4 and parts[3] == "events":
                self._identity(READ)
                co.get(run_id)
                return 200, {"events": co.events(run_id)}
            if method == "POST" and len(parts) == 4:
                action = parts[3]
                if action not in {"cancel", "approve", "recover"}:
                    raise APIError(404, "Unknown route")
                identity = self._identity(APPROVE if action == "approve" else OPERATE)
                body = self._body()
                if action == "cancel":
                    result = co.cancel(run_id, identity["actor"])
                elif action == "approve":
                    result = co.approve(run_id, body["step_path"], body["approved"], identity["actor"])
                else:
                    result = co.recover(run_id, identity["actor"], body.get("retry", True))
                return 200, result
        if method == "POST" and parts == ["v1", "jobs", "claim"]:
            identity = self._identity(WORK)
            body = self._body()
            return 200, {"job": co.claim(self._worker(body, identity), body.get("lease_seconds", 60))}
        if method == "POST" and len(parts) == 4 and parts[:2] == ["v1", "jobs"]:
            if parts[3] not in {"heartbeat", "complete", "fail"}:
                raise APIError(404, "Unknown route")
            identity = self._identity(WORK)
            body = self._body()
            args = (parts[2], body["lease_token"], self._worker(body, identity))
            if parts[3] == "heartbeat":
                return 200, co.heartbeat(*args, lease_seconds=body.get("lease_seconds", 60))
            if parts[3] == "complete":
                return 200, co.complete(*args, result=body["result"])
            return 200, co.fail(*args, error=body["error"])
        if parts == ["v1", "schedules"]:
            if method == "GET":
                self._identity(READ)
                return 200, {"schedules": co.list_schedules()}
            identity = self._identity(OPERATE)
            body = self._body()
            return 201, co.add_schedule(body["workflow"], body.get("inputs", {}), body["interval_seconds"], identity["actor"], body.get("schedule_id"))
        if method == "POST" and parts == ["v1", "schedules", "tick"]:
            self._identity(OPERATE)
            self._body()
            return 200, {"runs": co.tick()}
        raise APIError(404, "Unknown route")

    def _handle(self, method):
        try:
            result = self._route(method)
            self._send(*result)
        except APIError as exc:
            self._send(exc.status, {"error": exc.message})
        except ConflictError as exc:
            self._send(409, {"error": str(exc)})
        except KeyError as exc:
            # Missing body fields are malformed requests; unknown runs are 404.
            key = str(exc.args[0])
            status = 400 if key in {"workflow", "interval_seconds", "step_path", "approved", "lease_token", "result", "error"} else 404
            self._send(status, {"error": f"Missing field or resource: {key}"})
        except (ValueError, TypeError) as exc:
            self._send(400, {"error": str(exc)})
        except TimeoutError:
            self._send(408, {"error": "Request timed out"})
        except Exception:
            self._send(500, {"error": "Internal coordinator error"})

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")


def create_server(host="127.0.0.1", port=8080, db_path=".state/coordinator.db", auth_config=None, tick_seconds=0):
    config = _auth_config(auth_config or {})
    if host not in {"127.0.0.1", "localhost", "::1"} and not config:
        raise ValueError("Non-loopback binding requires nonempty authentication configuration")
    if isinstance(tick_seconds, bool) or not isinstance(tick_seconds, (int, float)) or not 0 <= tick_seconds <= 86400:
        raise ValueError("tick_seconds must be between 0 and 86400")
    server = CoordinatorHTTPServer((host, port), Handler)
    server.coordinator = Coordinator(db_path)
    server.auth_config = config
    server.tick_stop = threading.Event()
    server.tick_thread = None
    if tick_seconds:
        def poller():
            while not server.tick_stop.wait(tick_seconds):
                try:
                    server.coordinator.tick()
                except Exception:
                    # Persisted due time is unchanged on a failed transaction.
                    pass
        server.tick_thread = threading.Thread(target=poller, daemon=True, name="schedule-ticker")
        server.tick_thread.start()
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--db", default=".state/coordinator.db")
    parser.add_argument("--auth-file", required=True)
    parser.add_argument("--tick-seconds", type=float, default=0)
    args = parser.parse_args(argv)
    try:
        auth = json.loads(Path(args.auth_file).read_text(encoding="utf-8"))
        server = create_server(args.host, args.port, args.db, auth, args.tick_seconds)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    print(json.dumps({"status": "listening", "host": server.server_address[0], "port": server.server_address[1]}), flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
