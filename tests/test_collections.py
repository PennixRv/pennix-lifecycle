from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import skill_collections as collections


class CollectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name)
        self.home_patch = patch.object(Path, "home", return_value=self.home)
        self.home_patch.start()
        self.codex = self.home / "codex"
        self.manifest = {"schema": 1, "name": "example", "skills": {
            "example-skill": {"path": "skills/example-skill", "license": None}},
            "commands": {"example": "example-skill/bin/example"}, "materialized": {}}
        self.component = {"collection_contract": {
            "directory": "example", "source": {"repo": "example/skills", "ref": "a" * 40,
            "manifest": "collection.json"}}}

    def tearDown(self):
        self.home_patch.stop()
        self.temporary.cleanup()

    def stage(self, component, workspace):
        staging = workspace / "collection"
        skill = staging / "example-skill"
        (skill / "bin").mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\nname: example-skill\ndescription: test\n---\nExample\n")
        command = skill / "bin/example"
        command.write_text("#!/bin/sh\nexit 0\n")
        command.chmod(0o755)
        return staging, self.manifest

    def install(self):
        with patch.object(collections, "_stage", side_effect=self.stage):
            self.assertEqual("changed", collections.operate(self.component, self.codex, "install"))
        return collections.destination(self.component, self.codex)

    def test_atomic_install_upgrade_and_uninstall(self):
        path = self.install()
        source = collections.read_receipt(path)["source"]
        self.assertEqual("a" * 40, source["ref"])
        self.assertEqual("match", collections.inspect(self.component, self.codex)[0])
        self.component["collection_contract"]["source"]["ref"] = "b" * 40
        self.assertEqual("upgrade-required", collections.inspect(self.component, self.codex)[0])
        with patch.object(collections, "_stage", side_effect=self.stage):
            collections.operate(self.component, self.codex, "upgrade")
        self.assertEqual("b" * 40, collections.read_receipt(path)["source"]["ref"])
        collections.operate(self.component, self.codex, "uninstall")
        self.assertFalse(path.exists())
        self.assertFalse(collections.receipt_path(path).exists())
        self.assertFalse((self.home / ".local/bin/example").exists())

    def test_content_or_command_drift_is_never_deleted(self):
        path = self.install()
        (path / "example-skill/SKILL.md").write_text("modified")
        self.assertEqual("drifted", collections.inspect(self.component, self.codex)[0])
        with self.assertRaises(collections.CollectionError):
            collections.operate(self.component, self.codex, "uninstall")
        self.assertEqual("modified", (path / "example-skill/SKILL.md").read_text())

    def test_old_receipt_schema_is_rejected_without_compatibility(self):
        path = self.install()
        receipt = collections.receipt_path(path)
        value = json.loads(receipt.read_text())
        value["schema"] = 2
        receipt.write_text(json.dumps(value))
        self.assertEqual("drifted", collections.inspect(self.component, self.codex)[0])
        with self.assertRaises(collections.CollectionError):
            collections.operate(self.component, self.codex, "uninstall")
        self.assertTrue(path.exists())

    def test_failed_receipt_cutover_restores_tree_receipt_and_command(self):
        path = self.install()
        before = collections.receipt_path(path).read_bytes()
        original = collections.digest(path)
        self.component["collection_contract"]["source"]["ref"] = "b" * 40
        replace = os.replace
        failed = False
        def fail_once(source, target):
            nonlocal failed
            if Path(target) == collections.receipt_path(path) and not failed:
                failed = True
                raise OSError("receipt failure")
            return replace(source, target)
        with patch.object(collections, "_stage", side_effect=self.stage), patch.object(collections.os, "replace", side_effect=fail_once):
            with self.assertRaisesRegex(OSError, "receipt failure"):
                collections.operate(self.component, self.codex, "upgrade")
        self.assertEqual(before, collections.receipt_path(path).read_bytes())
        self.assertEqual(original, collections.digest(path))
        self.assertTrue((self.home / ".local/bin/example").is_symlink())

    def test_failed_rollback_keeps_recoverable_copy(self):
        path = self.install()
        self.component["collection_contract"]["source"]["ref"] = "b" * 40
        replace = os.replace
        failed_cutover = False
        def fail_cutover_and_rollback(source, target):
            nonlocal failed_cutover
            if Path(target) == collections.receipt_path(path):
                if not failed_cutover:
                    failed_cutover = True
                    raise OSError("receipt failure")
            if Path(target) == path and failed_cutover:
                raise OSError("tree restore failure")
            return replace(source, target)
        with patch.object(collections, "_stage", side_effect=self.stage), patch.object(collections.os, "replace", side_effect=fail_cutover_and_rollback):
            with self.assertRaisesRegex(collections.CollectionError, "可恢复资产保留"):
                collections.operate(self.component, self.codex, "upgrade")
        self.assertTrue(list(path.parent.glob(".example.stage-*/previous")))

    def test_manifest_duplicate_yaml_and_bad_paths_are_rejected(self):
        bad = "---\nname: example-skill\nname: other\ndescription: test\n---\n"
        with tempfile.TemporaryDirectory() as directory:
            staging = Path(directory)
            skill = staging / "example-skill"
            skill.mkdir()
            (skill / "SKILL.md").write_text(bad)
            with self.assertRaises(collections.CollectionError):
                collections.validate_members(staging, self.manifest)
        with self.assertRaises(collections.CollectionError):
            collections.relative("../escape")


if __name__ == "__main__":
    unittest.main()
