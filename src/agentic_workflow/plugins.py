"""Explicit, operator-trusted Python plugins; workflow IR never imports code.

This is an extension boundary, not a Python sandbox. A configured entrypoint
executes with the worker's operating-system privileges. Keep this configuration
outside request bodies and review/pin installed plugin code before enabling it.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import importlib.util
import inspect
from pathlib import Path
import re
from typing import Any

from .worker import Tool, default_tools


class PluginError(ValueError):
    """An explicit plugin configuration or registration is invalid."""


class PluginRegistry(dict[str, Tool]):
    """A normal tool mapping with secret-free, diagnostic plugin metadata."""

    def __init__(self) -> None:
        super().__init__()
        self.metadata: list[dict[str, Any]] = []


def _fingerprint(module: Any) -> str | None:
    source = inspect.getsourcefile(module)
    return hashlib.sha256(Path(source).read_bytes()).hexdigest() if source else None


def load_tool_plugins(config: dict[str, Any]) -> PluginRegistry:
    """Load only factories specified in trusted, caller-owned configuration.

    Schema: {"plugins": [{"builtin": "browser", "config": {...}},
                         {"entrypoint": "pkg.module:factory", "config": {...}}]}.
    Optional ``sha256`` pins the entrypoint module file before its import. This
    identifies that file, not its transitive dependencies or an authenticity proof.
    """
    if not isinstance(config, dict) or set(config) != {"plugins"}:
        raise PluginError("plugin config must contain only a plugins list")
    if not isinstance(config["plugins"], list) or len(config["plugins"]) > 32:
        raise PluginError("plugins must be a list of at most 32 entries")
    registry = PluginRegistry()
    reserved = set(default_tools())
    for item in config["plugins"]:
        if not isinstance(item, dict) or set(item) - {"builtin", "entrypoint", "config", "sha256"}:
            raise PluginError("invalid plugin configuration fields")
        if ("builtin" in item) == ("entrypoint" in item):
            raise PluginError("each plugin requires exactly one builtin or entrypoint")
        options = item.get("config", {})
        if not isinstance(options, dict):
            raise PluginError("plugin config options must be an object")
        if "builtin" in item:
            if "sha256" in item:
                raise PluginError("sha256 pins are supported for custom entrypoints only")
            if item["builtin"] == "browser":
                if set(options) != {"allowed_origins"}:
                    raise PluginError("browser config requires only allowed_origins")
                from . import rpa as module
                factory = module.browser_tools
                tools = factory(options["allowed_origins"])
            elif item["builtin"] == "desktop":
                if options:
                    raise PluginError("desktop config must be empty; configure DISPLAY on the worker")
                from . import desktop as module
                factory = module.desktop_tools
                tools = factory()
            else:
                raise PluginError("unknown builtin plugin")
            identity = f"builtin:{item['builtin']}"
        else:
            entrypoint = item["entrypoint"]
            if not isinstance(entrypoint, str) or not re.fullmatch(
                r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*", entrypoint
            ):
                raise PluginError("entrypoint must be a module:factory identifier")
            module_name, attribute = entrypoint.split(":")
            pin = item.get("sha256")
            if pin is not None:
                if not isinstance(pin, str) or not re.fullmatch(r"[0-9a-f]{64}", pin):
                    raise PluginError("sha256 must be a lowercase SHA-256 digest")
                # find_spec may import parent packages; all configured packages
                # remain trusted. No workflow-supplied module names reach here.
                spec = importlib.util.find_spec(module_name)
                if spec is None or not spec.origin or not Path(spec.origin).is_file():
                    raise PluginError("pinned plugin must have a readable module file")
                if hashlib.sha256(Path(spec.origin).read_bytes()).hexdigest() != pin:
                    raise PluginError("plugin module sha256 mismatch")
            module = importlib.import_module(module_name)
            factory = getattr(module, attribute, None)
            if not callable(factory):
                raise PluginError("plugin entrypoint must be a callable factory")
            tools = factory(options)
            identity = entrypoint
        if not isinstance(tools, dict) or not tools:
            raise PluginError("plugin factory must return a nonempty tool dictionary")
        for name, tool in tools.items():
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+", name):
                raise PluginError("tool names must be dotted identifiers")
            if not callable(tool):
                raise PluginError(f"tool {name} must be callable")
            if name in reserved or name in registry:
                raise PluginError(f"duplicate or reserved tool name: {name}")
        registry.update(tools)
        version = getattr(module, "__version__", None)
        if version is None:
            try:
                version = importlib.metadata.version(module.__name__.split(".")[0].replace("_", "-"))
            except importlib.metadata.PackageNotFoundError:
                version = "unknown"
        registry.metadata.append({
            "plugin": identity, "version": str(version),
            "module_sha256": _fingerprint(module), "tools": sorted(tools),
        })
    return registry
