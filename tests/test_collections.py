from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tarfile
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

    def test_retired_runtime_receipt_can_upgrade_without_reinstalling_retired_members(self):
        from copy import deepcopy
        current = deepcopy(self.manifest)
        self.manifest["skills"]["old-search"] = {"path": "skills/old-search", "license": None}
        self.manifest["commands"]["old-search"] = "old-search/search"
        self.manifest["materialized"]["old-search"] = {
            "repo": "example/old-search", "ref": "a" * 40, "commit": "a" * 40,
            "name": "old-search", "snapshot": "archive", "post_install": ["grok-search-runtime"]}
        with self.assertRaises(collections.CollectionError):
            collections.validate_manifest(self.manifest, "example")
        def old_stage(component, workspace, candidate=None):
            staging, manifest = self.stage(component, workspace, candidate)
            old = staging / "old-search"
            old.mkdir()
            (old / "SKILL.md").write_text("---\nname: old-search\ndescription: old\n---\n")
            (old / "search").write_text("#!/bin/sh\nexit 0\n")
            (old / "search").chmod(0o755)
            return staging, manifest
        with patch.object(collections, "_stage", side_effect=old_stage):
            collections.operate(self.component, self.codex, "install")
        path = collections.destination(self.component, self.codex)
        self.assertEqual("match", collections.inspect(self.component, self.codex)[0])
        self.manifest = current
        self.component["collection_contract"]["source"]["ref"] = "b" * 40
        with patch.object(collections, "_stage", side_effect=self.stage):
            collections.operate(self.component, self.codex, "upgrade")
        self.assertFalse((path / "old-search").exists())
        self.assertFalse((self.home / ".local/bin/old-search").is_symlink())
        self.assertEqual(current, collections.read_receipt(path)["manifest"])
        self.assertEqual("match", collections.inspect(self.component, self.codex)[0])
        invalid = deepcopy(current)
        invalid["materialized"]["example-skill"] = {
            "repo": "example/source", "ref": "a" * 40, "commit": "a" * 40,
            "name": "example-skill", "snapshot": "archive", "post_install": ["unknown-runtime"]}
        with self.assertRaises(collections.CollectionError):
            collections.validate_manifest(invalid, "example", reading_receipt=True)

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


