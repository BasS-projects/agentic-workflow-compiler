"""Transactional coordinator invariants, including exclusive claims and fencing."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import sqlite3
import threading
import time
import unittest

from agentic_workflow.coordinator import Coordinator, ConflictError, LeaseLostError


def flow(version="0.1"):
    return {"ir_version": version, "id": "distributed", "inputs": {}, "steps": [{"id": "value", "kind": "tool", "tool": "core.value", "args": {"value": 42}}], "outputs": {"value": {"$ref": "steps.value.value"}}}


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "coordinator.db"
        self.co = Coordinator(self.db)

    def test_idempotent_submission_immutable_identity_across_restart(self):
        self.co.submit(flow(), {}, "same")
        restarted = Coordinator(self.db)
        self.assertEqual(restarted.submit(flow(), {}, "same")["status"], "queued")
        changed = flow()
        changed["steps"][0]["args"]["value"] = 99
        with self.assertRaises(ConflictError):
            restarted.submit(changed, {}, "same")
        with self.assertRaises(ConflictError):
            restarted.submit(flow(), {}, "same", replay_safe=True)
        self.assertEqual(restarted.metrics()["runs_total"], 1)

    def test_concurrent_claim_has_exactly_one_owner(self):
        self.co.submit(flow(), {}, "one")
        with ThreadPoolExecutor(max_workers=8) as pool:
            claims = list(pool.map(lambda n: Coordinator(self.db).claim(f"worker{n}"), range(8)))
        jobs = [job for job in claims if job]
        self.assertEqual(len(jobs), 1)
        job = jobs[0]
        with self.assertRaises(LeaseLostError):
            self.co.complete("one", job["lease_token"], "imposter", {"status": "completed"})
        self.co.complete("one", job["lease_token"], job["worker_id"], {"status": "completed", "outputs": {"value": 42}})
        self.assertEqual(self.co.get("one")["result"]["outputs"]["value"], 42)

    def test_expired_lease_requires_operator_reconciliation_and_fences_previous(self):
        self.co.submit(flow(), {}, "uncertain")
        old = self.co.claim("old", lease_seconds=0.02)
        time.sleep(0.03)
        self.assertIsNone(self.co.claim("new"))
        self.assertEqual(self.co.get("uncertain")["status"], "needs_recovery")
        with self.assertRaises(LeaseLostError):
            self.co.heartbeat("uncertain", old["lease_token"], "old")
        self.co.recover("uncertain", actor="operator")
        new = self.co.claim("new")
        self.assertGreater(new["lease_token"], old["lease_token"])
        self.assertTrue(new["recovery_requested"])
        with self.assertRaises(LeaseLostError):
            self.co.complete("uncertain", old["lease_token"], "old", {"status": "completed"})

    def test_declared_replay_safe_requeues_expired_lease(self):
        self.co.submit(flow(), {}, "safe", replay_safe=True)
        self.co.claim("one", lease_seconds=0.01)
        time.sleep(0.02)
        job = self.co.claim("two")
        self.assertEqual(job["run_id"], "safe")
        self.assertTrue(job["recovery_requested"])

    def test_approval_bound_to_worker_and_authenticated_actor_audit(self):
        self.co.submit(flow(), {}, "approval")
        job = self.co.claim("first")
        self.co.complete("approval", job["lease_token"], "first", {"status": "waiting_approval", "approvals": [{"step_path": "review", "prompt": "Release?"}]})
        with self.assertRaises(ValueError):
            self.co.approve("approval", "invented", True, "reviewer")
        self.co.approve("approval", "review", True, "reviewer")
        self.assertIsNone(self.co.claim("other"))
        resumed = self.co.claim("first")
        self.assertEqual(resumed["decisions"], {"review": {"approved": True, "actor": "reviewer"}})
        self.assertTrue(resumed["resume"])

    def test_approval_decision_is_immutable_while_other_approvals_wait(self):
        self.co.submit(flow(), {}, "approval")
        job = self.co.claim("worker")
        self.co.complete("approval", job["lease_token"], "worker", {"status": "waiting_approval", "approvals": [{"step_path": path, "prompt": "Release?"} for path in ("a", "b")]})
        self.co.approve("approval", "a", True, "reviewer")
        self.assertEqual(self.co.approve("approval", "a", True, "reviewer")["status"], "waiting_approval")
        with self.assertRaises(ConflictError):
            self.co.approve("approval", "a", False, "reviewer")
        with self.assertRaises(ConflictError):
            self.co.approve("approval", "a", True, "different-reviewer")
        self.assertEqual(self.co.get("approval")["decisions"]["a"], {"approved": True, "actor": "reviewer"})

    def test_heartbeat_cannot_renew_lease_that_expired_while_waiting_for_lock(self):
        self.co.submit(flow(), {}, "blocked")
        job = self.co.claim("worker", lease_seconds=0.15)
        blocker = sqlite3.connect(self.db)
        self.addCleanup(blocker.close)
        blocker.execute("BEGIN IMMEDIATE")
        started = threading.Event()
        results = []
        def heartbeat():
            started.set()
            try:
                self.co.heartbeat("blocked", job["lease_token"], "worker", 10)
                results.append("renewed")
            except LeaseLostError:
                results.append("expired")
        thread = threading.Thread(target=heartbeat)
        thread.start()
        started.wait(1)
        time.sleep(0.25)
        blocker.rollback()
        thread.join(2)
        self.assertEqual(results, ["expired"])
        self.assertEqual(self.co.get("blocked")["status"], "needs_recovery")

    def test_cancel_revokes_lease_and_rejects_result(self):
        self.co.submit(flow(), {}, "cancel")
        job = self.co.claim("one")
        self.co.cancel("cancel", "operator")
        with self.assertRaises(LeaseLostError):
            self.co.complete("cancel", job["lease_token"], "one", {"status": "completed"})
        self.assertEqual(self.co.get("cancel")["status"], "cancelled")

    def test_executor_crash_is_uncertain(self):
        self.co.submit(flow(), {}, "crash")
        job = self.co.claim("one")
        self.co.fail("crash", job["lease_token"], "one", "process killed")
        self.assertEqual(self.co.get("crash")["status"], "needs_recovery")
        self.assertIsNone(self.co.claim("two"))

    def test_schedule_concurrent_tick_is_transactionally_deduplicated(self):
        schedule = self.co.add_schedule(flow(), {}, 10, schedule_id="periodic")
        due = schedule["next_at"]
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _: Coordinator(self.db).tick(now=due), range(6)))
        self.assertEqual(sum(len(result) for result in results), 1)
        self.assertEqual(self.co.metrics()["runs_total"], 1)
        self.assertEqual(self.co.tick(now=due), [])
        # A long outage coalesces missed intervals, without unbounded catchup.
        self.assertEqual(len(self.co.tick(now=due + 10000)), 1)
        self.assertGreater(self.co.list_schedules()[0]["next_at"], due + 10000)


if __name__ == "__main__":
    unittest.main()
