from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import skill_collections as collections
import pennix


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

    def stage(self, component, workspace, candidate=None):
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
        self.assertEqual(self.home / ".agents/skills/example", path)
        self.assertEqual(path, collections.destination(self.component, self.home / "another-codex"))
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

    def legacy_install(self):
        current = self.install()
        legacy = self.codex / "skills/example"
        legacy.parent.mkdir(parents=True)
        shutil.move(current, legacy)
        record = json.loads(collections.receipt_path(current).read_text())
        record["target"] = str(legacy)
        collections.receipt_path(legacy).write_bytes(collections.files.json_bytes(record))
        collections.receipt_path(legacy).chmod(0o600)
        collections.receipt_path(current).unlink()
        link = self.home / ".local/bin/example"
        link.unlink()
        link.symlink_to(legacy / "example-skill/bin/example")
        return legacy

    def test_migration_preserves_locked_identity_and_retires_old_tree(self):
        old = self.legacy_install()
        original = collections.read_receipt(old)
        self.assertEqual("migration-required", collections.inspect(self.component, self.codex)[0])
        with patch.object(collections, "_stage", side_effect=AssertionError("migration must not upgrade or fetch")):
            collections.migrate(self.component, self.codex)
        target = collections.destination(self.component, self.codex)
        self.assertFalse(old.exists())
        self.assertFalse(collections.receipt_path(old).exists())
        self.assertEqual(original["source"], collections.read_receipt(target)["source"])
        self.assertEqual(original["digest"], collections.digest(target))
        self.assertEqual(str(target / "example-skill/bin/example"), os.readlink(self.home / ".local/bin/example"))
        self.assertEqual("match", collections.inspect(self.component, self.codex)[0])

    def test_interrupted_migration_replays_from_private_snapshot(self):
        old = self.legacy_install()
        target = collections.destination(self.component, self.codex)
        copy = shutil.copytree
        def interrupt(source, destination, *args, **kwargs):
            if destination == target:
                raise OSError("simulated process interruption after retirement")
            return copy(source, destination, *args, **kwargs)
        with patch.object(collections.shutil, "copytree", side_effect=interrupt):
            with self.assertRaisesRegex(OSError, "simulated"):
                collections.migrate(self.component, self.codex)
        self.assertFalse(old.exists())
        self.assertEqual("migration-required", collections.inspect(self.component, self.codex)[0])
        workspace = collections._migration_dir(self.component, self.codex)
        self.assertFalse(workspace.is_relative_to(target.parent))
        self.assertEqual("a" * 40, collections._migration_receipt(self.component, self.codex)["source"]["ref"])
        collections.migrate(self.component, self.codex)
        self.assertEqual("match", collections.inspect(self.component, self.codex)[0])
        self.assertFalse(workspace.exists())

    def test_migration_refuses_drift_conflicts_and_source_upgrade(self):
        old = self.legacy_install()
        target = collections.destination(self.component, self.codex)
        target.mkdir(parents=True)
        with self.assertRaisesRegex(collections.CollectionError, "目标已存在"):
            collections.migrate(self.component, self.codex)
        target.rmdir()
        self.component["_candidate_target"] = {"ref": "b" * 40}
        with self.assertRaisesRegex(collections.CollectionError, "先 repair"):
            collections.migrate(self.component, self.codex)
        del self.component["_candidate_target"]
        (old / "example-skill/SKILL.md").write_text("user changes")
        with self.assertRaises(collections.CollectionError):
            collections.migrate(self.component, self.codex)
        self.assertEqual("user changes", (old / "example-skill/SKILL.md").read_text())

    def test_native_repair_journal_and_recover_replay_interrupted_migration(self):
        old = self.legacy_install()
        target = collections.destination(self.component, self.codex)
        catalog = pennix.owner_engine.load_catalog(pennix.CATALOG)
        component = catalog["components"]["pennix-skills-extra"]
        component["collection_contract"] = self.component["collection_contract"]
        fixture_catalog = self.home / "catalog.json"
        fixture_catalog.write_bytes(collections.files.json_bytes(catalog))
        environment = {"HOME": str(self.home), "CODEX_HOME": str(self.codex),
                       "XDG_CONFIG_HOME": str(self.home / "config"), "XDG_STATE_HOME": str(self.home / "state")}
        with patch.dict(os.environ, environment), patch.object(pennix, "CATALOG", fixture_catalog), patch.object(pennix, "_emit"):
            config = pennix.config_model.init(str(self.home / "workflow.config"))
            kconf, _ = pennix.config_model.load(str(config))
            for symbol in kconf.unique_defined_syms:
                if symbol.name.startswith("PENNIX_COMPONENT_"):
                    symbol.set_value("n")
            kconf.syms["PENNIX_COMPONENT_PENNIX_SKILLS_EXTRA"].set_value("y")
            kconf.syms["PENNIX_CODEX_HOME"].set_value(str(self.codex))
            pennix.config_model.write(kconf, config)
            entry = {"source": component["lock_source"], "ref": "a" * 40}
            lock = pennix.component_lock.path_for(config)
            pennix.component_lock.write(lock, {"schema": 1, "components": {"pennix-skills-extra": entry}},
                                       {"pennix-skills-extra": component["lock_source"]})
            args = SimpleNamespace(component="pennix-skills-extra", config=str(config), command="repair", json=True, expected_sha256=None)
            copy = shutil.copytree
            def interrupt(source, destination, *rest, **kwargs):
                if destination == target:
                    raise OSError("native migration interrupted")
                return copy(source, destination, *rest, **kwargs)
            with patch.object(collections.shutil, "copytree", side_effect=interrupt):
                with self.assertRaisesRegex(OSError, "native migration interrupted"):
                    pennix.cmd_repair(args)
            journal = pennix.apply_journal.read()
            self.assertEqual("unknown", journal["steps"][0]["status"])
            self.assertFalse(old.exists())
            recovery = SimpleNamespace(command="recover", config=str(config), json=True,
                transaction=journal["transaction"], step="pennix-skills-extra:repair", resolve="completed")
            self.assertEqual(0, pennix.cmd_recover(recovery))
            self.assertIsNone(pennix.apply_journal.read())
            self.assertEqual("match", collections.inspect(self.component, self.codex)[0])
            self.assertEqual(entry, pennix.component_lock.read(lock, {"pennix-skills-extra": component["lock_source"]})["components"]["pennix-skills-extra"])
            kconf.syms["PENNIX_COMPONENT_PENNIX_SKILLS_EXTRA"].set_value("n")
            pennix.config_model.write(kconf, config)
            deselected = pennix._plan(SimpleNamespace(config=str(config)))
            self.assertEqual("keep", deselected["components"]["pennix-skills-extra"]["action"])
            self.assertTrue(target.exists())

    def test_deselected_old_source_receipt_can_be_removed(self):
        path = self.install()
        self.component["collection_contract"]["source"] = {
            "repo": "new/skills", "ref": "b" * 40, "manifest": "collection.json"}
        self.assertEqual("blocked", collections.inspect(self.component, self.codex)[0])
        self.assertEqual("owned", collections.receipt_owned(self.component, self.codex))
        self.assertEqual("changed", collections.operate(self.component, self.codex, "uninstall"))
        self.assertFalse(path.exists())

    def test_explicit_candidate_replaces_a_matching_collection(self):
        path = self.install()
        candidate = {"repo": "example/skills", "ref": "b" * 40, "sha256": "c" * 64,
                     "path": "/tmp/source.tar.gz", "checkout": "/tmp/source"}
        self.component["_candidate_target"] = {"ref": candidate["ref"]}
        with patch.object(collections, "_stage", side_effect=self.stage):
            self.assertEqual("changed", collections.operate(self.component, self.codex, "upgrade", candidate=candidate))
        self.assertEqual(candidate["ref"], collections.read_receipt(path)["source"]["ref"])

    def test_update_candidate_takes_precedence_over_existing_lock(self):
        component = {**self.component, "_lock_target": {"ref": "a" * 40},
                     "_candidate_target": {"ref": "b" * 40}}
        self.assertEqual("b" * 40, collections.source_identity(component)["ref"])

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
        self.assertTrue(list(path.parent.parent.glob(".example.stage-*/previous")))

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
