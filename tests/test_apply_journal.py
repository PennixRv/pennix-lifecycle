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
            apply_journal.clear()
            self.assertIsNone(apply_journal.read())


if __name__ == "__main__":
    unittest.main()
