"""Browser policy checks and optional real Chromium network/UI integration."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest

from agentic_workflow.rpa import BrowserPolicyError, _write_artifact, browser_tools
from agentic_workflow.worker import TaskContext


class BrowserPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ctx = TaskContext(self.temp.name, "rpa", "step", "key")
        self.tool = browser_tools(["https://example.test"])["browser.run"]

    def test_initial_origin_and_all_actions_checked_before_browser_launch(self):
        for args in (
            {"url": "https://other.test", "actions": []},
            {"url": "file:///etc/passwd", "actions": []},
            {"url": "https://example.test", "actions": [{"action": "evaluate", "script": "1"}]},
            {"url": "https://example.test", "actions": [{"action": "screenshot", "path": "../escape.png"}]},
            {"url": "https://example.test", "actions": [], "timeout_ms": 999999},
        ):
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.tool(args, self.ctx)

    def test_allowlist_exact_origin_validation(self):
        for origins in ([], ["https://user:password@example.test"], ["https://example.test/path"], ["*.test"], "https://example.test"):
            with self.subTest(origins=origins), self.assertRaises(BrowserPolicyError):
                browser_tools(origins)
        self.assertEqual(browser_tools(["https://EXAMPLE.test:443/"])["browser.run"].allowed_origins, frozenset({"https://example.test:443"}))

    def test_artifacts_cannot_escape_via_symlink_or_overwrite_nonregular(self):
        outside = Path(self.temp.name).parent / (Path(self.temp.name).name + "-outside")
        outside.mkdir()
        self.addCleanup(outside.rmdir)
        (Path(self.temp.name) / "link").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            _write_artifact("link/escape.png", b"data", self.ctx)
        self.assertFalse((outside / "escape.png").exists())
        result = _write_artifact("reports/image.png", b"png-test", self.ctx)
        self.assertEqual(result, {"path": "reports/image.png", "bytes": 8})
        self.assertEqual((Path(self.temp.name) / result["path"]).read_bytes(), b"png-test")


class FixtureHandler(BaseHTTPRequestHandler):
    offsite_url = ""
    requests = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.requests.append(self.path)
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", self.offsite_url + "/should-never-arrive")
            self.end_headers()
            return
        if self.path == "/receipt.txt":
            self.send_response(200)
            self.send_header("Content-Disposition", 'attachment; filename="receipt.txt"')
            self.end_headers()
            self.wfile.write(b"Accepted ACME quote: 1250.00 THB")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(b'''<!doctype html><title>Vendor quote</title>
<input id="vendor"><input id="amount"><button id="submit" onclick="document.querySelector('#receipt').textContent='Accepted '+document.querySelector('#vendor').value+' quote: '+Number(document.querySelector('#amount').value).toFixed(2)+' THB'">Submit</button>
<p id="receipt"></p><a id="download" href="/receipt.txt">Download</a>''')


class RealBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise unittest.SkipTest("real browser gate: Playwright package not installed")
        with sync_playwright() as p:
            # Complete one driver RPC before teardown, even when no browser is
            # installed; this avoids abandoning driver initialization tasks.
            probe = p.request.new_context()
            probe.dispose()
            installed = Path(p.chromium.executable_path).exists()
        if not installed:
            raise unittest.SkipTest("real browser gate: Chromium executable not installed")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ctx = TaskContext(self.temp.name, "browser-integration", "step", "key")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = "http://127.0.0.1:" + str(self.server.server_port)
        self.tool = browser_tools([self.url])["browser.run"]

    def test_real_form_submission_screenshot_download(self):
        receipt = "Accepted ACME quote: 1250.00 THB"
        result = self.tool({"url": self.url, "actions": [
            {"action": "fill", "selector": "#vendor", "value": "ACME"},
            {"action": "fill", "selector": "#amount", "value": "1250"},
            {"action": "click", "selector": "#submit"},
            {"action": "assert_text", "selector": "#receipt", "value": receipt},
            {"action": "text", "selector": "#receipt", "name": "receipt"},
            {"action": "screenshot", "path": "proof/quote.png"},
            {"action": "download", "selector": "#download", "path": "proof/receipt.txt"},
        ]}, self.ctx)
        self.assertEqual(result["texts"], {"receipt": receipt})
        self.assertEqual((Path(self.temp.name) / "proof/receipt.txt").read_text(), receipt)
        self.assertTrue((Path(self.temp.name) / "proof/quote.png").read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))

    def test_redirect_cannot_contact_unallowed_server(self):
        class OtherHandler(FixtureHandler):
            requests = []
        other = ThreadingHTTPServer(("127.0.0.1", 0), OtherHandler)
        threading.Thread(target=other.serve_forever, daemon=True).start()
        self.addCleanup(other.server_close)
        self.addCleanup(other.shutdown)
        FixtureHandler.offsite_url = "http://127.0.0.1:" + str(other.server_port)
        with self.assertRaises(BrowserPolicyError):
            self.tool({"url": self.url + "/redirect", "actions": []}, self.ctx)
        self.assertEqual(OtherHandler.requests, [], "unapproved redirect target must never receive a request")


if __name__ == "__main__":
    unittest.main()
