from dataclasses import FrozenInstanceError
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agentic_workflow.worker import TaskContext, default_tools


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.context = TaskContext(str(self.workspace), "run", "step", "run:step")
        self.tools = default_tools()

    def call(self, name, **args):
        return self.tools[name](args, self.context)

    def test_context_frozen_and_registries_independent(self):
        with self.assertRaises(FrozenInstanceError):
            self.context.step_id = "different"
        self.tools.pop("core.value")
        self.assertIn("core.value", default_tools())

    def test_utf8_atomic_write_and_repeat(self):
        text = "  สวัสดี\r\nworld\n"
        expected = {"path": "output/result.txt", "bytes": len(text.encode("utf-8"))}
        self.assertEqual(self.call("files.write_text", path="output/result.txt", text=text), expected)
        self.assertEqual(self.call("files.read_text", path="output/result.txt"), {"text": text})
        self.assertEqual(self.call("files.write_text", path="output/result.txt", text=text), expected)
        self.assertEqual(list((self.workspace / "output").iterdir()), [self.workspace / "output/result.txt"])

    def test_exists_require_exists_and_contained_absolute_path(self):
        self.assertEqual(self.call("files.exists", path="missing/file"), {"exists": False, "path": "missing/file"})
        with self.assertRaises(FileNotFoundError):
            self.call("files.require_exists", path="missing/file")
        target = self.workspace / "present.txt"
        target.write_text("yes", encoding="utf-8")
        self.assertEqual(self.call("files.require_exists", path=str(target)), {"exists": True, "path": "present.txt"})
        self.assertEqual(self.call("files.exists", path="present.txt/nested"), {"exists": False, "path": "present.txt/nested"})
        self.assertTrue(self.call("files.exists", path=".")["exists"])

    def test_normalize_trims_lines_preserves_internal_blank_lines(self):
        self.assertEqual(
            self.call("text.normalize", text="\r\n  one  \r\n \r\n  สอง\t\n\n"),
            {"text": "one\n\nสอง"},
        )
        self.assertEqual(self.call("text.normalize", text=" \n\t\r\n"), {"text": ""})

    def test_rejects_escape_and_invalid_paths(self):
        outside = self.root / "secret.txt"
        outside.write_text("outside", encoding="utf-8")
        for path in ("../secret.txt", str(outside), "nested/../../secret.txt", "", "bad\x00path"):
            for name in ("files.exists", "files.require_exists", "files.read_text", "files.write_text"):
                with self.subTest(path=path, tool=name), self.assertRaises(ValueError):
                    self.call(name, path=path, text="changed")
        self.assertEqual(outside.read_text(encoding="utf-8"), "outside")

    @unittest.skipUnless(hasattr(os, "symlink"), "symlinks unavailable")
    def test_rejects_parent_target_and_internal_symlinks(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("outside", encoding="utf-8")
        (self.workspace / "redirect").symlink_to(outside, target_is_directory=True)
        (self.workspace / "link.txt").symlink_to(outside / "secret.txt")
        (self.workspace / "inside.txt").write_text("inside", encoding="utf-8")
        (self.workspace / "internal.txt").symlink_to(self.workspace / "inside.txt")
        (self.workspace / "dangling.txt").symlink_to(outside / "absent.txt")
        for path in ("redirect/secret.txt", "redirect/new/file.txt", "link.txt", "internal.txt", "dangling.txt"):
            for name in ("files.exists", "files.read_text", "files.write_text"):
                with self.subTest(path=path, tool=name), self.assertRaises(ValueError):
                    self.call(name, path=path, text="changed")
        self.assertEqual((outside / "secret.txt").read_text(encoding="utf-8"), "outside")
        self.assertFalse((outside / "new").exists())
        self.assertEqual((self.workspace / "inside.txt").read_text(encoding="utf-8"), "inside")

    def test_failed_replace_preserves_original_and_removes_temp(self):
        target = self.workspace / "result.txt"
        target.write_text("original", encoding="utf-8")
        with patch("agentic_workflow.worker.os.replace", side_effect=OSError("disk failure")):
            with self.assertRaisesRegex(OSError, "disk failure"):
                self.call("files.write_text", path="result.txt", text="replacement")
        self.assertEqual(target.read_text(encoding="utf-8"), "original")
        self.assertEqual(list(self.workspace.iterdir()), [target])

    def test_argument_validation_and_value(self):
        for name, args in (
            ("files.read_text", {}),
            ("files.exists", {"path": None}),
            ("files.write_text", {"path": "file.txt", "text": 1}),
            ("text.normalize", {"text": ["x"]}),
            ("core.value", {}),
        ):
            with self.subTest(tool=name), self.assertRaises(ValueError):
                self.tools[name](args, self.context)
        self.assertEqual(self.call("core.value", value={"count": 3}), {"value": {"count": 3}})
        self.assertEqual(self.call("core.value", value=None), {"value": None})

    def test_read_directory_and_write_directory_rejected(self):
        (self.workspace / "folder").mkdir()
        with self.assertRaises(ValueError):
            self.call("files.read_text", path="folder")
        with self.assertRaises(IsADirectoryError):
            self.call("files.write_text", path="folder", text="text")


if __name__ == "__main__":
    unittest.main()
