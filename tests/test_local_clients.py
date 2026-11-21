import hashlib
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import apply_journal
import candidates
import component_lock
import config_model
import hook_integrations
import local_clients
import owned_files
import pennix


class LocalClientTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"HOME": str(self.home), "XDG_STATE_HOME": str(self.home / "state")})
        self.env.start()
        self.content = b"#!/bin/sh\nexit 0\n"
        self.component = {"client": {"root": "$HOME/.client", "source": {"url": "https://example.test/client", "archive": False,
                          "sha256": hashlib.sha256(self.content).hexdigest()}, "paths": ["client"], "commands": {"client": "client"}}}

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_install_repeat_uninstall_and_receipt_failure_preserve_adjacent_data(self):
        target = self.home / ".client"
        target.mkdir()
        (target / "user-config.json").write_text("user data")
        with patch.object(local_clients.files, "download", side_effect=lambda url, path: path.write_bytes(self.content)):
            local_clients.operate("client", self.component, "install", adopt=True)
            self.assertEqual(local_clients.inspect("client", self.component)[0], "match")
            self.assertEqual(local_clients.operate("client", self.component, "install"), "no-op")
            local_clients.operate("client", self.component, "uninstall")
            self.assertEqual((target / "user-config.json").read_text(), "user data")
            self.assertFalse((self.home / ".local/bin/client").exists())
            original = owned_files.replace

            def fail_receipt(path, *args, **kwargs):
                if path == local_clients.receipt_path("client"):
                    raise OSError("disk full")
                return original(path, *args, **kwargs)

            with patch.object(local_clients.files, "replace", side_effect=fail_receipt):
                with self.assertRaises(OSError):
                    local_clients.operate("client", self.component, "install", adopt=True)
            self.assertFalse((target / "client").exists())
            self.assertFalse((self.home / ".local/bin/client").exists())
            self.assertFalse(local_clients.receipt_path("client").exists())

    def test_adoption_and_fixed_hash_cannot_overwrite_unknown_content(self):
        target = self.home / ".client"
        target.mkdir()
        (target / "client").write_bytes(b"unknown user binary")
        with patch.object(local_clients.files, "download", side_effect=lambda url, path: path.write_bytes(self.content)):
            with self.assertRaises(local_clients.ClientError):
                local_clients.operate("client", self.component, "install", adopt=True)
        self.assertEqual((target / "client").read_bytes(), b"unknown user binary")
        (target / "client").unlink()
        with patch.object(local_clients.files, "download", side_effect=lambda url, path: path.write_bytes(b"wrong hash")):
            with self.assertRaises(local_clients.ClientError):
                local_clients.operate("client", self.component, "install", adopt=True)
        self.assertFalse((target / "client").exists())

    def test_reviewed_command_migration_preserves_unknown_adjacent_data(self):
        command = self.home / ".local/bin/client"
        command.parent.mkdir(parents=True)
        command.write_bytes(b"existing wrapper")
        command.chmod(0o700)
        self.assertEqual(local_clients.inspect("client", self.component)[0], "external")
        reviewed = local_clients.fingerprint(self.component)
        with patch.object(local_clients.files, "download", side_effect=lambda url, path: path.write_bytes(self.content)):
            local_clients.operate("client", self.component, "upgrade", expected_sha256=reviewed)
        self.assertTrue(command.is_symlink())
        self.assertEqual(local_clients.inspect("client", self.component)[0], "match")

    def test_explicit_candidate_replaces_a_matching_client(self):
        self.component["id"] = "client"
        self.component["lock_source"] = "git:example/client:client"
        self.component["client"]["source"] = {
            "repo": "example/client", "path": "client", "archive": False,
        }
        original = self.home / "original"
        original.write_bytes(self.content)
        local_clients.operate("client", self.component, "install", candidate={
            "repo": "example/client", "ref": "b" * 40, "path": str(original),
            "sha256": hashlib.sha256(self.content).hexdigest(),
        })
        self.component["_lock_target"] = local_clients.lock_migration("client", self.component)
        self.assertEqual(self.component["_lock_target"]["ref"], "b" * 40)
        self.assertEqual(local_clients.inspect("client", self.component)[0], "match")
        replacement = self.home / "replacement"
        replacement.write_bytes(b"#!/bin/sh\nexit 1\n")
        candidate = {"repo": "example/client", "ref": "a" * 40,
                     "sha256": hashlib.sha256(replacement.read_bytes()).hexdigest(),
                     "path": str(replacement)}
        self.component["_candidate_target"] = candidate
        adjacent = self.home / ".client/user-config.json"
        adjacent.write_text("user data")
        self.assertEqual(local_clients.operate("client", self.component, "upgrade", candidate=candidate),
                         "changed; preserved adjacent user data")
        self.assertEqual((self.home / ".client/client").read_bytes(), replacement.read_bytes())
        self.assertEqual(adjacent.read_text(), "user data")
        self.assertEqual(local_clients.lock_migration("client", self.component)["ref"], "a" * 40)

    def test_legacy_url_receipt_migrates_to_a_matching_ref_lock(self):
        component = {"id": "client", "lock_source": "git:example/client:client",
                     "client": {"root": "$HOME/.client", "source": {"repo": "example/client",
                                 "path": "client", "archive": False}, "paths": ["client"],
                                "commands": {"client": "client"}}}
        target = self.home / ".client"
        target.mkdir()
        asset = target / "client"
        asset.write_bytes(self.content)
        command = self.home / ".local/bin/client"
        command.parent.mkdir(parents=True)
        command.symlink_to(asset)
        ref = "a" * 40
        digest = hashlib.sha256(self.content).hexdigest()
        record = {"schema": 1, "root": str(target),
                  "source": {"url": f"https://raw.githubusercontent.com/example/client/{ref}/client",
                             "archive": False, "sha256": digest},
                  "files": {"client": {"digest": digest, "mode": asset.stat().st_mode & 0o777}},
                  "commands": {"client": "client"}}
        owned_files.replace(local_clients.receipt_path("client"), owned_files.json_bytes(record), None)

        component["_lock_target"] = local_clients.lock_migration("client", component)
        self.assertEqual(component["_lock_target"], {"source": component["lock_source"], "ref": ref, "sha256": digest})
        self.assertEqual(local_clients.inspect("client", component)[0], "match")

    def test_recover_frozen_client_upgrade_and_uninstall_native_hook(self):
        key, step = "client", "client:upgrade"
        self.component.update(id=key, delivery="client", lock_source="git:example/client:client",
                              native_config={"owner": "aoe", "select": "PENNIX_AOE_HOOK"})
        self.component["client"]["source"] = {"repo": "example/client", "path": "client", "archive": False}
        codex_home = self.home / ".codex"
        codex_home.mkdir()
        hook_integrations.apply("aoe", codex_home)
        config = config_model.init(str(self.home / "workflow.config"))
        lock_path = component_lock.path_for(config)
        sources = {key: self.component["lock_source"]}
        original = self.home / "original"
        replacement = self.home / "replacement"
        original.write_bytes(self.content)
        newer = b"#!/bin/sh\nexit 1\n"
        replacement.write_bytes(newer)
        previous = {"source": sources[key], "ref": "a" * 40, "sha256": hashlib.sha256(self.content).hexdigest()}
        target = {"source": sources[key], "ref": "b" * 40, "sha256": hashlib.sha256(newer).hexdigest()}
        old_candidate = {**previous, "path": str(original), "repo": "example/client"}
        new_candidate = {**target, "path": str(replacement), "repo": "example/client"}
        local_clients.operate(key, self.component, "install", candidate=old_candidate)
        installed = self.home / ".client/client"
        adjacent = installed.with_name("user-config.json")
        adjacent.write_text("user data")
        component_lock.write(lock_path, {"schema": 1, "components": {key: previous}}, sources)
        with patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}):
            journal = apply_journal.start("update", key, apply_journal.digest(config), apply_journal.digest(lock_path),
                [{"id": step, "component": key, "action": "upgrade", "target": target}])
        released_inspect = local_clients.inspect

        def inspect_against_old_lock(name, component):
            return released_inspect(name, {**component, "_lock_target": previous})

        with patch.object(local_clients, "inspect", side_effect=inspect_against_old_lock):
            with self.assertRaisesRegex(local_clients.ClientError, "写入后核验失败"):
                pennix._journal_mutate(journal, step,
                    lambda: local_clients.operate(key, self.component, "upgrade", candidate=new_candidate))
        self.assertEqual(installed.read_bytes(), self.content)
        self.assertEqual(apply_journal.step(apply_journal.read(), step)["status"], "unknown")

        argv = ["recover", "--config", str(config), "--transaction", journal["transaction"], "--step", step,
                "--resolve", "completed", "--json"]
        catalog = {"components": {key: self.component}, "configuration_targets": {}}
        with patch.object(pennix.owner_engine, "load_catalog", return_value=catalog), \
                patch.object(candidates, "RESOLVERS", {key: candidates.resolve_client}), \
                patch.object(owned_files, "download") as download, redirect_stdout(io.StringIO()):
            config_before = config.read_bytes()
            config.write_bytes(config_before + b"\n")
            with self.assertRaisesRegex(pennix.CliError, "配置与原事务不同"):
                pennix.cmd_recover(pennix.parser().parse_args(argv))
            config.write_bytes(config_before)
            component_lock.write(lock_path, {"schema": 1, "components": {key: target}}, sources)
            with self.assertRaisesRegex(pennix.CliError, "锁文件与事务"):
                pennix.cmd_recover(pennix.parser().parse_args(argv))
            component_lock.write(lock_path, {"schema": 1, "components": {key: previous}}, sources)
            installed.write_bytes(b"drift")
            with self.assertRaisesRegex(pennix.CliError, "后置|未证明"):
                pennix.cmd_recover(pennix.parser().parse_args(argv))
            download.assert_not_called()
            installed.write_bytes(self.content)
            download.side_effect = lambda url, path: path.write_bytes(b"bad digest")
            with self.assertRaisesRegex(candidates.CandidateError, "sha256 changed"):
                pennix.cmd_recover(pennix.parser().parse_args(argv))
            self.assertEqual(installed.read_bytes(), self.content)
            download.side_effect = lambda url, path: path.write_bytes(newer)
            self.assertEqual(pennix.cmd_recover(pennix.parser().parse_args(argv)), 0)
            self.assertIn(f"/{target['ref']}/client", download.call_args.args[0])
            self.assertEqual(installed.read_bytes(), newer)
            self.assertEqual(component_lock.read(lock_path, sources)["components"][key], target)
            self.assertIsNone(apply_journal.read())

            args = pennix.parser().parse_args(["uninstall", key, "--config", str(config), "--confirm", key, "--json"])
            self.assertEqual(pennix.cmd_uninstall(args), 0)
        self.assertFalse(installed.exists())
        self.assertFalse(local_clients.receipt_path(key).exists())
        self.assertEqual(hook_integrations.inspect("aoe", codex_home)["state"], "absent")
        self.assertEqual(component_lock.read(lock_path, sources)["components"], {})
        self.assertEqual(adjacent.read_text(), "user data")

    def test_hooks_adopt_only_exact_entries_and_keep_other_owners(self):
        home = self.home / ".codex"
        home.mkdir()
        other = [{"hooks": [{"command": "other owner", "type": "command"}]}]
        document = {"hooks": {"SessionStart": list(other), "Stop": other}}
        document["hooks"]["SessionStart"] += hook_integrations.desired("aoe")["SessionStart"]
        (home / "hooks.json").write_text(json.dumps(document))
        self.assertEqual(hook_integrations.inspect("aoe", home)["state"], "external")
        with self.assertRaises(hook_integrations.HookError):
            hook_integrations.apply("aoe", home)
        hook_integrations.apply("aoe", home, adopt=True)
        hook_integrations.apply("aoe", home, remove=True)
        value = json.loads((home / "hooks.json").read_text())
        self.assertEqual(value["hooks"], {"SessionStart": other, "Stop": other})

    def test_removing_aoe_cannot_move_foreign_positional_trust(self):
        key, event = "aoe", "SessionStart"
        for owned_first in [True, False]:
            with self.subTest(owned_first=owned_first), tempfile.TemporaryDirectory(dir=self.home) as directory:
                home = Path(directory)
                owned = hook_integrations.desired(key)[event]
                external = {"hooks": [{"type": "command", "command": "foreign-handler"}]}
                entries = [*owned, external] if owned_first else [external, *owned]
                (home / 'hooks.json').write_text(json.dumps({'hooks': {event: entries}}))
                hook_integrations.apply(key, home, adopt=True)
                position = 1 if owned_first else 0
                trust = f"{home / 'hooks.json'}:session_start:{position}:0"
                (home / 'config.toml').write_text(f'[hooks.state.{json.dumps(trust)}]\ntrusted_hash = "sha256:{"a"*64}"\n')
                before = (home / 'hooks.json').read_bytes(), (home / 'config.toml').read_bytes()
                if owned_first:
                    with self.assertRaisesRegex(hook_integrations.HookError, 'trusted Hook'):
                        hook_integrations.apply(key, home, remove=True)
                    self.assertEqual(before, ((home / 'hooks.json').read_bytes(), (home / 'config.toml').read_bytes()))
                else:
                    hook_integrations.apply(key, home, remove=True)
                    self.assertEqual(json.loads((home / 'hooks.json').read_text())['hooks'][event], [external])
                    self.assertEqual((home / 'config.toml').read_bytes(), before[1])
                # Every fixture has a distinct home, but the same key's receipt is target-bound.
                hook_integrations._path(key).unlink(missing_ok=True)
