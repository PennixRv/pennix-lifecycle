import copy
import json
import unittest
from unittest.mock import patch

import test_native_integrations as fixtures
import native_integrations as owner
import codegraph_integration as graph
import codex_hook_state


class CodeGraphHooksTests(unittest.TestCase):
    setUp = fixtures.NativeIntegrationTests.setUp
    tearDown = fixtures.NativeIntegrationTests.tearDown

    def native_install(self, command, home):
        (home / "config.toml").write_text('[mcp_servers.codegraph]\ncommand="codegraph"\nargs=["serve","--mcp"]\n')
        entry = {"hooks": [{"type": "command", "command": "codegraph prompt-hook --agent codex",
                            "timeout": 2, "additionalContextLimit": 0}]}
        (home / "hooks.json").write_text(json.dumps({"hooks": {event: [entry] for event in graph.EVENTS}}))

    def rows(self, command, home, cwd):
        document, _ = owner._hooks(home)
        state = owner._toml(home / "config.toml").get("hooks", {}).get("state", {})
        rows = []
        for event, native in graph.EVENTS.items():
            for index, group in enumerate(document.get("hooks", {}).get(event, [])):
                if graph.marked(group):
                    key = f"{home / 'hooks.json'}:{native}:{index}:0"
                    digest = "sha256:" + ("a" if event == "SessionStart" else "b") * 64
                    rows.append({"key": key, "sourcePath": str(home / "hooks.json"),
                                 "command": group["hooks"][0]["command"], "matcher": None,
                                 "currentHash": digest, "enabled": True,
                                 "trustStatus": "trusted" if state.get(key) == {"trusted_hash": digest} else "untrusted"})
        return [{"hooks": rows}]

    def run_configure(self):
        return patch.object(owner, "_run", side_effect=self.native_install)

    def test_schema_one_migration_idempotence_and_uninstall_preserve_foreign_slots(self):
        owner.apply("codegraph", self.codex)
        baseline = owner._read_receipt("codegraph")["baseline"]
        foreign = {"hooks": [{"type": "command", "command": "foreign"}]}
        (self.codex / "hooks.json").write_text(json.dumps({"extra": True, "hooks": {"SessionStart": [foreign]}}))
        with self.run_configure(), patch.object(codex_hook_state, "list_hooks", side_effect=self.rows):
            self.assertEqual(owner.inspect("codegraph", self.codex, enabled=True)["state"], "update-required")
            self.assertEqual(owner.apply("codegraph", self.codex), "changed")
            receipt = owner._read_receipt("codegraph")
            self.assertEqual(receipt["schema"], 2)
            self.assertEqual(receipt["baseline"], baseline)
            self.assertEqual(len(receipt["trust"]), 2)
            self.assertEqual(owner.apply("codegraph", self.codex), "no-op")
            document, _ = owner._hooks(self.codex)
            document["hooks"]["SessionStart"].append(copy.deepcopy(foreign))
            (self.codex / "hooks.json").write_text(json.dumps(document))
            owner.unconfigure("codegraph", self.codex)
        after, _ = owner._hooks(self.codex)
        self.assertTrue(after["extra"])
        self.assertEqual(after["hooks"]["SessionStart"], [foreign, {"hooks": []}, foreign])
        self.assertNotIn("state", owner._toml(self.codex / "config.toml").get("hooks", {}))
        self.assertIsNone(owner._read_receipt("codegraph"))

    def test_native_hash_failure_restores_all_bytes(self):
        before = (self.codex / "config.toml").read_bytes()
        with self.run_configure(), patch.object(codex_hook_state, "list_hooks", return_value=[{"hooks": []}]):
            with self.assertRaises(owner.NativeIntegrationError):
                owner.apply("codegraph", self.codex)
        self.assertEqual(before, (self.codex / "config.toml").read_bytes())
        self.assertFalse((self.codex / "hooks.json").exists())
        self.assertIsNone(owner._read_receipt("codegraph"))

    def test_trust_drift_does_not_overwrite_other_owners(self):
        with self.run_configure(), patch.object(codex_hook_state, "list_hooks", side_effect=self.rows):
            owner.apply("codegraph", self.codex)
            path = self.codex / "config.toml"
            path.write_text(path.read_text().replace("sha256:" + "a" * 64, "sha256:" + "c" * 64))
            before = path.read_bytes()
            self.assertEqual(owner.inspect("codegraph", self.codex, enabled=True)["state"], "drifted")
            with self.assertRaises(owner.NativeIntegrationError):
                owner.apply("codegraph", self.codex, repair=True)
            self.assertEqual(before, path.read_bytes())

    def test_receipt_failure_restores_hooks_trust_and_mcp_bytes(self):
        path = self.codex / "config.toml"
        before = path.read_bytes()
        with self.run_configure(), patch.object(codex_hook_state, "list_hooks", side_effect=self.rows), \
                patch.object(owner, "_write_receipt", side_effect=OSError("fixture")):
            with self.assertRaises(owner.NativeIntegrationError):
                owner.apply("codegraph", self.codex)
        self.assertEqual(before, path.read_bytes())
        self.assertFalse((self.codex / "hooks.json").exists())

    def test_disabled_hooks_fail_before_any_write(self):
        path = self.codex / "config.toml"
        path.write_text(path.read_text().replace("hooks = true", "hooks = false"))
        before = path.read_bytes()
        with self.run_configure():
            with self.assertRaises(owner.NativeIntegrationError):
                owner.apply("codegraph", self.codex)
        self.assertEqual(before, path.read_bytes())
