from pathlib import Path
import copy
import json
import os
import tempfile
import unittest
from unittest.mock import patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import copilot_integrations as copilot
import config_model
import connection_owners
import owned_files
import pennix
import apply_journal


class CopilotIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "user"
        self.home.mkdir()
        self.target = self.root / "copilot-custom"
        self.target.mkdir()
        self.codex = self.home / ".codex/config.toml"
        self.codex.parent.mkdir()
        self.codex.write_text('[mcp_servers.keep]\ncommand = "keep"\n')
        self.codex_before = self.codex.read_bytes()
        self.env = patch.dict(os.environ, {"HOME": str(self.home), "XDG_STATE_HOME": str(self.root / "state"),
                                          "CODEX_HOME": str(self.codex.parent), "COPILOT_HOME": str(self.root / "wrong")})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.want = {"mcp": {"type": "local", "command": "/usr/bin/node", "args": ["/fixed/server.mjs", "--agent", "copilot-cli"],
                             "tools": ["*"], "env": {"COPILOT_HOME": str(self.target)}},
                     "files": {str(self.target / "hooks/context-mode.json"): '{"version":1}', **{
                         str(self.target / "skills" / n / "SKILL.md"): f"---\nname: {n}\n---\nfixture" for n in copilot.context.SKILLS}}}
        self.desired_patch = patch.object(copilot, "desired", side_effect=lambda key, home: copy.deepcopy(self.want) if key == "context-mode" else {
            "mcp": {"type": "stdio", "command": "codegraph", "args": ["serve", "--mcp"], "tools": ["*"], "deferTools": "never"}, "files": {}})
        self.desired_patch.start()
        self.addCleanup(self.desired_patch.stop)
        self.calls = []
        self.fail_after_mcp = False
        self.native = patch.object(copilot, "run", side_effect=self.run_native)
        self.native.start()
        self.addCleanup(self.native.stop)
        self.hooks = patch.object(copilot, "verify_hooks")
        self.hooks.start()
        self.addCleanup(self.hooks.stop)
        self.mcp = self.target / "mcp-config.json"
        self.mcp.write_text(json.dumps({"mcpServers": {"github": {"url": "https://example.invalid"}}}))

    def run_native(self, args, home):
        self.assertEqual(home, self.target)
        self.calls.append(args)
        if args[:3] == ["copilot", "skill", "list"]:
            if self.fail_after_mcp:
                raise copilot.Error("injected failure")
            return b"[]"
        if args[:2] == ["codegraph", "install"]:
            self.assertEqual(args, ["codegraph", "install", "--target", "copilot-cli", "--location", "global", "--yes"])
            key = "codegraph"
        elif args[:3] == ["copilot", "mcp", "remove"]:
            value = json.loads(self.mcp.read_text())
            value["mcpServers"].pop(args[3])
            self.mcp.write_text(json.dumps(value))
            return b""
        elif args[:3] == ["copilot", "mcp", "add"]:
            key = args[3]
        elif args[:2] == ["copilot", "skill"]:
            path = home / "settings.json"
            value = copilot.document(path)
            entries = value.setdefault("skillDirectories", [])
            if args[2] == "add":
                entries.append(args[3])
            else:
                entries.remove(args[3])
            path.write_text(json.dumps(value))
            return b""
        else:
            self.fail(f"unexpected owner call: {args}")
        value = json.loads(self.mcp.read_text())
        value["mcpServers"][key] = copilot.desired(key, home)["mcp"]
        self.mcp.write_text(json.dumps(value))
        return b""

    def test_apply_noop_withdraw_isolated_from_codex_and_other_mcp(self):
        self.mcp.chmod(0o640)
        with owned_files.locked():
            self.assertEqual(copilot.apply("context-mode", self.target), "changed")
            self.assertEqual(copilot.apply("context-mode", self.target), "no-op")
            self.assertEqual(copilot.inspect("context-mode", self.target)["state"], "current")
            self.assertEqual(copilot.unconfigure("context-mode", self.target), "changed")
        self.assertEqual(self.codex.read_bytes(), self.codex_before)
        self.assertEqual(json.loads(self.mcp.read_text()), {"mcpServers": {"github": {"url": "https://example.invalid"}}})
        self.assertEqual(self.mcp.stat().st_mode & 0o777, 0o640)
        self.assertFalse((self.home / ".copilot").exists())
        self.assertFalse((self.root / "wrong").exists())

    def test_codegraph_native_no_init_separate_receipt_and_target(self):
        copilot.apply("codegraph", self.target)
        copilot.apply("context-mode", self.target)
        a, target = copilot.paths("codegraph", self.target)
        b, _ = copilot.paths("context-mode", self.target)
        self.assertNotEqual(a, b)
        self.assertNotEqual(a, copilot.paths("codegraph", self.root / "other")[0])
        legacy = owned_files.state_root() / "native/codegraph.json"
        legacy.write_text('{"schema":1,"legacy":"untouched"}')
        copilot.unconfigure("codegraph", self.target)
        self.assertEqual(copilot.inspect("context-mode", self.target)["state"], "current")
        self.assertEqual(legacy.read_text(), '{"schema":1,"legacy":"untouched"}')
        with patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "other-state")}):
            with self.assertRaises(copilot.Error):
                copilot.bind("codegraph", self.target)
        self.assertTrue(target.exists())

    def test_partial_failure_restore_and_unknown_drift_rejected(self):
        self.fail_after_mcp = True
        with self.assertRaises(copilot.Error):
            copilot.apply("context-mode", self.target)
        self.assertIsNotNone(copilot.read("context-mode", self.target))
        self.assertEqual(copilot.recover("context-mode", self.target), "restored")
        self.fail_after_mcp = False
        copilot.apply("context-mode", self.target)
        path = self.target / "skills/ctx-doctor/SKILL.md"
        path.write_text("user edit")
        before = self.mcp.read_bytes()
        for action in (copilot.apply, copilot.unconfigure, copilot.recover):
            with self.assertRaises(copilot.Error):
                action("context-mode", self.target)
        self.assertEqual(path.read_text(), "user edit")
        self.assertEqual(self.mcp.read_bytes(), before)

    def test_partial_withdraw_can_finish_only_recorded_removal(self):
        copilot.apply("context-mode", self.target)
        record = copilot.read("context-mode", self.target)
        record["operation"] = {"id": "transaction:withdraw", "action": "unconfigure"}
        path = copilot.bind("context-mode", self.target)
        owned_files.replace(path, owned_files.json_bytes(record), owned_files.read(path))
        (self.target / "skills/ctx-stats/SKILL.md").unlink()
        self.assertEqual(copilot.unconfigure("context-mode", self.target, recovery=True, operation_id="transaction:withdraw"), "changed")
        self.assertEqual(copilot.inspect("context-mode", self.target)["state"], "absent")

    def test_recovery_requires_the_same_transaction_and_missing_receipt_is_unknown(self):
        with self.assertRaises(copilot.Error):
            copilot.recover("context-mode", self.target, operation_id="transaction:configure")
        self.fail_after_mcp = True
        with self.assertRaises(copilot.Error):
            copilot.apply("context-mode", self.target, operation_id="transaction:configure")
        for action in (lambda: copilot.recover("context-mode", self.target, operation_id="other:configure"),
                       lambda: copilot.unconfigure("context-mode", self.target, recovery=True, operation_id="transaction:configure")):
            with self.assertRaises(copilot.Error):
                action()
        self.assertEqual(copilot.inspect("context-mode", self.target)["state"], "current")
        self.assertEqual(copilot.recover("context-mode", self.target, operation_id="transaction:configure"), "restored")

    def test_failed_update_restores_the_previous_configuration_and_receipt(self):
        (self.home / ".agents/skills/pennix-skills").mkdir(parents=True)
        copilot.apply("context-mode", self.target)
        receipt = copilot.read("context-mode", self.target)
        before = copy.deepcopy(self.want)
        directories = copilot.document(self.target / "settings.json")["skillDirectories"]
        self.want["mcp"]["command"] = "/updated/node"
        for p in self.want["files"]:
            self.want["files"][p] += "\nupdated"
        self.fail_after_mcp = True
        with self.assertRaises(copilot.Error):
            copilot.apply("context-mode", self.target, operation_id="update:configure")
        self.assertEqual(copilot.recover("context-mode", self.target, operation_id="update:configure"), "restored")
        self.assertTrue(copilot.matches("context-mode", self.target, before))
        self.assertEqual(copilot.read("context-mode", self.target), receipt)
        self.assertEqual(copilot.document(self.target / "settings.json")["skillDirectories"], directories)
        self.want = before
        self.fail_after_mcp = False
        self.assertEqual(copilot.inspect("context-mode", self.target)["state"], "current")
        with self.assertRaises(copilot.Error):
            copilot.recover("context-mode", self.target)

    def test_explicit_repair_restores_missing_owned_file_and_rejects_unknown_content(self):
        copilot.apply("context-mode", self.target)
        path = self.target / "skills/ctx-stats/SKILL.md"
        path.unlink()
        self.assertEqual(copilot.apply("context-mode", self.target, repair=True), "changed")
        self.assertEqual(path.read_text(), self.want["files"][str(path)])
        path.write_text("user changed")
        with self.assertRaises(copilot.Error):
            copilot.apply("context-mode", self.target, repair=True)

    def test_policy_and_unknown_receipt_block_before_native_write(self):
        (self.target / "settings.json").write_text('{"disableAllHooks":true}')
        with self.assertRaises(copilot.Error):
            copilot.apply("context-mode", self.target)
        self.assertEqual(self.calls, [])
        path, _ = copilot.paths("context-mode", self.target)
        owned_files.replace(path, b'{"schema":999}', None)
        with self.assertRaises(copilot.Error):
            copilot.inspect("context-mode", self.target)

    def test_prewrite_withdraw_rejection_preserves_receipt_and_reports_no_effect(self):
        copilot.apply("context-mode", self.target)
        receipt = copilot.read("context-mode", self.target)
        (self.target / "skills/ctx-stats/SKILL.md").write_text("user edit")
        with self.assertRaises(copilot.Error) as caught:
            copilot.unconfigure("context-mode", self.target)
        self.assertEqual(caught.exception.effect, "none")
        self.assertEqual(copilot.read("context-mode", self.target), receipt)

    def test_missing_collections_use_native_directory_registration_and_exact_withdraw(self):
        directory = self.home / ".agents/skills/pennix-skills"
        directory.mkdir(parents=True)
        settings = self.target / "settings.json"
        settings.write_text('{"skillDirectories":["/user/keep"],"other":true}')
        copilot.apply("context-mode", self.target)
        self.assertIn(["copilot", "skill", "add", str(directory)], self.calls)
        self.assertEqual(copilot.read("context-mode", self.target)["directories"], [str(directory)])
        copilot.unconfigure("context-mode", self.target)
        self.assertEqual(json.loads(settings.read_text()), {"skillDirectories": ["/user/keep"], "other": True})

    def test_existing_collection_registration_is_never_claimed(self):
        directory = self.home / ".agents/skills/pennix-skills"
        directory.mkdir(parents=True)
        settings = self.target / "settings.json"
        settings.write_text(json.dumps({"skillDirectories": [str(directory)]}))
        copilot.apply("context-mode", self.target)
        self.assertEqual(copilot.read("context-mode", self.target)["directories"], [])
        copilot.unconfigure("context-mode", self.target)
        self.assertEqual(json.loads(settings.read_text())["skillDirectories"], [str(directory)])

    def test_directory_registration_restores_absent_settings_key(self):
        (self.home / ".agents/skills/pennix-skills").mkdir(parents=True)
        settings = self.target / "settings.json"
        settings.write_text('{"other":true}')
        copilot.apply("context-mode", self.target)
        copilot.unconfigure("context-mode", self.target)
        self.assertEqual(json.loads(settings.read_text()), {"other": True})

    def test_shared_lock_blocks_second_writer(self):
        with owned_files.locked():
            with self.assertRaises(owned_files.OwnershipError):
                with owned_files.locked():
                    self.fail("must not acquire another writer")

    def test_legacy_configuration_keeps_copilot_disabled(self):
        config = config_model._new()
        self.assertEqual(config_model.config_value(config, "PENNIX_TARGET_COPILOT_CONTEXT_MODE"), "n")
        self.assertEqual(config_model.config_value(config, "PENNIX_TARGET_COPILOT_CODEGRAPH"), "n")

    def test_plan_apply_and_update_repair_steps_use_the_selected_home(self):
        config = config_model._new()
        config.syms["PENNIX_COPILOT_HOME"].set_value(str(self.target))
        config.syms["PENNIX_TARGET_COPILOT_CONTEXT_MODE"].set_value("y")
        spec = {"select": "PENNIX_TARGET_COPILOT_CONTEXT_MODE", "host": "copilot-cli", "native": "context-mode",
                "owner": "context-mode", "component": "context-mode", "command": ["copilot"],
                "values": {"home": "PENNIX_COPILOT_HOME"}, "secrets": {}}
        catalog = {"configuration_targets": {"copilot-context-mode": spec}}
        status = connection_owners.plan(config, catalog, self.codex.parent, self.root / "absent-secrets")
        self.assertEqual(status["copilot-context-mode"]["action"], "configure")
        self.assertEqual(status["copilot-context-mode"]["target_home"], str(self.target))
        self.assertEqual(connection_owners.apply_target("copilot-context-mode", spec, config, self.codex.parent,
                                                       self.root / "absent-secrets", "configure"), "changed")
        from types import SimpleNamespace
        steps = pennix._copilot_steps(SimpleNamespace(kconf=config, component="context-mode"), catalog)
        self.assertEqual(steps[0]["component"], "connection:copilot-context-mode")
        status = connection_owners.plan(config, catalog, self.codex.parent, self.root / "absent-secrets")
        self.assertEqual(status["copilot-context-mode"]["action"], "keep")
        config.syms["PENNIX_TARGET_COPILOT_CONTEXT_MODE"].set_value("n")
        (self.target / "settings.json").write_text('{"disableAllHooks":true}')
        status = connection_owners.plan(config, catalog, self.codex.parent, self.root / "absent-secrets")
        self.assertEqual(status["copilot-context-mode"]["action"], "unconfigure")
        connection_owners.apply_target("copilot-context-mode", spec, config, self.codex.parent,
                                       self.root / "absent-secrets", "unconfigure")
        self.assertTrue(copilot.document(self.target / "settings.json")["disableAllHooks"])

    def test_full_uninstall_collects_the_explicit_copilot_target_with_hooks_disabled(self):
        from types import SimpleNamespace
        config = config_model._new()
        config.syms["PENNIX_CODEX_HOME"].set_value(str(self.codex.parent))
        config.syms["PENNIX_COPILOT_HOME"].set_value(str(self.target))
        spec = {"host": "copilot-cli", "native": "context-mode"}
        catalog = {"components": {}, "configuration_targets": {"copilot-context-mode": spec}}
        copilot.apply("context-mode", self.target)
        (self.target / "settings.json").write_text('{"disableAllHooks":true}')
        args = SimpleNamespace(component=None, all=True, confirm_all="ALL", json=True)
        with patch.object(pennix, "_load", return_value=(config, self.root / "config", catalog)), \
             patch.object(pennix, "_plan", return_value={"components": {}}), \
             patch.object(pennix.relay_owner, "local_owned", return_value=False), \
             patch.object(pennix.relay_owner, "daemon_owned", return_value=False), \
             patch.object(pennix.paseo_service, "inspect", return_value="absent"), \
             patch.object(pennix, "_open_transaction", side_effect=RuntimeError("validated preflight")) as opened:
            with self.assertRaisesRegex(RuntimeError, "validated preflight"):
                pennix.cmd_uninstall(args)
        self.assertEqual(opened.call_args.args[3][0]["id"], "connection:copilot-context-mode:unconfigure")

    def test_native_recover_restores_only_the_bound_failed_step(self):
        from types import SimpleNamespace
        config = config_model._new()
        config.syms["PENNIX_CODEX_HOME"].set_value(str(self.codex.parent))
        config.syms["PENNIX_COPILOT_HOME"].set_value(str(self.target))
        config_path = self.root / "workflow.config"
        config_path.write_text("synthetic configuration")
        lock = self.root / "components.lock.json"
        lock.write_text("{}")
        step_id = "connection:copilot-context-mode:configure"
        steps = [{"id": step_id, "component": "connection:copilot-context-mode", "action": "configure", "target": {}}]
        active = apply_journal.start("apply", None, apply_journal.digest(config_path), apply_journal.digest(lock), steps)
        active = apply_journal.started(active, step_id)
        operation_id = f"{active['transaction']}:{step_id}"
        self.fail_after_mcp = True
        with self.assertRaises(copilot.Error):
            copilot.apply("context-mode", self.target, operation_id=operation_id)
        apply_journal.unknown(active, step_id)
        args = SimpleNamespace(resolve="restored", transaction=active["transaction"], step=step_id,
                               component_lock_path=lock, json=True)
        catalog = {"configuration_targets": {"copilot-context-mode": {"host": "copilot-cli", "native": "context-mode"}}}
        with patch.object(pennix, "_load", return_value=(config, config_path, catalog)), patch.object(pennix, "_emit"):
            self.assertEqual(pennix.cmd_recover(args), 0)
        self.assertIsNone(apply_journal.read())
        self.assertIsNone(copilot.read("context-mode", self.target))

    def test_native_recover_never_rewinds_a_verified_step(self):
        from types import SimpleNamespace
        config = config_model._new()
        config.syms["PENNIX_CODEX_HOME"].set_value(str(self.codex.parent))
        config.syms["PENNIX_COPILOT_HOME"].set_value(str(self.target))
        config_path = self.root / "workflow.config"
        config_path.write_text("synthetic configuration")
        lock = self.root / "components.lock.json"
        lock.write_text("{}")
        step_id = "connection:copilot-context-mode:configure"
        active = apply_journal.start("apply", None, apply_journal.digest(config_path), apply_journal.digest(lock), [
            {"id": step_id, "component": "connection:copilot-context-mode", "action": "configure", "target": {}},
            {"id": "later", "component": "later", "action": "configure", "target": {}}])
        copilot.apply("context-mode", self.target, operation_id=f"{active['transaction']}:{step_id}")
        apply_journal.verified(active, step_id)
        args = SimpleNamespace(resolve="restored", transaction=active["transaction"], step=step_id,
                               component_lock_path=lock, json=True)
        catalog = {"configuration_targets": {"copilot-context-mode": {"host": "copilot-cli", "native": "context-mode"}}}
        before = self.mcp.read_bytes()
        with patch.object(pennix, "_load", return_value=(config, config_path, catalog)), self.assertRaises(pennix.CliError):
            pennix.cmd_recover(args)
        self.assertEqual(self.mcp.read_bytes(), before)

    def test_prewrite_policy_rejection_has_no_effect_and_can_release_the_journal(self):
        from types import SimpleNamespace
        config = config_model._new()
        config.syms["PENNIX_CODEX_HOME"].set_value(str(self.codex.parent))
        config.syms["PENNIX_COPILOT_HOME"].set_value(str(self.target))
        config_path = self.root / "workflow.config"
        config_path.write_text("synthetic configuration")
        lock = self.root / "components.lock.json"
        lock.write_text("{}")
        step_id = "connection:copilot-context-mode:configure"
        active = apply_journal.start("apply", None, apply_journal.digest(config_path), apply_journal.digest(lock), [
            {"id": step_id, "component": "connection:copilot-context-mode", "action": "configure", "target": {}}])
        (self.target / "settings.json").write_text('{"disableAllHooks":true}')
        spec = {"host": "copilot-cli", "native": "context-mode"}
        with self.assertRaises(connection_owners.ConnectionError):
            pennix._journal_mutate(active, step_id, lambda: connection_owners.apply_target(
                "copilot-context-mode", spec, config, self.codex.parent, self.root / "secrets", "configure",
                operation_id=f"{active['transaction']}:{step_id}"))
        self.assertEqual(apply_journal.step(apply_journal.read(), step_id)["observed"]["effect"], "none")
        self.assertEqual(self.calls, [])
        self.assertIsNone(copilot.read("context-mode", self.target))
        args = SimpleNamespace(resolve="not-started", transaction=active["transaction"], step=step_id,
                               component_lock_path=lock, json=True)
        with patch.object(pennix, "_load", return_value=(config, config_path, {"configuration_targets": {}})), patch.object(pennix, "_emit"):
            self.assertEqual(pennix.cmd_recover(args), 0)
        self.assertIsNone(apply_journal.read())

    def test_native_completed_recovery_finalizes_the_same_pending_receipt(self):
        from types import SimpleNamespace
        config = config_model._new()
        config.syms["PENNIX_CODEX_HOME"].set_value(str(self.codex.parent))
        config.syms["PENNIX_COPILOT_HOME"].set_value(str(self.target))
        config_path = self.root / "workflow.config"
        config_path.write_text("synthetic configuration")
        lock = self.root / "components.lock.json"
        lock.write_text("{}")
        step_id = "connection:copilot-context-mode:configure"
        active = apply_journal.start("apply", None, apply_journal.digest(config_path), apply_journal.digest(lock), [
            {"id": step_id, "component": "connection:copilot-context-mode", "action": "configure", "target": {}}])
        operation_id = f"{active['transaction']}:{step_id}"
        original = owned_files.replace
        receipt_path = copilot.bind("context-mode", self.target)
        def fail_final_receipt(path, content, before, **kwargs):
            if path == receipt_path and content and json.loads(content)["rollback"] is None:
                raise OSError("injected final receipt failure")
            return original(path, content, before, **kwargs)
        with patch.object(owned_files, "replace", side_effect=fail_final_receipt), self.assertRaises(copilot.Error):
            pennix._journal_mutate(active, step_id, lambda: copilot.apply("context-mode", self.target, operation_id=operation_id))
        self.assertIsNotNone(copilot.read("context-mode", self.target)["rollback"])
        with self.assertRaises(copilot.Error):
            copilot.complete("context-mode", self.target, operation_id="another:step")
        args = SimpleNamespace(resolve="completed", transaction=active["transaction"], step=step_id,
                               component_lock_path=lock, json=True)
        catalog = {"components": {}, "configuration_targets": {"copilot-context-mode": {
            "host": "copilot-cli", "native": "context-mode", "values": {}, "secrets": {}}}}
        with patch.object(pennix, "_load", return_value=(config, config_path, catalog)), patch.object(pennix, "_emit"):
            self.assertEqual(pennix.cmd_recover(args), 0)
        self.assertIsNone(apply_journal.read())
        self.assertIsNone(copilot.read("context-mode", self.target)["rollback"])
        self.assertEqual(copilot.apply("context-mode", self.target), "no-op")
        with self.assertRaises(copilot.Error):
            copilot.recover("context-mode", self.target, operation_id=operation_id)


if __name__ == "__main__":
    unittest.main()
