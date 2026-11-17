from __future__ import annotations

import json
import stat
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import credential_import
import secret_store
import pennix
import config_model
import os
import io
import shlex
from types import SimpleNamespace
from contextlib import redirect_stdout


class SecretStoreTests(unittest.TestCase):
    def test_owner_import_uses_sftp_login_home_without_tilde_extension(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            source = home / '.config/windsurf-code-search/config.json'
            source.parent.mkdir(parents=True)
            source.write_bytes(b'{"apiKey":"synthetic-private-value"}')
            def sftp(command, **kwargs):
                self.assertEqual(command[0], 'sftp')
                self.assertIn('-oStrictHostKeyChecking=yes', command)
                operation, remote, destination = shlex.split(kwargs['input'])
                self.assertEqual(operation, 'get')
                path = home / remote
                if not path.is_file():
                    return SimpleNamespace(returncode=1)
                Path(destination).write_bytes(path.read_bytes())
                return SimpleNamespace(returncode=0)
            with patch.object(credential_import.subprocess, 'run', side_effect=sftp):
                self.assertEqual(credential_import._fetch_remote('trusted-home', '.config/windsurf-code-search/config.json'), source.read_bytes())

    def test_central_transfer_is_selective_and_rejects_bad_envelopes(self):
        good = {"schema": 1, "fields": {"codex": {"api_key": "private-fixture"}}}
        self.assertEqual(credential_import.read_from_lifecycle("home", ["codex.api_key"],
                         fetch=lambda *_: json.dumps(good).encode()), good["fields"])
        bad = [b'{"schema":1,"schema":1,"fields":{}}', b'[]', b'private-fixture',
               json.dumps({**good, "fields": {"codex": {"api_key": "private-fixture", "unknown": "extra"}}}).encode(),
               json.dumps({"schema": 1, "fields": {}}).encode(), b'x' * (credential_import.LIMIT + 1)]
        for raw in bad:
            with self.subTest(size=len(raw)), self.assertRaises(credential_import.CredentialImportError) as failure:
                credential_import.read_from_lifecycle("home", ["codex.api_key"], fetch=lambda *_: raw)
            self.assertNotIn("private-fixture", str(failure.exception))
        with self.assertRaises(credential_import.CredentialImportError):
            credential_import.read_from_lifecycle("-oProxyCommand=bad", ["codex.api_key"])

    def test_selected_requirements_check_and_config_change_are_redacted(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"HOME": directory,
                "XDG_CONFIG_HOME": directory + '/config', "XDG_STATE_HOME": directory + '/state'}):
            cfg = config_model.init()
            kconf, _ = config_model.load()
            for name, value in {"PENNIX_COMPONENT_PENNIX_SKILLS": "y", "PENNIX_COMPONENT_CODEX_CONFIG": "y",
                                "PENNIX_TARGET_SIYUAN": "y"}.items():
                kconf.syms[name].set_value(value)
            config_model.write(kconf, cfg)
            kconf, _ = config_model.load()
            path = Path(config_model.config_value(kconf, 'PENNIX_SECRETS_FILE'))
            secret_store.update(path, {"codex": {"api_key": "private-fixture"}})
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(pennix.main(['secrets', 'check', '--json']), 2)
            self.assertIn('siyuan.api_token', output.getvalue())
            self.assertNotIn('private-fixture', output.getvalue())
            before = path.read_bytes()
            def changed(*_):
                cfg.write_bytes(cfg.read_bytes() + b'\n# changed\n')
                return {"siyuan": {"api_token": "new"}}
            with patch.object(credential_import, 'read_from_lifecycle', side_effect=changed):
                args = pennix.parser().parse_args(['secrets', 'import-ssh', '--alias', 'home', '--field', 'siyuan.api_token'])
                with self.assertRaises(pennix.CliError):
                    pennix.cmd_secrets_import_ssh(args)
            self.assertEqual(path.read_bytes(), before)

    def test_partial_source_failure_preserves_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'secrets.toml'
            secret_store.update(path, {"codex": {"api_key": "keep"}})
            before = path.read_bytes()
            def fetch(_alias, source):
                if source == '.codex/auth.json':
                    return b'{"OPENAI_API_KEY":"keep"}'
                raise credential_import.CredentialImportError('unavailable')
            with self.assertRaises(credential_import.CredentialImportError):
                credential_import.import_from_ssh(path, 'home', ['codex', 'siyuan'], fetch=fetch)
            self.assertEqual(path.read_bytes(), before)

    def test_preserved_codex_native_auth_is_a_central_requirement(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'HOME':directory,
                'XDG_CONFIG_HOME':directory+'/config'}):
            cfg = config_model.init()
            kconf, _ = config_model.load()
            kconf.syms['PENNIX_COMPONENT_CODEX_CONFIG'].set_value('y')
            kconf.syms['PENNIX_CODEX_PROVIDER_PRESERVE'].set_value('y')
            home = Path(config_model.config_value(kconf, 'PENNIX_CODEX_HOME'))
            home.mkdir()
            path = config_model.config_value(kconf, 'PENNIX_SECRETS_FILE')
            (home/'config.toml').write_text('model_provider="existing"\n[model_providers.existing.auth]\ncommand='+
                json.dumps(str(Path(directory)/'.local/bin/pennix'))+'\nargs='+
                json.dumps(['secrets','codex-token','--secrets-file',path])+'\n')
            self.assertIn('codex.api_key', credential_import.required_fields(kconf, {'configuration_targets':{}}))

    def test_private_export_rejects_terminal_and_timeout_kills_ssh_group(self):
        args = pennix.parser().parse_args(['secrets','export','--field','codex.api_key','--private-stdout'])
        with patch.object(sys.stdout, 'isatty', return_value=True), self.assertRaises(pennix.CliError):
            pennix.cmd_secrets_export(args)
        real_popen = credential_import.subprocess.Popen
        children = []
        def process(*_, **kwargs):
            child = real_popen([sys.executable,'-c','import time;time.sleep(30)'], **kwargs)
            children.append(child)
            return child
        with patch.object(credential_import.subprocess,'Popen',side_effect=process), \
                patch.object(credential_import.time,'monotonic',side_effect=[0,61]), \
                self.assertRaises(credential_import.CredentialImportError):
            credential_import._fetch_lifecycle('home',['codex.api_key'])
        self.assertIsNotNone(children[0].poll())

    def test_first_secret_write_rejects_non_private_existing_parent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory) / "shared"
            parent.mkdir(mode=0o755)
            parent.chmod(0o755)
            path = parent / "secrets.toml"
            with self.assertRaises(secret_store.SecretError):
                secret_store.update(path, {"codex": {"api_key": "private-test-value"}})
            self.assertFalse(path.exists())
            self.assertEqual(stat.S_IMODE(parent.stat().st_mode), 0o755)

    def test_non_utf8_import_is_redacted_and_does_not_create_secret_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "secrets.toml"
            with self.assertRaises(credential_import.CredentialImportError) as failure:
                credential_import.import_from_ssh(path, "fixture", ["cch"],
                    fetch=lambda *_args: b"private-test-value\xff")
            self.assertNotIn("private-test-value", str(failure.exception))
            self.assertFalse(path.exists())

    def test_update_is_private_and_conflicts_are_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config" / "pennix" / "secrets.toml"
            self.assertEqual(secret_store.update(path, {"codex": {"api_key": "key-a"}}), {"codex.api_key"})
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            with self.assertRaises(secret_store.SecretError):
                secret_store.update(path, {"codex": {"api_key": "key-b"}})
            self.assertEqual(secret_store.get(path, "codex", "api_key"), "key-a")

    def test_retired_secrets_are_read_only_and_unknown_fields_still_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "secrets.toml"
            original = b'[grok]\napi_key="old"\ntavily_api_key="old-tavily"\n[hikari]\napi_token="old-hikari"\n'
            path.write_bytes(original)
            path.chmod(0o600)
            self.assertEqual(secret_store.get(path, "grok", "api_key"), "old")
            for service, field in [("grok", "api_key"), ("hikari", "api_token")]:
                with self.assertRaises(secret_store.SecretError):
                    secret_store.update(path, {service: {field: "new"}})
                with self.assertRaises(secret_store.SecretError):
                    secret_store.field_names([f"{service}.{field}"])
                with self.assertRaises(credential_import.CredentialImportError):
                    credential_import.read_from_ssh("home", [service], fetch=lambda *_: b"{}")
            self.assertEqual(path.read_bytes(), original)
            secret_store.update(path, {"codex": {"api_key": "active"}})
            self.assertEqual(secret_store.get(path, "hikari", "api_token"), "old-hikari")
            path.write_bytes(path.read_bytes() + b'[unknown]\nkey="reject"\n')
            with self.assertRaises(secret_store.SecretError):
                secret_store.load(path)

    def test_broken_symlink_is_not_treated_as_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "secrets.toml"
            path.symlink_to(Path(directory) / "missing-secrets.toml")
            with self.assertRaises(secret_store.SecretError):
                secret_store.update(path, {"codex": {"api_key": "key-a"}})

    def test_import_reads_only_fixed_sources_and_writes_once(self) -> None:
        fixtures = {
            ".codex/auth.json": json.dumps({"OPENAI_API_KEY": "codex-key"}),
            ".config/pennix-siyuan/connection.json": json.dumps({"api_token": "siyuan-token"}),
            ".config/cch-codex-tmux-status/cch-admin-token": "cch-token\n",
            ".config/windsurf-code-search/config.json": json.dumps({"apiKey": "windsurf-key"}),
        }

        def fetch(alias: str, remote: str) -> bytes:
            self.assertEqual(alias, "office")
            return fixtures[remote].encode()

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "secrets.toml"
            fields = sorted(credential_import.SOURCES)
            changed = credential_import.import_from_ssh(path, "office", fields, fetch=fetch)
            self.assertEqual(changed, {
                "codex.api_key",
                "siyuan.api_token",
                "cch.admin_token",
                "windsurf.api_key",
            })

    def test_import_rejects_untrusted_alias_and_unknown_source(self) -> None:
        with self.assertRaises(credential_import.CredentialImportError):
            credential_import.import_from_ssh(Path("/tmp/secrets.toml"), "office;rm", ["codex"])
        with self.assertRaises(credential_import.CredentialImportError):
            credential_import.import_from_ssh(Path("/tmp/secrets.toml"), "office", ["windsurf"], fetch=lambda *_: b"")


if __name__ == "__main__":
    unittest.main()
