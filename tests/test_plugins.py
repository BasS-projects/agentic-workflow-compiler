"""Trusted plugin registration, identity pins and rejection before factory effects."""

import hashlib
import importlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

from agentic_workflow.plugins import PluginError, load_tool_plugins
from agentic_workflow.worker import TaskContext


class PluginTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.module = self.path / "fixture_plugin.py"
        self.module.write_text('''__version__ = "1.4.2"
def factory(config):
    def multiply(args, context):
        return {"value": args["value"] * config["factor"]}
    return {"fixture.multiply": multiply}
def reserved(config):
    return {"core.value": lambda args, context: {}}
def invalid(config):
    return {"bad": 42}
''')
        sys.path.insert(0, str(self.path))
        importlib.invalidate_caches()
        self.addCleanup(lambda: sys.path.remove(str(self.path)))
        self.addCleanup(lambda: sys.modules.pop("fixture_plugin", None))

    def test_explicit_factory_receives_config_and_records_code_identity(self):
        digest = hashlib.sha256(self.module.read_bytes()).hexdigest()
        registry = load_tool_plugins({"plugins": [{"entrypoint": "fixture_plugin:factory", "config": {"factor": 7, "secret": "not-metadata"}, "sha256": digest}]})
        self.assertEqual(registry["fixture.multiply"]({"value": 6}, TaskContext(self.temp.name, "run", "step", "key")), {"value": 42})
        self.assertEqual(registry.metadata[0]["version"], "1.4.2")
        self.assertEqual(registry.metadata[0]["module_sha256"], digest)
        self.assertNotIn("not-metadata", json.dumps(registry.metadata))

    def test_pin_mismatch_rejects_before_module_import(self):
        with self.assertRaisesRegex(PluginError, "sha256 mismatch"):
            load_tool_plugins({"plugins": [{"entrypoint": "fixture_plugin:factory", "sha256": "0" * 64}]})
        self.assertNotIn("fixture_plugin", sys.modules)

    def test_workflow_import_fields_cannot_be_plugin_configuration(self):
        for config in ({"steps": []}, {"plugins": [], "module": "os"}, {"plugins": [{"entrypoint": "os:system", "args": "pwd"}]}):
            with self.subTest(config=config), self.assertRaises(PluginError):
                load_tool_plugins(config)

    def test_reserved_duplicate_and_invalid_tool_registration_are_rejected(self):
        for factory in ("reserved", "invalid"):
            with self.subTest(factory=factory), self.assertRaises(PluginError):
                load_tool_plugins({"plugins": [{"entrypoint": f"fixture_plugin:{factory}"}]})
        with self.assertRaisesRegex(PluginError, "duplicate"):
            load_tool_plugins({"plugins": [{"builtin": "desktop"}, {"builtin": "desktop"}]})

    def test_builtins_require_explicit_valid_config(self):
        tools = load_tool_plugins({"plugins": [{"builtin": "browser", "config": {"allowed_origins": ["https://example.test"]}}, {"builtin": "desktop"}]})
        self.assertEqual(set(tools), {"browser.run", "desktop.run"})
        for item in ({"builtin": "browser"}, {"builtin": "desktop", "config": {"shell": "bash"}}, {"builtin": "unknown"}):
            with self.subTest(item=item), self.assertRaises(ValueError):
                load_tool_plugins({"plugins": [item]})


if __name__ == "__main__":
    unittest.main()
