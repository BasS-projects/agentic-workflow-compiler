"""Real Chromium console integration test against disposable API/worker processes.

Requires the rpa extra and an installed Playwright Chromium browser. A missing
browser fails this gate; it is never reported as a successful simulation.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import runpy
import socket
import subprocess
import sys
import tempfile
import time
from urllib.error import URLError
from urllib.request import urlopen
import uuid


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / ".state" / "ui-report.json")
    args = parser.parse_args()
    args.output = args.output.resolve()
    artifacts = args.output.parent / "ui-proof"
    artifacts.mkdir(parents=True, exist_ok=True)
    checks = []
    report = {"status": "running", "checks": checks, "browser": "chromium", "artifacts": []}
    processes, logs, tokens = [], [], {}

    def check(name, condition):
        checks.append({"name": name, "status": "passed" if condition else "failed"})
        if not condition:
            raise AssertionError(name)

    try:
        from playwright.sync_api import expect, sync_playwright

        with tempfile.TemporaryDirectory(prefix="awc-console-") as temporary:
            state = Path(temporary)
            tokens = runpy.run_path(str(ROOT / "deploy" / "init.py"))["initialize"](state / "config")
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            url = f"http://127.0.0.1:{port}"
            server_log = (artifacts / "server.log").open("w", encoding="utf-8")
            logs.append(server_log)
            server = subprocess.Popen([sys.executable, "-m", "agentic_workflow.server", "--host", "127.0.0.1", "--port", str(port), "--db", str(state / "coordinator.db"), "--auth-file", str(state / "config" / "auth.json")], cwd=ROOT, stdout=server_log, stderr=server_log)
            processes.append(server)
            deadline = time.monotonic() + 30
            while True:
                try:
                    with urlopen(url + "/healthz", timeout=1) as response:
                        if response.status == 200:
                            break
                except (OSError, URLError):
                    pass
                if server.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError("Disposable API did not become healthy; inspect ui-proof/server.log")
                time.sleep(0.1)
            worker_log = (artifacts / "worker.log").open("w", encoding="utf-8")
            logs.append(worker_log)
            worker = subprocess.Popen([sys.executable, "-m", "agentic_workflow.remote_worker", "--url", url, "--token-env", "AWC_WORKER_TOKEN", "--worker-id", "worker-1", "--workspace", str(state / "worker"), "--plugins", str(state / "config" / "plugins.json"), "--poll-seconds", "0.1"], cwd=ROOT, env=dict(os.environ, AWC_WORKER_TOKEN=tokens["worker"]), stdout=worker_log, stderr=worker_log)
            processes.append(worker)

            try:
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    page = browser.new_page(viewport={"width": 1440, "height": 1050})
                    page.set_default_timeout(20000)
                    script_errors = []
                    page.on("pageerror", lambda error: script_errors.append(str(error)))
                    page.goto(url)
                    expect(page.get_by_role("heading", name="Workflow operations")).to_be_visible()
                    page.locator("#token").fill(tokens["operator"])
                    page.get_by_role("button", name="Connect", exact=True).click()
                    expect(page.locator("#connection-label")).to_have_text("Connected")
                    check("token_removed_from_form", page.locator("#token").input_value() == "")
                    check("token_absent_from_browser_storage", page.evaluate("Object.keys(localStorage).length + Object.keys(sessionStorage).length") == 0)

                    run_id = "ui-proof-" + uuid.uuid4().hex
                    hostile = '<img src=x onerror="window.injected=true">'
                    page.get_by_role("tab", name="Submit workflow", exact=True).click()
                    page.locator("#run-id").fill(run_id)
                    page.locator("#inputs-json").fill(json.dumps({"text": hostile + "\n  Approved input  "}))
                    page.get_by_role("button", name="Submit run", exact=True).click()
                    expect(page.get_by_role("button", name="Refresh details", exact=True)).to_be_visible()

                    def refresh_until(status):
                        deadline = time.monotonic() + 30
                        while time.monotonic() < deadline:
                            page.get_by_role("button", name="Refresh details", exact=True).click()
                            if page.locator("#run-detail .badge." + status).count():
                                return
                            page.wait_for_timeout(200)
                        raise TimeoutError("Console run did not reach " + status)

                    refresh_until("waiting_approval")
                    expect(page.get_by_text("Approval required", exact=True)).to_be_visible()
                    check("ui_submission_reached_real_worker_approval", True)
                    desktop_path = artifacts / "console-desktop.png"
                    page.screenshot(path=str(desktop_path), full_page=True)
                    report["artifacts"].append(str(desktop_path))

                    page.get_by_role("button", name="Disconnect", exact=True).click()
                    check("disconnect_clears_protected_content", page.locator("#run-rows tr").count() == 0 and page.locator("#run-detail .badge").count() == 0)
                    page.locator("#token").fill(tokens["approver"])
                    page.get_by_role("button", name="Connect", exact=True).click()
                    expect(page.locator("#connection-label")).to_have_text("Connected")
                    page.get_by_role("button", name=run_id, exact=True).click()
                    page.get_by_role("button", name="Approve", exact=True).click()
                    refresh_until("completed")
                    check("ui_approval_resumes_and_completes_worker", True)
                    visible_result = page.locator("#run-detail details").first.locator("pre").inner_text()
                    result = json.loads(visible_result)
                    check("ui_result_matches_expected_document", result.get("outputs", {}).get("text") == hostile + "\nApproved input")
                    check("hostile_output_rendered_as_text", page.evaluate("window.injected === undefined") and page.locator("#run-detail img").count() == 0)
                    page.get_by_role("button", name="Load event history", exact=True).click()
                    expect(page.locator("#run-detail summary").filter(has_text="Event history")).to_be_visible()
                    check("ui_can_load_audit_events", True)

                    page.set_viewport_size({"width": 390, "height": 844})
                    mobile_path = artifacts / "console-mobile.png"
                    page.screenshot(path=str(mobile_path), full_page=True)
                    report["artifacts"].append(str(mobile_path))
                    check("mobile_layout_has_no_page_overflow", page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"))
                    page.reload()
                    expect(page.locator("#connection-label")).to_have_text("Disconnected")
                    check("reload_clears_authentication_and_data", page.locator("#token").input_value() == "" and page.locator("#run-rows tr").count() == 0)
                    check("no_browser_script_errors", not script_errors)
                    browser.close()
                report["status"] = "passed"
            finally:
                # Stop executors before TemporaryDirectory removes their databases.
                for process in reversed(processes):
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
    except Exception as error:
        report["status"] = "failed"
        message = str(error)
        for token in tokens.values():
            message = message.replace(token, "[REDACTED]")
        report["error"] = type(error).__name__ + ": " + message[:2000]
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        for log in logs:
            log.close()
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"Console browser integration: {report['status']} ({sum(c['status'] == 'passed' for c in checks)} checks passed)")
    print(f"Report: {args.output}")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
