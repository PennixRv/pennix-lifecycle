import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import apply_journal


class ApplyJournalTests(unittest.TestCase):
    def test_steps_are_durable_and_unknown_state_is_not_complete(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
                "HOME": directory, "XDG_STATE_HOME": str(Path(directory) / "state")}):
            value = apply_journal.start("apply", None, "a" * 64, None, [
                {"id": "context-mode:install", "component": "context-mode", "action": "install", "target": {}}
            ])
            value = apply_journal.set_target(value, "context-mode:install", {
                "source": "npm:@pennixrv/context-mode:latest", "version": "1.0.0", "sha256": "b" * 64})
            value = apply_journal.started(value, "context-mode:install")
            self.assertEqual(apply_journal.read()["steps"][0]["status"], "started")
            value = apply_journal.unknown(value, "context-mode:install")
            self.assertFalse(apply_journal.complete(value))
            self.assertEqual(apply_journal.read()["steps"][0]["status"], "unknown")
            value = apply_journal.retry(value, "context-mode:install")
            self.assertEqual(apply_journal.read()["steps"][0]["status"], "pending")
            with self.assertRaises(apply_journal.JournalError):
                apply_journal.clear()
            apply_journal.verified(value, "context-mode:install")
            apply_journal.clear()
            self.assertIsNone(apply_journal.read())

    def test_retry_requires_unknown_status(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(apply_journal.owned_files, "state_root", return_value=Path(directory)):
            value = apply_journal.start("update", "pennix-skills", "a" * 64, "b" * 64, [
                {"id": "pennix-skills:upgrade", "component": "pennix-skills", "action": "upgrade", "target": {"ref": "c" * 40}},
            ])
            with self.assertRaises(apply_journal.JournalError):
                apply_journal.retry(value, "pennix-skills:upgrade")
            apply_journal.verified(value, "pennix-skills:upgrade")
            apply_journal.clear()

    def test_paseo_config_receipt_survives_later_service_failure(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
                apply_journal.owned_files, "state_root", return_value=Path(directory)):
            value = apply_journal.start("apply", None, "a" * 64, None, [
                {"id": "paseo-config:configure", "component": "paseo-config",
                 "action": "configure", "target": {"listen": "127.0.0.1:6767"}},
                {"id": "paseo-autostart:start", "component": "paseo-autostart",
                 "action": "service", "target": {}},
            ])
            value = apply_journal.verified(value, "paseo-config:configure", {"state": "current"})
            value = apply_journal.started(value, "paseo-autostart:start")
            value = apply_journal.unknown(value, "paseo-autostart:start")
            self.assertEqual([step["status"] for step in value["steps"]], ["verified", "unknown"])
            self.assertFalse(apply_journal.complete(value))
            with self.assertRaises(apply_journal.JournalError):
                apply_journal.clear()
            self.assertIsNotNone(apply_journal.read())


if __name__ == "__main__":
    unittest.main()
