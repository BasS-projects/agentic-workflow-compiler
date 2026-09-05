"""Real Playwright actions in an ephemeral Chromium browser context.

Origins are an operator-configured network allowlist, not a hostile-browser
sandbox. Browser workers should run in their own restricted container/network.
No workflow JavaScript, cookies, profiles, shell commands or launch flags are
accepted. Artifacts use descriptor-relative, symlink-rejecting workspace writes.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit
import uuid

from .worker import TaskContext, _path_info, _parent_directory, _check_write_target

__version__ = "0.2.0"


class BrowserUnavailableError(RuntimeError):
    """Playwright or its real Chromium executable is unavailable."""


class BrowserPolicyError(ValueError):
    """The workflow requests browser behavior outside its configured limits."""


def _origin(url: str) -> str:
    if not isinstance(url, str) or len(url) > 8192:
        raise BrowserPolicyError("URL must be a string of at most 8192 characters")
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username is not None or parts.password is not None:
        raise BrowserPolicyError("browser URLs require HTTP(S) without embedded credentials")
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError as exc:
        raise BrowserPolicyError("invalid URL port") from exc
    host = parts.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    return f"{parts.scheme}://{host}:{port}"


def _artifact_path(path: str, context: TaskContext) -> str:
    if not isinstance(path, str):
        raise ValueError("artifact path must be a string")
    root, parts, relative = _path_info(path, context)
    if not parts:
        raise ValueError("artifact path must name a file")
    try:
        with _parent_directory(root, parts) as (parent, name):
            _check_write_target(parent, name)
    except FileNotFoundError:
        pass
    return relative


def _write_artifact(path: str, content: bytes, context: TaskContext) -> dict[str, Any]:
    if len(content) > 16 * 1024 * 1024:
        raise ValueError("artifact exceeds 16 MiB limit")
    _artifact_path(path, context)
    root, parts, relative = _path_info(path, context)
    with _parent_directory(root, parts, create=True) as (parent, name):
        _check_write_target(parent, name)
        temporary = f".rpa-{uuid.uuid4().hex}.tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            _check_write_target(parent, name)
            os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        finally:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass
    return {"path": relative, "bytes": len(content)}


def _string(value: Any, label: str, maximum: int = 65536) -> str:
    if not isinstance(value, str) or len(value) > maximum:
        raise BrowserPolicyError(f"{label} must be a string of at most {maximum} characters")
    return value


@dataclass(frozen=True)
class BrowserRun:
    allowed_origins: frozenset[str]

    def __call__(self, args: dict[str, Any], context: TaskContext) -> dict[str, Any]:
        if not isinstance(args, dict) or set(args) - {"url", "actions", "headless", "timeout_ms"}:
            raise BrowserPolicyError("unsupported browser arguments")
        url = args.get("url")
        if _origin(url) not in self.allowed_origins:
            raise BrowserPolicyError("initial URL origin is not allowed")
        actions = args.get("actions")
        if not isinstance(actions, list) or len(actions) > 100:
            raise BrowserPolicyError("actions must be a list of at most 100 actions")
        headless = args.get("headless", True)
        if not isinstance(headless, bool):
            raise BrowserPolicyError("headless must be boolean")
        timeout = args.get("timeout_ms", 10000)
        if type(timeout) is not int or not 100 <= timeout <= 30000:
            raise BrowserPolicyError("timeout_ms must be between 100 and 30000")
        fields = {
            "fill": {"action", "selector", "value"}, "click": {"action", "selector"},
            "text": {"action", "selector", "name"}, "assert_text": {"action", "selector", "value"},
            "screenshot": {"action", "path"}, "download": {"action", "selector", "path"},
        }
        names: set[str] = set()
        for action in actions:
            if not isinstance(action, dict) or action.get("action") not in fields:
                raise BrowserPolicyError("unsupported browser action")
            if set(action) != fields[action["action"]]:
                raise BrowserPolicyError("browser action has missing or unexpected fields")
            for key in ("selector", "name", "value"):
                if key in action:
                    _string(action[key], key, 1024 if key != "value" else 65536)
            if "selector" in action and not action["selector"]:
                raise BrowserPolicyError("selector cannot be empty")
            if "name" in action:
                if not action["name"] or action["name"] in names:
                    raise BrowserPolicyError("text result names must be nonempty and unique")
                names.add(action["name"])
            if "path" in action:
                _artifact_path(action["path"], context)
        try:
            from playwright.sync_api import sync_playwright, expect
        except ImportError as exc:
            raise BrowserUnavailableError("install the browser extra and run: python -m playwright install chromium") from exc
        blocked: list[str] = []

        def check_url(value: str) -> bool:
            try:
                origin = _origin(value)
            except BrowserPolicyError:
                return False
            return origin in self.allowed_origins

        def ensure_policy() -> None:
            if blocked:
                raise BrowserPolicyError("browser request or navigation left the allowed origins")

        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(headless=headless)
            except Exception as exc:
                raise BrowserUnavailableError("Chromium launch failed; install the browser executable and its OS dependencies") from exc
            try:
                session = browser.new_context(accept_downloads=True, service_workers="block", viewport={"width": 1280, "height": 720})
                session.set_default_timeout(timeout)

                def route_request(route: Any) -> None:
                    if not check_url(route.request.url):
                        blocked.append("request")
                        route.abort("blockedbyclient")
                        return
                    # Disable automatic redirect following in the transport, so
                    # an off-origin Location is rejected BEFORE contacting it.
                    response = route.fetch(max_redirects=0, timeout=timeout)
                    target = response.headers.get("location")
                    if 300 <= response.status < 400 and target and not check_url(urljoin(route.request.url, target)):
                        blocked.append("redirect")
                        route.abort("blockedbyclient")
                    else:
                        route.fulfill(response=response)

                session.route("**/*", route_request)
                session.route_web_socket("**/*", lambda socket: socket.close())

                def watch_page(opened: Any) -> None:
                    def navigation(frame: Any) -> None:
                        if frame.url != "about:blank" and not check_url(frame.url):
                            blocked.append("navigation")
                    opened.on("framenavigated", navigation)
                session.on("page", watch_page)
                page = session.new_page()
                texts: dict[str, str] = {}
                screenshots: list[str] = []
                downloads: list[dict[str, Any]] = []
                try:
                    response = page.goto(url, wait_until="domcontentloaded", timeout=timeout)
                    ensure_policy()
                    if response is not None and response.status >= 400:
                        raise RuntimeError(f"browser initial page returned HTTP {response.status}")
                    for action in actions:
                        ensure_policy()
                        kind = action["action"]
                        locator = page.locator(action["selector"]) if "selector" in action else None
                        if kind == "fill":
                            locator.fill(action["value"])
                        elif kind == "click":
                            locator.click()
                        elif kind == "text":
                            texts[action["name"]] = locator.inner_text()
                        elif kind == "assert_text":
                            expect(locator).to_have_text(action["value"], timeout=timeout)
                        elif kind == "screenshot":
                            artifact = _write_artifact(action["path"], page.screenshot(type="png"), context)
                            screenshots.append(artifact["path"])
                        elif kind == "download":
                            with page.expect_download(timeout=timeout) as pending:
                                locator.click()
                            download = pending.value
                            failure = download.failure()
                            if failure:
                                raise RuntimeError("browser download failed")
                            temporary = download.path()
                            if temporary is None:
                                raise RuntimeError("browser download has no local file")
                            with Path(temporary).open("rb") as stream:
                                content = stream.read(16 * 1024 * 1024 + 1)
                            downloads.append(_write_artifact(action["path"], content, context))
                        ensure_policy()
                    if not check_url(page.url):
                        raise BrowserPolicyError("final page URL is not allowed")
                    return {"url": page.url, "title": page.title(), "texts": texts,
                            "screenshots": screenshots, "downloads": downloads, "action_count": len(actions)}
                except Exception:
                    ensure_policy()
                    raise
                finally:
                    session.close()
            finally:
                browser.close()


def browser_tools(allowed_origins: list[str] | tuple[str, ...]) -> dict[str, BrowserRun]:
    """Return an explicit browser.run registration with mandatory exact origins."""
    if not isinstance(allowed_origins, (list, tuple)) or not allowed_origins or len(allowed_origins) > 64:
        raise BrowserPolicyError("allowed_origins must contain 1 to 64 exact origins")
    normalized = set()
    for origin in allowed_origins:
        normalized.add(_origin(origin))
        parsed = urlsplit(origin)
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise BrowserPolicyError("allowed_origins entries must be origins without paths, queries or fragments")
    return {"browser.run": BrowserRun(frozenset(normalized))}