class ScopedCollectionTests(unittest.TestCase):
    key = pennix.SCOPED_COLLECTION

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        env = {"HOME": str(self.root), "XDG_STATE_HOME": str(self.root / "state"),
               "XDG_CONFIG_HOME": str(self.root / "config"), "CODEX_HOME": str(self.root / ".codex")}
        environment = patch.dict(os.environ, env)
        environment.start()
        self.addCleanup(environment.stop)
        self.config = pennix.config_model.init(str(self.root / "workflow.config"))
        self.catalog = pennix.owner_engine.load_catalog(pennix.CATALOG)
        self.catalog_path = self.root / "catalog.json"
        catalog = patch.object(pennix, "CATALOG", self.catalog_path)
        catalog.start()
        self.addCleanup(catalog.stop)
        self.archives = {}
        self.archive("a" * 40, "First")
        resolver = patch.object(pennix.candidates, "_download_archive", side_effect=self.download)
        self.download_mock = resolver.start()
        self.addCleanup(resolver.stop)
        emitter = patch.object(pennix, "_emit")
        self.emit = emitter.start()
        self.addCleanup(emitter.stop)
        tools = patch.object(pennix.system_devel_tools, "inspect", return_value={"state": "ready", "missing": []})
        self.tools = tools.start()
        self.addCleanup(tools.stop)
        self.target = self.root / ".agents/skills" / self.key

    def archive(self, ref, text, *, commands=None):
        manifest = {"schema": 1, "name": self.key, "skills": {
            "sample": {"path": "skills/sample", "license": "LICENSE"}},
            "commands": commands or {}, "materialized": {}}
        path = self.root / (ref + ".tar.gz")
        with tarfile.open(path, "w:gz") as archive:
            for name, value in {"collection.json": json.dumps(manifest), "skills/sample/LICENSE": "test",
                                "skills/sample/SKILL.md": f"---\nname: sample\ndescription: test\n---\n{text}\n"}.items():
                raw = value.encode()
                member = tarfile.TarInfo("collection/" + name)
                member.size = len(raw)
                member.mode = 0o644
                archive.addfile(member, io.BytesIO(raw))
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        self.archives[ref] = path
        contract = self.catalog["components"][self.key]["collection_contract"]
        contract["source"]["ref"], contract["archive_sha256"] = ref, digest
        self.catalog_path.write_text(json.dumps(self.catalog))

    def download(self, _, ref, workspace, name):
        out = Path(workspace) / name
        shutil.copyfile(self.archives[ref], out)
        return out, hashlib.sha256(out.read_bytes()).hexdigest()

    def select(self, value):
        kconf, path = pennix.config_model.load(str(self.config))
        kconf.syms["PENNIX_SYSTEM_UPGRADE"].set_value("y")
        kconf.syms["PENNIX_COMPONENT_PENNIX_SYSTEM_DEVEL_SKILLS"].set_value(value)
        pennix.config_model.write(kconf, path)

    def cmd(self, name, *words, scoped=True):
        options = ["--component", self.key] if scoped and name in {"plan", "apply"} else []
        args = pennix.parser().parse_args([name, *options, *words, "--config", str(self.config), "--json"])
        return args.function(args)

    def test_default_off_and_native_scoped_install_is_idempotent(self):
        with self.assertRaisesRegex(pennix.CliError, "已选择"):
            self.cmd("plan")
        self.download_mock.assert_not_called()
        self.select("y")
        other = self.target.parent / "unmanaged"
        other.mkdir(parents=True)
        (other / "keep").write_text("untouched")
        with patch.object(pennix, "_plan", side_effect=AssertionError("no global preflight")):
            self.cmd("plan")
            plan = self.emit.call_args.args[0]
            self.assertEqual(set(plan["components"]), {self.key})
            self.assertFalse(plan["side_effects"]["system_upgrade"])
            self.download_mock.assert_not_called()
            self.cmd("apply")
            self.assertIn("First", (self.target / "sample/SKILL.md").read_text())
            self.assertEqual("a" * 40, collections.read_receipt(self.target)["source"]["ref"])
            self.assertIsNone(pennix.apply_journal.read())
            self.cmd("apply")
            self.assertEqual(self.download_mock.call_count, 1)
            self.assertEqual(self.emit.call_args.args[0]["results"], {self.key: "no-op"})
        self.assertEqual((other / "keep").read_text(), "untouched")
        self.select("n")
        self.assertTrue(self.target.exists())
        self.cmd("uninstall", self.key, "--confirm", self.key)
        self.assertFalse(self.target.exists())
        self.assertEqual((other / "keep").read_text(), "untouched")

    def test_rejects_unmanaged_directory_and_foreign_transaction(self):
        self.select("y")
        self.target.mkdir(parents=True)
        with self.assertRaisesRegex(pennix.CliError, "拒绝"):
            self.cmd("apply")
        self.target.rmdir()
        config_digest = pennix.apply_journal.digest(self.config)
        pending = pennix.apply_journal.start("apply", None, config_digest, None, [
            {"id": "system-upgrade", "component": "system", "action": "upgrade", "target": {}}])
        with self.assertRaisesRegex(pennix.CliError, "不同范围"):
            self.cmd("apply")
        self.assertEqual(pennix.apply_journal.read(), pending)
        self.download_mock.assert_not_called()

    def test_mutated_archive_never_starts_transaction(self):
        self.select("y")
        self.archives["a" * 40].write_bytes(b"not the reviewed archive")
        with self.assertRaisesRegex(pennix.candidates.CandidateError, "catalog pin"):
            self.cmd("apply")
        self.assertIsNone(pennix.apply_journal.read())
        self.assertFalse(self.target.exists())

    def test_documentation_archive_rejects_commands(self):
        self.select("y")
        self.archive("b" * 40, "Second", commands={"unwanted": "sample/bin/unwanted"})
        with self.assertRaisesRegex(pennix.candidates.CandidateError, "不能包含命令"):
            self.cmd("apply")
        self.assertIsNone(pennix.apply_journal.read())
        self.assertFalse(self.target.exists())
        self.assertFalse((self.root / ".local/bin/unwanted").exists())

    def test_missing_tools_block_collection_writes_without_starting_package_transaction(self):
        self.select("y")
        self.tools.return_value = {"state": "missing", "missing": ["meson"]}
        self.cmd("plan")
        value = self.emit.call_args.args[0]
        self.assertEqual(value["components"][self.key]["state"], "missing")
        self.assertEqual(value["components"][self.key]["action"], "blocked")
        self.assertFalse(value["side_effects"]["system_upgrade"])
        for action, words in [("apply", ()), ("update", (self.key,))]:
            with self.subTest(action=action), self.assertRaisesRegex(pennix.CliError, "system-devel-tools"):
                self.cmd(action, *words)
        self.download_mock.assert_not_called()
        self.assertFalse(self.target.exists())
        self.assertIsNone(pennix.apply_journal.read())

    def test_recover_proven_restore_and_preserve_unknown(self):
        self.select("y")
        replace = collections.os.replace
        for fail_rollback in (False, True):
            with self.subTest(fail_rollback=fail_rollback):
                def interrupt(source, target):
                    if Path(target) == collections.receipt_path(self.target):
                        raise OSError("simulated receipt commit failure")
                    if fail_rollback and Path(source) == self.target:
                        raise OSError("simulated rollback failure")
                    return replace(source, target)
                with patch.object(collections.os, "replace", side_effect=interrupt):
                    with self.assertRaises(pennix.CliError):
                        self.cmd("apply")
                j = pennix.apply_journal.read()
                step = j["steps"][0]
                self.assertEqual(step["observed"]["effect"], "unknown" if fail_rollback else "restored")
                args = ["--transaction", j["transaction"], "--step", step["id"], "--resolve", "restored"]
                if fail_rollback:
                    with self.assertRaisesRegex(pennix.CliError, "不能释放 unknown"):
                        self.cmd("recover", *args)
                    self.assertEqual(pennix.apply_journal.read(), j)
                    self.assertTrue(self.target.exists())
                else:
                    self.assertFalse(self.target.exists())
                    self.cmd("recover", *args)
                    self.assertIsNone(pennix.apply_journal.read())

    def test_fixed_source_changes_only_by_update_not_scoped_or_global_apply(self):
        self.select("y")
        self.cmd("apply")
        self.archive("b" * 40, "Second")
        with self.assertRaisesRegex(pennix.CliError, "不升级"):
            self.cmd("apply")
        self.cmd("plan", scoped=False)
        plan = self.emit.call_args.args[0]
        self.assertEqual(plan["components"][self.key]["action"], "keep")
        self.assertEqual(collections.read_receipt(self.target)["source"]["ref"], "a" * 40)
        with patch.object(sys.stdin, "isatty", return_value=True), \
                patch.object(sys.stdout, "isatty", return_value=True), patch("builtins.input", return_value="yes") as confirm:
            self.cmd("update", self.key)
            confirm.assert_called_once()
            self.assertEqual("b" * 40, collections.read_receipt(self.target)["source"]["ref"])
            self.assertIn("Second", (self.target / "sample/SKILL.md").read_text())
            confirm.reset_mock()
            self.cmd("update", self.key)
            self.assertEqual(self.emit.call_args.args[0]["status"], "no-op")
            confirm.assert_not_called()
        self.select("n")
        self.cmd("plan", scoped=False)
        self.assertEqual(self.emit.call_args.args[0]["components"][self.key]["action"], "keep")
        with self.assertRaisesRegex(pennix.CliError, "未在"):
            self.cmd("update", self.key)


if __name__ == "__main__":
    unittest.main()
