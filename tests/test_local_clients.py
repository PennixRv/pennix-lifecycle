import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import hook_integrations
import local_clients
import owned_files


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
        self.component["client"]["source"] = {
            "repo": "example/client", "path": "client", "archive": False,
            "url": "https://example.test/client",
            "sha256": hashlib.sha256(self.content).hexdigest(),
        }
        with patch.object(local_clients.files, "download", side_effect=lambda url, path: path.write_bytes(self.content)):
            local_clients.operate("client", self.component, "install", adopt=True)
        replacement = self.home / "replacement"
        replacement.write_bytes(b"#!/bin/sh\nexit 1\n")
        candidate = {"repo": "example/client", "ref": "a" * 40,
                     "sha256": hashlib.sha256(replacement.read_bytes()).hexdigest(),
                     "path": str(replacement)}
        self.assertEqual(local_clients.operate("client", self.component, "upgrade", candidate=candidate),
                         "changed; preserved adjacent user data")
        self.assertEqual((self.home / ".client/client").read_bytes(), replacement.read_bytes())

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

    def test_hikari_staging_generates_wrapper_without_running_upstream_installer(self):
        config = self.home / ".config/tavily-hikari-cli"
        config.mkdir(parents=True)
        sentinel = config / "config.json"
        sentinel.write_text("user data")
        component = {"id": "tavily-hikari", "client": {
            "root": "$HOME/.local/lib/pennix-clients/tavily-hikari",
            "source": {"url": "https://example.test/tvly-hikari", "archive": False,
                       "sha256": hashlib.sha256(self.content).hexdigest()},
            "paths": ["tvly-hikari"], "commands": {"tvly-hikari": "tvly-hikari"},
        }}
        workspace = self.home / "staging"
        workspace.mkdir()
        with patch.object(local_clients.files, "download", side_effect=lambda url, path: path.write_bytes(self.content)) as download:
            with patch.object(local_clients.shutil, "which", return_value="/usr/bin/tvly"):
                with patch.object(local_clients.subprocess, "run", return_value=type("Result", (), {"returncode": 0, "stdout": "tvly 0.1.8"})()) as run:
                    staged = local_clients.stage(component, workspace)
        self.assertEqual([call.args[0] for call in download.call_args_list], ["https://example.test/tvly-hikari"])
        self.assertEqual(run.call_args.args[0], ["tvly", "--version"])
        self.assertEqual(staged[".tvly-hikari-real"].read_bytes(), self.content)
        self.assertIn(str(component["client"]["root"]).replace("$HOME", str(self.home)),
                      staged["tvly-hikari"].read_text())
        self.assertEqual(sentinel.read_text(), "user data")

    def test_hooks_adopt_only_exact_entries_and_keep_other_owners(self):
        home = self.home / ".codex"
        home.mkdir()
        document = {"hooks": {"SessionStart": [{"hooks": [{"command": "other owner", "type": "command"}]}]}}
        document["hooks"]["SessionStart"] += hook_integrations.desired("aoe")["SessionStart"]
        (home / "hooks.json").write_text(json.dumps(document))
        self.assertEqual(hook_integrations.inspect("aoe", home)["state"], "external")
        with self.assertRaises(hook_integrations.HookError):
            hook_integrations.apply("aoe", home)
        hook_integrations.apply("aoe", home, adopt=True)
        hook_integrations.apply("peon-ping", home)
        hook_integrations.apply("aoe", home, remove=True)
        value = json.loads((home / "hooks.json").read_text())
        self.assertEqual(value["hooks"]["SessionStart"], [{"hooks": [{"command": "other owner", "type": "command"}]}])
        self.assertEqual(hook_integrations.inspect("peon-ping", home)["state"], "current")
