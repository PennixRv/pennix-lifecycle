import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import apply_journal
import candidates
import owned_files
import pennix
import ponytail
import update_inventory

COMPONENT = {"id": ponytail.KEY, **json.loads((ROOT / "references/catalog.json").read_text())["components"][ponytail.KEY]}
OUTER = "96608e078b2d5ea607c5c3b8b5a54d1c1fba6846"
DRIFT = "ebd8654c2c627f721eb8f9c0d1bd4090a29f9e10"
FORMAL = "7801d061243d7b753bad0e0b3c057cd250b11755"
OLD = {"source": COMPONENT["lock_source"], "version": "5.3.3", "ref": OUTER}
OLD_LOCK = {**OLD, "sha256": "be0efb08ba8a50572aeafb6d7c3302989bb82da476d74768d59ef2ef18ab1744"}
TARGET = {**OLD, "ref": FORMAL, "sha256": "1" * 64}


class ReleaseIdentityTests(unittest.TestCase):
    def test_official_tag_is_peeled_without_branch_lookup(self):
        tag = "refs/tags/v5.3.3"
        result = subprocess.CompletedProcess([], 0, f'{"a"*40}\t{tag}\n{FORMAL}\t{tag}^{{}}\n', "")
        metadata = {"tag_name": "v5.3.3", "draft": False, "prerelease": False, "published_at": "2026-10-29T03:11:08Z"}
        with patch.object(candidates, "_json", return_value=metadata) as query, \
                patch.object(candidates, "_run", return_value=result) as run:
            self.assertEqual(update_inventory.available(COMPONENT, OLD, {}), {k: TARGET[k] for k in OLD})
            self.assertTrue(query.call_args.args[0].endswith("/releases/latest"))
            self.assertEqual(run.call_args.args[0][-2:], [tag, tag+"^{}"])
            with self.assertRaisesRegex(candidates.CandidateError, "ref changed"):
                candidates.plugin_release(COMPONENT, OLD)
            self.assertTrue(query.call_args.args[0].endswith("/releases/tags/v5.3.3"))
        for change in ({"draft": True}, {"prerelease": True}, {"published_at": None}, {"tag_name": "main"}):
            with self.subTest(change=change), patch.object(candidates, "_json", return_value={**metadata, **change}), \
                    patch.object(candidates, "_run") as run:
                with self.assertRaises(candidates.CandidateError):
                    candidates.plugin_release(COMPONENT)
                run.assert_not_called()

    def test_frozen_archive_digest_and_manifest_version_both_checked(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(candidates, "plugin_release", return_value={k: TARGET[k] for k in OLD}), \
                patch.object(candidates, "_download_archive", return_value=(Path(directory) / "plugin.tar.gz", "2"*64)), \
                patch.object(candidates, "_archive_json", return_value={"name": "ponytail", "version": "5.3.3"}) as manifest:
            with self.assertRaisesRegex(candidates.CandidateError, "sha256 changed"):
                candidates.resolve_plugin(COMPONENT, Path(directory), TARGET)
            manifest.return_value["version"] = "5.3.4"
            with self.assertRaisesRegex(candidates.CandidateError, "版本不一致"):
                candidates.resolve_plugin(COMPONENT, Path(directory))

    def test_archive_and_formal_update_cannot_change_existing_release_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "source.tar.gz"
            data = b'{"name":"ponytail","version":"5.3.3"}'
            with tarfile.open(archive, "w:gz", format=tarfile.PAX_FORMAT, pax_headers={"comment": FORMAL}) as package:
                f = tarfile.TarInfo("source/.codex-plugin/plugin.json")
                f.size = len(data)
                package.addfile(f, io.BytesIO(data))
            self.assertEqual(candidates._archive_json(archive, ".codex-plugin/plugin.json", git_commit=FORMAL)["name"], "ponytail")
            with self.assertRaisesRegex(candidates.CandidateError, "不证明目标提交"):
                candidates._archive_json(archive, ".codex-plugin/plugin.json", git_commit=DRIFT)
        for update in ({**TARGET, "ref": DRIFT}, {**TARGET, "sha256": "2"*64}, {**TARGET, "version": "5.3.2"}):
            with self.assertRaisesRegex(candidates.CandidateError, "降级或同名标签"):
                candidates.verify_plugin_upgrade(TARGET, update)


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="pennix-ponytail-test-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        if self._testMethodName == "test_native_both_legacy_payloads_and_forward_or_rollback":
            self.resolved = candidates.resolve_plugin(COMPONENT, self.root, {"version": "5.3.3", "ref": FORMAL})
        self.home = self.root / "codex"
        self.home.mkdir()
        environment = patch.dict(os.environ, {"HOME": str(self.root), "CODEX_HOME": str(self.home),
                                                "XDG_STATE_HOME": str(self.root / "state")})
        environment.start()
        self.addCleanup(environment.stop)
        self.args = argparse.Namespace(
            codex_home=self.home, config_path=self.root / "workflow.config",
            component_lock_path=self.root / "components.lock.json",
            component_lock_sources={ponytail.KEY: COMPONENT["lock_source"]},
            version=None, confirm=None, json=True)
        self.args.config_path.write_bytes(b"isolated config\n")
        self.args.config_snapshot = self.args.config_path.read_bytes()

    def set_legacy_records(self):
        record = {"schema": 1, "component": ponytail.KEY, "id": COMPONENT["plugin"]["id"],
                  "target": owned_files.target_identity(), **OLD}
        owned_files.replace(ponytail.receipt_path(), owned_files.json_bytes(record), None)
        self.args.component_lock = {"schema": 1, "components": {ponytail.KEY: OLD_LOCK}}
        self.args.component_lock_snapshot = owned_files.json_bytes(self.args.component_lock)
        owned_files.replace(self.args.component_lock_path, self.args.component_lock_snapshot, None)
        return record

    def sync_args(self):
        self.args.component_lock_snapshot = self.args.component_lock_path.read_bytes()
        self.args.component_lock = json.loads(self.args.component_lock_snapshot)

    def test_preview_rejects_changed_payload_and_routes_recovery_after_lock_write(self):
        self.set_legacy_records()
        before = {"market": {"kind": "legacy", "ref": OUTER}, "file": None,
                  "payload": {"version": "5.3.3", "ref": DRIFT}}
        with patch.object(ponytail, "Native") as instance, patch.object(ponytail, "prove_retrievable"), \
                patch.object(candidates, "resolve", return_value={**TARGET, "tag": "v5.3.3"}), \
                patch.object(pennix, "_emit") as emit:
            instance.return_value.observe.return_value = before
            self.assertEqual(pennix._ponytail_update(self.args, COMPONENT), 0)
            self.args.confirm = emit.call_args.args[0]["review_sha256"]
            instance.return_value.observe.return_value = {**before, "payload": {"version": "5.3.3", "ref": OUTER}}
            with self.assertRaisesRegex(pennix.CliError, "摘要不匹配"):
                pennix._ponytail_update(self.args, COMPONENT)
            self.assertIsNone(apply_journal.read())
            instance.return_value.observe.return_value = before
            with patch.object(ponytail, "move"), patch.object(apply_journal, "checkpoint_lock",
                                                            side_effect=OSError("injected after lock write")):
                with self.assertRaisesRegex(OSError, "injected"):
                    pennix._ponytail_update(self.args, COMPONENT)
            self.sync_args()
            active = apply_journal.read()
            self.assertEqual(active["steps"][0]["status"], "unknown")
            self.assertEqual(self.args.component_lock["components"][ponytail.KEY], TARGET)
            self.assertNotEqual(apply_journal.digest(self.args.component_lock_path), active["lock_digest"])
            self.args.resolve, self.args.transaction, self.args.step = "completed", active["transaction"], active["steps"][0]["id"]
            instance.return_value.observe.return_value = {"market": {"kind": "local", "ref": FORMAL}, "file": FORMAL,
                                                          "payload": {"version": "5.3.3", "ref": FORMAL}}
            with patch.object(ponytail, "move") as move:
                self.assertEqual(pennix._ponytail_recover(self.args, COMPONENT, active, active["steps"][0]), 0)
                self.assertEqual(move.call_args.args[1:], (before, TARGET))
            self.assertIsNone(apply_journal.read())
            self.assertEqual(ponytail.receipt_entry(ponytail.receipt(COMPONENT)[0]), TARGET)

    def test_unregistered_cached_plugin_is_not_treated_as_first_install(self):
        native = ponytail.Native(COMPONENT, self.home)
        cache = self.home / "plugins/cache/ponytail/ponytail/5.3.3"
        cache.mkdir(parents=True)
        with patch.object(native, "call", side_effect=[{"marketplaces": []}, {"installed": [], "available": []}]):
            native.codex = "codex"
            self.assertFalse(native.empty(native.observe()))
        (self.home / "config.toml").write_text('[plugins."ponytail@ponytail"]\nenabled=false\n')
        with patch.object(native, "call") as call, self.assertRaisesRegex(ponytail.PonytailError, "用户禁用"):
            native.observe()
        call.assert_not_called()

    @unittest.skipUnless(os.environ.get("PENNIX_PONYTAIL_NATIVE") == "1", "需要显式允许隔离 Codex 和公开 Git 访问")
    def test_native_both_legacy_payloads_and_forward_or_rollback(self):
        native = ponytail.Native(COMPONENT, self.home)
        self.assertTrue(native.empty(native.observe()))
        desired = candidates.lock_entry(self.resolved)
        ponytail.operate(COMPONENT, self.home, "install", self.resolved)
        self.assertTrue(native.matches(native.observe(), desired))
        self.assertEqual(ponytail.status({**COMPONENT, "_lock_target": desired}, self.home)[0], "match")
        ponytail.operate(COMPONENT, self.home, "uninstall", None)
        self.assertTrue(native.empty(native.observe()))
        record = self.set_legacy_records()
        for old, resolution in ((OUTER, "restored"), (DRIFT, "restored"), (DRIFT, "completed")):
            with self.subTest(old=old, resolution=resolution):
                native.write(old, None)
                native.call("marketplace", "add", str(native.root))
                native.install({"ref": old, "version": "5.3.3"})
                native.call("marketplace", "remove", native.market)
                native.write(None, old)
                native.call("marketplace", "add", native.url, "--ref", OUTER)
                before = native.observe()
                self.assertEqual(before["payload"]["ref"], old)
                self.assertEqual(ponytail.status({**COMPONENT, "_lock_target": OLD_LOCK}, self.home)[0], "plugin-migration-required")
                proposed = {"schema": 1, "components": {ponytail.KEY: desired}}
                target = {"before": before, "previous": OLD_LOCK, "receipt_before": record, "desired": desired,
                          "lock_before": apply_journal.digest(self.args.component_lock_path),
                          "lock_after": hashlib.sha256(owned_files.json_bytes(proposed)).hexdigest()}
                step = {"id": f"{ponytail.KEY}:release", "component": ponytail.KEY, "action": "pin-release", "target": target}
                active = pennix._start_transaction(self.args, "update", ponytail.KEY, [step])
                active = apply_journal.started(active, step["id"])
                if resolution == "restored":
                    # Fail after Codex replaced the actual payload, but before receipt and lock commit.
                    ponytail.move(native, before, desired)
                    self.assertTrue(native.matches(native.observe(), desired))
                else:
                    # A native list now names the desired ref, while the checkout is still old.
                    native.call("marketplace", "remove", native.market)
                    native.write(desired["ref"], None)
                    native.call("marketplace", "add", str(native.root))
                    self.assertFalse(native.matches(native.observe(), desired))
                self.args.resolve, self.args.transaction, self.args.step = resolution, active["transaction"], step["id"]
                with patch.object(pennix, "_emit"):
                    pennix._ponytail_recover(self.args, COMPONENT, active, active["steps"][0])
                self.assertIsNone(apply_journal.read())
                self.sync_args()
                if resolution == "restored":
                    self.assertEqual(native.observe(), before)
                    self.assertEqual(ponytail.receipt(COMPONENT)[0], record)
                    self.assertEqual(self.args.component_lock["components"][ponytail.KEY], OLD_LOCK)
                    native.call("remove", native.plugin)
                    native.call("marketplace", "remove", native.market)
                else:
                    self.assertTrue(native.matches(native.observe(), desired))
                    self.assertEqual(self.args.component_lock["components"][ponytail.KEY], desired)
                    self.assertEqual(ponytail.receipt_entry(ponytail.receipt(COMPONENT)[0]), desired)
