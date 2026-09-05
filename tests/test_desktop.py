"""Real X11 input tests run only with an explicitly configured disposable display."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from agentic_workflow.desktop import DesktopUnavailableError, desktop_status, desktop_tools
from agentic_workflow.worker import TaskContext


class DesktopPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ctx = TaskContext(self.temp.name, "desktop", "step", "key")
        self.tool = desktop_tools()["desktop.run"]

    def test_missing_display_fails_explicitly(self):
        with patch.dict(os.environ, {"DISPLAY": ""}):
            self.assertFalse(desktop_status()["available"])
            with self.assertRaises(DesktopUnavailableError):
                self.tool({"actions": [{"action": "screenshot", "path": "proof.png"}]}, self.ctx)
        self.assertFalse((Path(self.temp.name) / "proof.png").exists())

    def test_invalid_action_list_rejected_before_any_input(self):
        for action in (
            {"action": "shell", "command": "touch bad"},
            {"action": "key", "keys": "Return; pwd"},
            {"action": "type", "text": "line\ncommand"},
            {"action": "click", "x": -1, "y": 0},
            {"action": "screenshot", "path": "../escape.png"},
            {"action": "assert_pixel", "x": 1, "y": 1, "rgb": [999, 0, 0]},
        ):
            with self.subTest(action=action), self.assertRaises(ValueError):
                self.tool({"actions": [action]}, self.ctx)


@unittest.skipUnless(os.environ.get("AGENTIC_DESKTOP_SIT") == "1", "real desktop gate: set AGENTIC_DESKTOP_SIT=1 on disposable DISPLAY")
class RealDesktopTests(unittest.TestCase):
    def test_real_window_receives_input_and_screenshot_matches_success(self):
        status = desktop_status()
        if not status["available"]:
            self.skipTest(status["reason"])
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "receipt.txt"
            fixture = Path(__file__).resolve().parents[1] / "examples/desktop_fixture.py"
            process = subprocess.Popen([sys.executable, str(fixture), str(target)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    found = subprocess.run(["xdotool", "search", "--onlyvisible", "--name", "^Workflow Desktop SIT$"], capture_output=True)
                    if found.returncode == 0:
                        break
                    if process.poll() is not None:
                        self.fail("desktop fixture did not start: " + process.stderr.read().decode())
                    time.sleep(0.05)
                result = desktop_tools()["desktop.run"]({"actions": [
                    {"action": "focus", "title": "Workflow Desktop SIT"},
                    {"action": "type", "text": "ACME 1250"},
                    {"action": "key", "keys": "Return"},
                    {"action": "assert_title", "value": "Saved: ACME 1250"},
                    {"action": "assert_pixel", "x": 460, "y": 220, "rgb": [25, 135, 84]},
                    {"action": "screenshot", "path": "proof/desktop.png"},
                ]}, TaskContext(directory, "desktop-sit", "step", "key"))
                self.assertEqual(target.read_text(), "ACME 1250")
                self.assertEqual(result["assertions"], 2)
                self.assertTrue((Path(directory) / "proof/desktop.png").read_bytes().startswith(b"\x89PNG"))
            finally:
                process.terminate()
                process.wait(timeout=5)
                if process.stderr:
                    process.stderr.close()


if __name__ == "__main__":
    unittest.main()
