import hashlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import owned_files
import retire_web_search


class RetirementTests(unittest.TestCase):
    def test_preview_digest_guard_cleanup_and_repeat_preserve_central_secrets(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {
                "HOME": temporary, "XDG_CONFIG_HOME": temporary + "/config",
                "XDG_STATE_HOME": temporary + "/state"}):
            root = Path(temporary)
            config = root / "config/grok-search/config.json"
            config.parent.mkdir(parents=True, mode=0o700)
            config.write_bytes(b'{"defaultExtra":false}')
            config.chmod(0o600)
            central = root / "config/pennix/secrets.toml"
            central.parent.mkdir()
            central.write_bytes(b'[grok]\napi_key="keep"\n')
            preview = retire_web_search.cleanup()
            self.assertEqual(hashlib.sha256(config.read_bytes()).hexdigest(), preview["config_sha256"])
            with self.assertRaises(owned_files.OwnershipError):
                retire_web_search.cleanup(execute=True, expected_sha256="0" * 64)
            self.assertTrue(config.exists())
            retire_web_search.cleanup(execute=True, expected_sha256=preview["config_sha256"])
            self.assertFalse(config.parent.exists())
            self.assertEqual(b'[grok]\napi_key="keep"\n', central.read_bytes())
            self.assertEqual("cleaned", retire_web_search.cleanup(execute=True)["state"])

    def test_unknown_files_credentials_symlinks_and_pending_transaction_block(self):
        for fixture in ["receipt", "credentials", "symlink", "transaction"]:
            with self.subTest(fixture=fixture), tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {
                    "HOME": temporary, "XDG_CONFIG_HOME": temporary + "/config",
                    "XDG_STATE_HOME": temporary + "/state"}):
                root = Path(temporary)
                config = root / "config/grok-search/config.json"
                config.parent.mkdir(parents=True, mode=0o700)
                config.write_bytes(b'{"defaultExtra":false}')
                config.chmod(0o600)
                if fixture == "receipt":
                    (config.parent / ".pennix-lifecycle-grok.json").write_text("{}")
                if fixture == "credentials":
                    config.write_text('{"apiKey":"must-not-delete"}')
                if fixture == "symlink":
                    config.unlink()
                    config.symlink_to(root / "missing")
                with patch.object(retire_web_search.apply_journal, "read", return_value={} if fixture == "transaction" else None):
                    with self.assertRaises(owned_files.OwnershipError):
                        retire_web_search.cleanup(execute=True)
                self.assertTrue(config.exists() or config.is_symlink())
