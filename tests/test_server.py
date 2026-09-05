"""Real HTTP authorization, role separation and bounded protocol tests."""
import json
import http.client
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from agentic_workflow.server import create_server


def flow():
    return {"ir_version": "0.1", "id": "api", "inputs": {}, "steps": [{"id": "one", "kind": "tool", "tool": "core.value", "args": {"value": 1}}], "outputs": {}}


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.tokens = {role: role + "-token-1234567890" for role in ("viewer", "operator", "approver", "admin", "worker")}
        config = {token: {"actor": role + "-actor", "role": role} for role, token in self.tokens.items()}
        self.server = create_server("127.0.0.1", 0, Path(self.temp.name) / "central.db", config)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.cleanup_server)
        self.url = "http://127.0.0.1:" + str(self.server.server_port)

    def cleanup_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def request(self, path, role=None, body=None):
        headers = {"Content-Type": "application/json"}
        if role:
            headers["Authorization"] = "Bearer " + self.tokens[role]
        data = None if body is None else json.dumps(body).encode()
        request = Request(self.url + path, data=data, headers=headers)
        try:
            with urlopen(request, timeout=3) as response:
                content = response.read()
                return response.status, json.loads(content) if "application/json" in response.headers["Content-Type"] else content.decode()
        except HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_public_health_and_protected_read(self):
        self.assertEqual(self.request("/healthz")[0], 200)
        self.assertEqual(self.request("/v1/runs")[0], 401)
        self.assertEqual(self.request("/v1/runs", "viewer"), (200, {"runs": []}))
        self.assertEqual(self.request("/metrics", "viewer")[0], 200)
        self.assertEqual(self.request("/metrics")[0], 401)

    def test_roles_cannot_escalate_with_body_and_worker_cannot_impersonate(self):
        body = {"workflow": flow(), "inputs": {}, "run_id": "job", "actor": "admin", "role": "admin"}
        self.assertEqual(self.request("/v1/runs", "viewer", body)[0], 403)
        status, result = self.request("/v1/runs", "operator", body)
        self.assertEqual(status, 201)
        self.assertEqual(result["actor"], "operator-actor")
        self.assertEqual(self.request("/v1/jobs/claim", "operator", {})[0], 403)
        self.assertEqual(self.request("/v1/jobs/claim", "worker", {"worker_id": "someone"})[0], 403)
        status, claim = self.request("/v1/jobs/claim", "worker", {})
        self.assertEqual(status, 200)
        job = claim["job"]
        self.assertEqual(job["worker_id"], "worker-actor")
        self.assertEqual(self.request("/v1/runs/job/approve", "operator", {"step_path": "review", "approved": True})[0], 403)
        self.assertEqual(self.request("/v1/runs/job/cancel", "approver", {})[0], 403)
        self.assertEqual(self.request("/v1/runs/job/cancel", "operator", {})[0], 200)
        self.assertEqual(self.request("/v1/jobs/job/complete", "worker", {"lease_token": job["lease_token"], "result": {"status": "completed"}})[0], 409)

    def test_approval_actor_is_token_identity(self):
        self.request("/v1/runs", "operator", {"workflow": flow(), "run_id": "job"})
        job = self.request("/v1/jobs/claim", "worker", {})[1]["job"]
        self.request("/v1/jobs/job/complete", "worker", {"lease_token": job["lease_token"], "result": {"status": "waiting_approval", "approvals": [{"step_path": "review", "prompt": "Approve?"}]}})
        status, result = self.request("/v1/runs/job/approve", "approver", {"step_path": "review", "approved": True, "actor": "fake-admin"})
        self.assertEqual(status, 200)
        self.assertEqual(result["decisions"]["review"]["actor"], "approver-actor")

    def test_conflicting_identity_validation_and_body_limit(self):
        self.request("/v1/runs", "operator", {"workflow": flow(), "run_id": "same"})
        changed = flow()
        changed["id"] = "different"
        self.assertEqual(self.request("/v1/runs", "operator", {"workflow": changed, "run_id": "same"})[0], 409)
        self.assertEqual(self.request("/v1/runs", "operator", {"inputs": {}})[0], 400)
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        connection.putrequest("POST", "/v1/runs")
        connection.putheader("Authorization", "Bearer " + self.tokens["operator"])
        connection.putheader("Content-Length", str(2 * 1024 * 1024 + 1))
        connection.endheaders()
        # Server rejects declared oversized bodies before reading payload bytes.
        self.assertEqual(connection.getresponse().status, 413)
        connection.close()
        self.assertEqual(self.request("/v1/runs/missing", "viewer")[0], 404)

    def test_duplicate_keys_and_excessive_nesting_are_bad_requests(self):
        for raw in (b'{"workflow":{},"workflow":{}}', b'{"nested":' + b'[' * 1100 + b'0' + b']' * 1100 + b'}'):
            request = Request(self.url + "/v1/runs", data=raw, headers={"Authorization": "Bearer " + self.tokens["operator"], "Content-Type": "application/json"})
            with self.assertRaises(HTTPError) as raised:
                urlopen(request, timeout=3)
            self.assertEqual(raised.exception.code, 400)

    def test_compiled_bundle_requires_review_before_submission(self):
        from agentic_workflow.compilation import compile_bundle, approve_bundle
        source = "```workflow-ir\n" + json.dumps(flow()) + "\n```"
        bundle = compile_bundle(source)
        self.assertEqual(self.request("/v1/runs", "operator", {"workflow": bundle})[0], 400)
        approved = approve_bundle(bundle, "human")
        status, run = self.request("/v1/runs", "operator", {"workflow": approved})
        self.assertEqual(status, 201)
        self.assertEqual(run["workflow"], flow())
        approved["workflow"]["id"] = "tampered"
        self.assertEqual(self.request("/v1/runs", "operator", {"workflow": approved})[0], 400)

    def test_public_bind_requires_auth(self):
        with self.assertRaises(ValueError):
            create_server("0.0.0.0", 0, Path(self.temp.name) / "other.db", {})


if __name__ == "__main__":
    unittest.main()
