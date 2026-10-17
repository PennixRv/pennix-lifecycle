from __future__ import annotations

import json
import stat
import tempfile
import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import credential_import
import secret_store


class SecretStoreTests(unittest.TestCase):
    def test_update_is_private_and_conflicts_are_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config" / "pennix" / "secrets.toml"
            self.assertEqual(secret_store.update(path, {"codex.api_key": "key-a"}), ["codex.api_key"])
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            with self.assertRaises(secret_store.SecretError):
                secret_store.update(path, {"codex.api_key": "key-b"})
            self.assertEqual(secret_store.get("codex.api_key", path), "key-a")

    def test_broken_symlink_is_not_treated_as_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "secrets.toml"
            path.symlink_to(Path(directory) / "missing-secrets.toml")
            with self.assertRaises(secret_store.SecretError):
                secret_store.update(path, {"codex.api_key": "key-a"})

    def test_import_reads_only_fixed_sources_and_writes_once(self) -> None:
        fixtures = {
            ".codex/auth.json": json.dumps({"OPENAI_API_KEY": "codex-key"}),
            ".config/pennix-siyuan/connection.json": json.dumps({"api_token": "siyuan-token"}),
            ".config/cch-codex-tmux-status/cch-admin-token": "cch-token\n",
            ".config/tavily-hikari-cli/config.json": json.dumps({"token": "hikari-token"}),
            ".config/grok-search/config.json": json.dumps({"apiKey": "grok-key", "tavilyApiKey": "tavily-key"}),
        }

        def fetch(_alias: str, remote: str, destination: Path) -> None:
            destination.write_text(fixtures[remote], encoding="utf-8")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "secrets.toml"
            fields = sorted(credential_import.SOURCES)
            changed = credential_import.import_fields("office", fields, path, fetch=fetch)
            self.assertEqual(changed, fields)
            self.assertEqual(secret_store.load(path), {
                "codex.api_key": "codex-key",
                "siyuan.api_token": "siyuan-token",
                "cch.admin_token": "cch-token",
                "hikari.api_token": "hikari-token",
                "grok.api_key": "grok-key",
                "grok.tavily_api_key": "tavily-key",
            })

    def test_import_rejects_untrusted_alias_and_unknown_source(self) -> None:
        with self.assertRaises(credential_import.CredentialImportError):
            credential_import.import_fields("office;rm", ["codex.api_key"])
        with self.assertRaises(credential_import.CredentialImportError):
            credential_import.import_fields("office", ["windsurf.api_key"], fetch=lambda *_: None)


if __name__ == "__main__":
    unittest.main()
