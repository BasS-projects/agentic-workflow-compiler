"""Process-level HTTP tests: no worker shares the coordinator's SQLite file."""
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from agentic_workflow.remote_worker import CoordinatorClient


def flow():
    return {"ir_version": "0.1", "id": "http_process", "inputs": {}, "steps": [
        {"id": "write", "kind": "tool", "tool": "files.write_text", "args": {"path": "output.txt", "text": "worker result"}},
        {"id": "read", "kind": "tool", "tool": "files.read_text", "args": {"path": "output.txt"}},
    ], "outputs": {"text": {"$ref": "steps.read.text"}}}


class RemoteWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = os.environ.copy()
        self.env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src") + os.pathsep + str(self.root) + os.pathsep + self.env.get("PYTHONPATH", "")
        self.tokens = {role: role + "-token-1234567890" for role in ("operator", "approver", "worker1", "worker2")}
        config = {token: {"actor": role, "role": "worker" if role.startswith("worker") else role} for role, token in self.tokens.items()}
        auth = self.root / "auth.json"
        auth.write_text(json.dumps(config))
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"
        self.server = subprocess.Popen([sys.executable, "-m", "agentic_workflow.server", "--port", str(port), "--db", str(self.root / "central" / "queue.db"), "--auth-file", str(auth)], env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.addCleanup(self.stop_process, self.server)
        self.wait_for(lambda: self.health(), 10)
        self.client = CoordinatorClient(self.url, self.tokens["operator"])

    @staticmethod
    def stop_process(process):
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if process.stderr:
            process.stderr.close()
        if process.stdout:
            process.stdout.close()

    def health(self):
        try:
            with urlopen(self.url + "/healthz", timeout=0.3) as response:
                return response.status == 200
        except (URLError, TimeoutError):
            return False

    def get(self, run_id):
        request = Request(self.url + "/v1/runs/" + run_id, headers={"Authorization": "Bearer " + self.tokens["operator"]})
        with urlopen(request, timeout=3) as response:
            return json.load(response)

    @staticmethod
    def wait_for(predicate, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.02)
        raise AssertionError("Timed out waiting for process-level condition")

    def worker(self, name, once=True, plugins=None, lease=2):
        env = self.env.copy()
        env["TEST_WORKER_TOKEN"] = self.tokens[name]
        args = [sys.executable, "-m", "agentic_workflow.remote_worker", "--url", self.url, "--token-env", "TEST_WORKER_TOKEN", "--worker-id", name, "--workspace", str(self.root / name), "--lease-seconds", str(lease), "--poll-seconds", "0.05"]
        if once:
            args.append("--once")
        if plugins:
            args += ["--plugins", str(plugins)]
        process = subprocess.Popen(args, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(self.stop_process, process)
        return process

    def finish(self, process):
        stdout, stderr = process.communicate(timeout=15)
        self.assertEqual(process.returncode, 0, (stdout.decode(), stderr.decode()))
        return stdout.decode()

    def plugin(self, seconds):
        (self.root / "slow_plugin.py").write_text('''from pathlib import Path
import time

def slow(args, context):
    root = Path(context.workspace)
    (root / "entered").write_text("ready")
    time.sleep(args["seconds"])
    (root / "finished").write_text("done")
    return {"done": True}

def factory(config):
    return {"test.slow": slow}
''')
        config = self.root / "plugins.json"
        config.write_text(json.dumps({"plugins": [{"entrypoint": "slow_plugin:factory", "config": {}}]}))
        workflow = flow()
        workflow["steps"] = [{"id": "slow", "kind": "tool", "tool": "test.slow", "args": {"seconds": seconds}, "timeout_seconds": 10}]
        workflow["outputs"] = {"done": {"$ref": "steps.slow.done"}}
        return config, workflow

    def run_directory(self, worker, run_id):
        return self.root / worker / "runs" / hashlib.sha256(run_id.encode()).hexdigest()

    def test_two_independent_process_workers_execute_job_once(self):
        self.client.post("/v1/runs", {"workflow": flow(), "inputs": {}, "run_id": "exclusive"})
        workers = [self.worker("worker1"), self.worker("worker2")]
        outputs = [self.finish(worker) for worker in workers]
        result = self.get("exclusive")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["attempts"], 1)
        self.assertEqual(result["result"]["outputs"], {"text": "worker result"})
        copies = list(self.root.glob("worker*/runs/*/workspace/output.txt"))
        self.assertEqual(len(copies), 1)
        self.assertEqual(copies[0].read_text(), "worker result")
        self.assertTrue((self.root / "central" / "queue.db").exists())
        self.assertEqual(len(list(self.root.glob("worker*/runs/*/runtime.db"))), 1)
        self.assertNotIn(self.tokens["worker1"], "".join(outputs))

    def test_heartbeats_extend_lease_while_real_child_is_running(self):
        config, workflow = self.plugin(1.1)
        self.client.post("/v1/runs", {"workflow": workflow, "run_id": "heartbeat"})
        worker = self.worker("worker1", plugins=config, lease=0.45)
        entered = self.run_directory("worker1", "heartbeat") / "workspace" / "entered"
        self.wait_for(entered.exists)
        time.sleep(0.7)
        self.assertEqual(self.get("heartbeat")["status"], "running")
        self.finish(worker)
        self.assertEqual(self.get("heartbeat")["status"], "completed")

    def test_cancel_stops_separate_session_tool_before_late_effect(self):
        config, workflow = self.plugin(1.2)
        self.client.post("/v1/runs", {"workflow": workflow, "run_id": "cancel"})
        worker = self.worker("worker1", plugins=config, lease=0.45)
        workspace = self.run_directory("worker1", "cancel") / "workspace"
        self.wait_for(lambda: (workspace / "entered").exists())
        self.client.post("/v1/runs/cancel/cancel", {})
        self.finish(worker)
        time.sleep(1.3)
        self.assertEqual(self.get("cancel")["status"], "cancelled")
        self.assertFalse((workspace / "finished").exists())

    def test_approval_survives_worker_exit_and_same_worker_restart(self):
        workflow = flow()
        workflow["ir_version"] = "0.2"
        workflow["steps"].insert(1, {"id": "review", "kind": "approval", "prompt": "Release the written document?"})
        self.client.post("/v1/runs", {"workflow": workflow, "run_id": "approve"})
        self.finish(self.worker("worker1"))
        waiting = self.get("approve")
        self.assertEqual(waiting["status"], "waiting_approval")
        output = self.run_directory("worker1", "approve") / "workspace" / "output.txt"
        before = output.stat().st_mtime_ns
        CoordinatorClient(self.url, self.tokens["approver"]).post("/v1/runs/approve/approve", {"step_path": "review", "approved": True})
        self.finish(self.worker("worker2"))  # Cannot claim another worker's checkpoint.
        self.assertEqual(self.get("approve")["status"], "queued")
        self.finish(self.worker("worker1"))
        self.assertEqual(self.get("approve")["status"], "completed")
        self.assertEqual(output.stat().st_mtime_ns, before)

    def test_lost_approval_checkpoint_requires_new_run_and_fresh_review(self):
        workflow = flow()
        workflow["ir_version"] = "0.2"
        workflow["steps"][1:1] = [{"id": "first", "kind": "approval", "prompt": "First?"}, {"id": "second", "kind": "approval", "prompt": "Second?"}]
        self.client.post("/v1/runs", {"workflow": workflow, "run_id": "takeover"})
        self.finish(self.worker("worker1"))
        approver = CoordinatorClient(self.url, self.tokens["approver"])
        approver.post("/v1/runs/takeover/approve", {"step_path": "first", "approved": True})
        self.finish(self.worker("worker1"))
        self.assertEqual(self.get("takeover")["approvals"][0]["step_path"], "second")
        self.client.post("/v1/runs/takeover/recover", {"retry": True})
        self.finish(self.worker("worker2"))
        result = self.get("takeover")
        self.assertEqual(result["status"], "needs_recovery")
        self.assertIn("new run_id", result["error"])
        self.assertFalse((self.run_directory("worker2", "takeover") / "workspace" / "output.txt").exists())

    def test_missing_checkpoint_does_not_silently_replay_effects(self):
        workflow = flow()
        workflow["ir_version"] = "0.2"
        workflow["steps"].insert(1, {"id": "review", "kind": "approval", "prompt": "Release?"})
        self.client.post("/v1/runs", {"workflow": workflow, "run_id": "lostdisk"})
        self.finish(self.worker("worker1"))
        CoordinatorClient(self.url, self.tokens["approver"]).post("/v1/runs/lostdisk/approve", {"step_path": "review", "approved": True})
        directory = self.run_directory("worker1", "lostdisk")
        directory.rename(directory.with_name(directory.name + "-lost"))
        self.finish(self.worker("worker1"))
        self.assertEqual(self.get("lostdisk")["status"], "needs_recovery")
        self.assertFalse((directory / "workspace" / "output.txt").exists())

    def test_client_refuses_embedded_credentials_and_redirects(self):
        with self.assertRaises(ValueError):
            CoordinatorClient("http://user:password@localhost:8080", "token")


if __name__ == "__main__":
    unittest.main()
