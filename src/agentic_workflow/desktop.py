"""Optional real X11 desktop actions on an explicitly configured display.

Use a disposable Xvfb display dedicated to a worker. This operates real windows;
it is not an accessibility tree adapter, OCR engine, or OS privilege sandbox.
"""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import io
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
from typing import Any

from .rpa import _artifact_path, _write_artifact
from .worker import TaskContext

__version__ = "0.2.0"


class DesktopUnavailableError(RuntimeError):
    """The explicitly configured real X11 desktop cannot be used."""


def _command(*args: str) -> str:
    try:
        result = subprocess.run(["xdotool", *args], capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DesktopUnavailableError("xdotool could not operate the configured X11 display") from exc
    if result.returncode:
        raise RuntimeError(f"X11 action {args[0]} failed (exit {result.returncode})")
    return result.stdout.strip()


def _check_dependencies() -> str:
    display = os.environ.get("DISPLAY", "")
    if not re.fullmatch(r":\d+(?:\.\d+)?", display):
        raise DesktopUnavailableError("desktop.run requires an explicit local X11 DISPLAY, such as :99")
    if not shutil.which("xdotool"):
        raise DesktopUnavailableError("desktop.run requires xdotool; install it on the desktop worker")
    try:
        from PIL import ImageGrab, features
        if not features.check_feature("xcb"):
            raise DesktopUnavailableError("Pillow requires XCB support for X11 screenshots")
    except ImportError as exc:
        raise DesktopUnavailableError("desktop.run requires Pillow with XCB support") from exc
    try:
        _command("getdisplaygeometry")
    except RuntimeError as exc:
        raise DesktopUnavailableError("cannot connect to the configured local X11 display") from exc
    return display


def desktop_status() -> dict[str, Any]:
    """Probe real dependencies without performing desktop input actions."""
    try:
        display = _check_dependencies()
    except DesktopUnavailableError as exc:
        return {"available": False, "reason": str(exc)}
    return {"available": True, "display": display}


@contextmanager
def _display_lock(display: str):
    # A shared per-UID display lock prevents two workflow steps interleaving
    # clicks. It does not prevent a human or unrelated application using X11.
    name = "agentic-desktop-" + str(os.getuid()) + "-" + display.replace(":", "").replace(".", "-") + ".lock"
    descriptor = os.open(Path(tempfile.gettempdir()) / name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("configured desktop is busy with another workflow action") from exc
        yield
    finally:
        os.close(descriptor)


def _integer(value: Any, label: str, maximum: int = 32767) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError(f"{label} must be an integer between 0 and {maximum}")
    return value


def _desktop_run(args: dict[str, Any], context: TaskContext) -> dict[str, Any]:
    if not isinstance(args, dict) or set(args) != {"actions"}:
        raise ValueError("desktop.run requires only an actions list")
    actions = args["actions"]
    if not isinstance(actions, list) or not 1 <= len(actions) <= 100:
        raise ValueError("desktop actions must contain 1 to 100 actions")
    fields = {
        "focus": {"action", "title"}, "type": {"action", "text"},
        "key": {"action", "keys"}, "click": {"action", "x", "y"},
        "assert_title": {"action", "value"}, "assert_pixel": {"action", "x", "y", "rgb"},
        "screenshot": {"action", "path"},
    }
    # Validate the entire action list before making any desktop changes.
    for action in actions:
        if not isinstance(action, dict) or action.get("action") not in fields:
            raise ValueError("unsupported desktop action")
        expected = fields[action["action"]]
        if set(action) != expected and not (action["action"] == "assert_pixel" and set(action) == expected | {"tolerance"}):
            raise ValueError("desktop action has missing or unexpected fields")
        for name in ("title", "text", "keys", "value"):
            if name in action and (not isinstance(action[name], str) or not action[name] or len(action[name]) > 4096):
                raise ValueError(f"{name} must be a nonempty string of at most 4096 characters")
        if action["action"] == "type" and any(ord(char) < 32 or ord(char) > 126 for char in action["text"]):
            raise ValueError("desktop type supports printable ASCII; use explicit key actions for Return and Tab")
        if action["action"] == "key" and not re.fullmatch(r"[A-Za-z0-9_]+(?:\+[A-Za-z0-9_]+)*", action["keys"]):
            raise ValueError("keys must name one X11 key chord, such as ctrl+a or Return")
        for name in ("x", "y"):
            if name in action:
                _integer(action[name], name)
        if action["action"] == "assert_pixel":
            if not isinstance(action["rgb"], list) or len(action["rgb"]) != 3:
                raise ValueError("rgb must be a three-channel integer list")
            for channel in action["rgb"]:
                _integer(channel, "rgb channel", 255)
            _integer(action.get("tolerance", 0), "tolerance", 255)
        if "path" in action:
            _artifact_path(action["path"], context)
    display = _check_dependencies()
    from PIL import ImageGrab
    screenshots: list[str] = []
    assertions = 0
    focused: str | None = None
    with _display_lock(display):
        for action in actions:
            kind = action["action"]
            if kind == "focus":
                windows = _command("search", "--onlyvisible", "--name", "^" + re.escape(action["title"]) + "$").splitlines()
                if len(windows) != 1:
                    raise RuntimeError("focus requires exactly one visible window with the exact title")
                focused = windows[0]
                _command("windowfocus", "--sync", focused)
            elif kind in {"type", "key", "click"}:
                if focused is None or _command("getwindowfocus") != focused:
                    raise RuntimeError("desktop input requires an explicit focused window that retains focus")
                if kind == "type":
                    _command("type", "--clearmodifiers", "--delay", "1", "--", action["text"])
                elif kind == "key":
                    _command("key", "--clearmodifiers", action["keys"])
                else:
                    width, height = map(int, _command("getdisplaygeometry").split())
                    if action["x"] >= width or action["y"] >= height:
                        raise ValueError("click coordinates are outside the configured display")
                    _command("mousemove", "--sync", str(action["x"]), str(action["y"]))
                    _command("click", "1")
            elif kind == "assert_title":
                deadline = time.monotonic() + 3
                while _command("getwindowfocus", "getwindowname") != action["value"]:
                    if time.monotonic() >= deadline:
                        raise AssertionError("focused window title did not match expected value")
                    time.sleep(0.025)
                assertions += 1
            elif kind in {"screenshot", "assert_pixel"}:
                screen = ImageGrab.grab(xdisplay=display).convert("RGB")
                if kind == "screenshot":
                    output = io.BytesIO()
                    screen.save(output, format="PNG")
                    artifact = _write_artifact(action["path"], output.getvalue(), context)
                    screenshots.append(artifact["path"])
                else:
                    if action["x"] >= screen.width or action["y"] >= screen.height:
                        raise ValueError("pixel coordinates are outside the configured display")
                    actual = screen.getpixel((action["x"], action["y"]))
                    if any(abs(a - b) > action.get("tolerance", 0) for a, b in zip(actual, action["rgb"])):
                        raise AssertionError(f"screen pixel {actual} did not match {tuple(action['rgb'])}")
                    assertions += 1
    return {"display": display, "action_count": len(actions), "assertions": assertions, "screenshots": screenshots}


def desktop_tools() -> dict[str, Any]:
    """Register the explicit desktop tool; runtime invocation probes availability."""
    return {"desktop.run": _desktop_run}
