from pathlib import Path
import json
import os
import copy
import shutil
import tomllib
import tempfile
import unittest
from unittest.mock import patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import native_integrations
import context_mode_integration
import config_model
import pennix
from types import SimpleNamespace


FAKE_NATIVE = r'''#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

home = Path(os.environ["CODEX_HOME"])
config = home / "config.toml"
home.mkdir(parents=True, exist_ok=True)
args = sys.argv[1:]
if Path(sys.argv[0]).name == "codex":
    import tomlkit
    document = tomlkit.parse(config.read_text() if config.exists() else "")
    if args[:3] == ["mcp", "add", "context-mode"]:
        environment = [args[i + 1].split("=", 1) for i, arg in enumerate(args) if arg == "--env"]
        command = args[args.index("--") + 1:]
        server = {"command": command[0]}
        if command[1:]:
            server["args"] = command[1:]
        if environment:
            server["env"] = dict(environment)
        document.setdefault("mcp_servers", tomlkit.table())["context-mode"] = server
    elif args == ["mcp", "remove", "context-mode"]:
        document["mcp_servers"].pop("context-mode", None)
    else:
        sys.exit(2)
    config.write_text(tomlkit.dumps(document))
else:
    if args[0] == "install":
        config.write_text('[mcp_servers.codegraph]\ncommand = "codegraph"\nargs = ["serve", "--mcp"]\n')
    else:
        config.write_text('[mcp_servers.other]\ncommand = "other"\n')
'''


def context_plan(root, home):
    node = str(Path(shutil.which("node")).resolve())
    mcp = {"command": node, "args": [str(root / "server.mjs"), "--agent", "codex"],
           "omit_tools_from": ["deferred"], "default_tools_approval_mode": "approve"}
    block = "[mcp_servers.context-mode]\n" + "".join(f"{k} = {json.dumps(v)}\n" for k, v in mcp.items())
    hooks = {}
    for event, native in context_mode_integration.EVENTS.items():
        entry = {"hooks": [{"type": "command", "command": f'{json.dumps(node)} {json.dumps(str(root / "hook.mjs"), ensure_ascii=False)} --agent codex', "timeout": 5}]}
        if event in {"PreToolUse", "PostToolUse"}:
            entry["matcher"] = "^(Bash|apply_patch|mcp__context_mode__ctx_.*)$"
        hooks[event] = [entry]
        key = f"{home / 'hooks.json'}:{native}:0:0"
        block += f'\n[hooks.state.{json.dumps(key)}]\ntrusted_hash = "sha256:{"a" * 64}"\n'
    steps = [{"kind": "config", "file": str(home / "config.toml"), "format": "toml",
              "key": ["mcp_servers", "context-mode"], "block": block},
             {"kind": "hook", "file": str(home / "hooks.json"), "block": json.dumps({"hooks": hooks})}]
    for name in sorted(context_mode_integration.SKILLS):
        steps.append({"kind": "skill", "file": str(home / "skills" / name / "SKILL.md"),
                      "block": f"---\nname: {name}\ndescription: fixture\ndisable-model-invocation: true\n---\nRead-only fixture.\n"})
    return {"command": "setup", "print": {"agent": "codex", "steps": steps, "verify": "context-mode doctor --agents codex"}}


class NativeIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "home with 空格"
        self.codex = self.home / ".codex"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for name in ("codex", "codegraph"):
            path = self.bin / name
            path.write_text(FAKE_NATIVE, encoding="utf-8")
            path.chmod(0o700)
        self.environment = patch.dict(os.environ, {
            "HOME": str(self.home), "PATH": f"{self.bin}:{self.home / '.local/bin'}:{os.environ['PATH']}",
            "XDG_STATE_HOME": str(self.root / "state"), "CODEX_HOME": str(self.codex),
        })
        self.environment.start()
        self.codex.mkdir(parents=True)
        (self.codex / "config.toml").write_text('[features]\nhooks = true\n[mcp_servers.other]\ncommand = "other"\n')
        self.package = self.home / ".local/lib/node_modules/@pennixrv/context-mode"
        (self.package / "configs/codex").mkdir(parents=True)
        (self.package / "package.json").write_text(json.dumps({"name": "@pennixrv/context-mode", "version": "2.1.5-pennix.1"}))
        self.plan = context_plan(self.package, self.codex)
        (self.package / "plan.json").write_text(json.dumps(self.plan))
        (self.package / "cli.mjs").write_text('import {readFileSync} from "node:fs"; console.log(readFileSync(new URL("plan.json", import.meta.url), "utf8"));\n')
        hook_config = {"hooks": {event: [{"hooks": [{"type": "command", "command": f"context-mode hook codex {event.lower()}"}]}]
                                 for event in native_integrations.EVENTS}}
        (self.package / "configs/codex/hooks.json").write_text(json.dumps(hook_config))
        entry = self.home / ".local/bin/context-mode"
        entry.parent.mkdir(parents=True)
        entry.symlink_to(self.package / "cli.mjs")

    def tearDown(self):
        self.environment.stop()
        self.temp.cleanup()

    def test_install_upgrade_idempotence_and_uninstall_preserve_other_owners(self):
        other = {"extra": "preserve", "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "trellis hook"}]}]}}
        (self.codex / "hooks.json").write_text(json.dumps(other))
        self.assertEqual(native_integrations.inspect("context-mode", self.codex, enabled=True)["state"], "absent")
        self.assertEqual(native_integrations.apply("context-mode", self.codex), "changed")
        self.assertEqual(native_integrations.apply("context-mode", self.codex), "no-op")
        (self.package / "package.json").write_text(json.dumps({"name": "@pennixrv/context-mode", "version": "2.1.5-pennix.2"}))
        self.assertEqual(native_integrations.inspect("context-mode", self.codex, enabled=True)["state"], "current")
        self.assertIn("preserved", native_integrations.unconfigure("context-mode", self.codex))
        self.assertEqual(json.loads((self.codex / "hooks.json").read_text()), other)
        self.assertIn('command = "other"', (self.codex / "config.toml").read_text())
        self.assertFalse(native_integrations._receipt_path("context-mode").exists())

    def test_single_component_repair_registers_fresh_native_integration(self):
        path = config_model.init(str(self.root / "workflow.config"))
        kconf, _ = config_model.load(str(path))
        kconf.syms["PENNIX_CODEX_HOME"].set_value(str(self.codex))
        kconf.syms["PENNIX_COMPONENT_CODEX_CLI"].set_value("y")
        kconf.syms["PENNIX_CODEX_HOOKS"].set_value("y")
        kconf.syms["PENNIX_COMPONENT_CONTEXT_MODE"].set_value("y")
        config_model.write(kconf, path)
        args = SimpleNamespace(component="context-mode", config=str(path), json=True, expected_sha256=None)
        with patch.object(pennix.owner_engine, "component_operation", return_value="changed") as package:
            self.assertEqual(pennix.cmd_repair(args), 0)
        package.assert_called_once()
        self.assertEqual(native_integrations.inspect("context-mode", self.codex, enabled=True)["state"], "current")

    def seed_without_path(self):
        desired = native_integrations._desired("context-mode", self.codex, enabled=True)
        native_integrations._context_apply(self.codex, desired, None)
        return native_integrations._read_receipt("context-mode")

    def test_path_migration_is_stable_and_wrapper_finds_its_dependency(self):
        self.seed_without_path()
        local_bin = self.home / ".local/bin"
        wrapper, dependency = local_bin / "wrapper", local_bin / "dependency"
        wrapper.write_text('#!/bin/sh\nexec dependency\n')
        dependency.write_text('#!/bin/sh\nprintf "dependency-found"\n')
        wrapper.chmod(0o700)
        dependency.chmod(0o700)
        with patch.dict(os.environ, {"PATH": "/usr/bin"}):
            self.assertEqual("update-required", native_integrations.inspect("context-mode", self.codex, enabled=True)["state"])
            with self.assertRaisesRegex(context_mode_integration.ContextModeError, "原生环境"):
                native_integrations.apply("context-mode", self.codex, repair=True)
        native_integrations.apply("context-mode", self.codex, repair=True)
        receipt = native_integrations._read_receipt("context-mode")
        saved_path = receipt["mcp"]["env"]["PATH"]
        with patch.dict(os.environ, {"PATH": "/usr/bin"}):
            self.assertEqual("current", native_integrations.inspect("context-mode", self.codex, enabled=True)["state"])
            self.assertEqual("no-op", native_integrations.apply("context-mode", self.codex))
        with patch.dict(os.environ, {"PATH": str(self.root)}):
            self.assertEqual("current", native_integrations.inspect("context-mode", self.codex, enabled=True)["state"])
        import subprocess
        result = subprocess.run([str(wrapper)], env={"PATH": saved_path}, capture_output=True, text=True)
        self.assertEqual((0, "dependency-found"), (result.returncode, result.stdout))
        self.assertEqual(receipt, native_integrations._read_receipt("context-mode"))

    def test_path_filters_relative_empty_and_session_directories_in_order(self):
        temporary = self.codex / "tmp/codex-arg0-fixture"
        temporary.mkdir(parents=True)
        local_bin = self.home / ".local/bin"
        (local_bin / "node").symlink_to(shutil.which("node"))
        raw = f"relative::{temporary}:{local_bin}:/usr/bin:{local_bin}"
        with patch.dict(os.environ, {"PATH": raw}):
            self.assertEqual(f"{local_bin}:/usr/bin", context_mode_integration.configured_path(self.codex))

    def test_failed_path_migration_restores_original_receipt_and_mcp(self):
        previous = self.seed_without_path()
        config = native_integrations._toml(self.codex / "config.toml")
        hooks = (self.codex / "hooks.json").read_bytes()
        with patch.object(native_integrations, "_write_receipt", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(context_mode_integration.ContextModeError, "已撤回"):
                native_integrations.apply("context-mode", self.codex, repair=True)
        self.assertEqual(previous, native_integrations._read_receipt("context-mode"))
        self.assertEqual(config, native_integrations._toml(self.codex / "config.toml"))
        self.assertEqual(hooks, (self.codex / "hooks.json").read_bytes())

    def test_invalid_receipted_path_is_rejected(self):
        native_integrations.apply("context-mode", self.codex)
        path = native_integrations._receipt_path("context-mode")
        record = json.loads(path.read_bytes())
        record["mcp"]["env"]["PATH"] = "/usr/bin:relative"
        path.write_text(json.dumps(record))
        before = (self.codex / "config.toml").read_bytes()
        with self.assertRaises(context_mode_integration.ContextModeError):
            native_integrations.apply("context-mode", self.codex, repair=True)
        self.assertEqual(before, (self.codex / "config.toml").read_bytes())

    def test_new_hooks_file_is_removed_on_uninstall(self):
        native_integrations.apply("context-mode", self.codex)
        native_integrations.unconfigure("context-mode", self.codex)
        self.assertFalse((self.codex / "hooks.json").exists())

    def test_absent_native_config_does_not_require_installed_package(self):
        (self.package / "package.json").unlink()
        self.assertEqual(native_integrations.inspect("context-mode", self.codex, enabled=True)["state"], "absent")

    def test_external_hooks_or_mcp_are_not_taken_over(self):
        for source in ("mcp", "hooks"):
            with self.subTest(source=source):
                if source == "mcp":
                    (self.codex / "config.toml").write_text('[mcp_servers.context-mode]\ncommand = "external"\n')
                else:
                    (self.codex / "config.toml").write_text('[features]\nhooks = true\n')
                    (self.codex / "hooks.json").write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{"command": "context-mode hook codex stop"}]}]}}))
                self.assertEqual(native_integrations.inspect("context-mode", self.codex, enabled=True)["state"], "external")
                with self.assertRaises(native_integrations.NativeIntegrationError):
                    native_integrations.apply("context-mode", self.codex)

    def test_drifted_hooks_are_not_overwritten_or_removed(self):
        native_integrations.apply("context-mode", self.codex)
        path = self.codex / "hooks.json"
        path.write_text(path.read_text().replace("--agent codex", "--agent different"))
        for operation in (native_integrations.apply, native_integrations.unconfigure):
            with self.assertRaises(native_integrations.NativeIntegrationError):
                operation("context-mode", self.codex)
        self.assertTrue(native_integrations._receipt_path("context-mode").exists())
        with self.assertRaises(native_integrations.NativeIntegrationError):
            native_integrations.apply("context-mode", self.codex, repair=True)

    def test_package_hook_refresh_cannot_adopt_unreceipted_writes(self):
        native_integrations.apply("context-mode", self.codex)
        hooks = json.loads((self.codex / "hooks.json").read_text())
        hooks["hooks"]["PreToolUse"][0]["matcher"] = "mcp__.*"
        (self.codex / "hooks.json").write_text(json.dumps(hooks))
        self.assertEqual(native_integrations.inspect("context-mode", self.codex, enabled=True)["state"], "drifted")
        with self.assertRaises(native_integrations.NativeIntegrationError):
            native_integrations.apply("context-mode", self.codex)

    def test_receipt_failure_retracts_only_this_installation(self):
        before = (self.codex / "config.toml").read_text()
        with patch.object(native_integrations, "_write_receipt", side_effect=OSError("disk full")):
            with self.assertRaises(native_integrations.NativeIntegrationError):
                native_integrations.apply("context-mode", self.codex)
        self.assertEqual(native_integrations._toml(self.codex / "config.toml"), tomllib.loads(before))
        self.assertFalse((self.codex / "hooks.json").exists())

    def test_unconfigure_uses_receipt_even_if_package_is_missing(self):
        native_integrations.apply("context-mode", self.codex)
        (self.package / "package.json").unlink()
        native_integrations.unconfigure("context-mode", self.codex)
        self.assertIsNone(native_integrations._server("context-mode", self.codex))

    def test_plan_rejects_unknown_steps_commands_paths_and_fields_before_writes(self):
        invalid = []
        value = copy.deepcopy(self.plan)
        value["print"]["steps"].append({"kind": "run", "argv": ["touch", "marker"]})
        invalid.append(value)
        value = copy.deepcopy(self.plan)
        value["print"]["steps"][1]["file"] = str(self.root / "outside.json")
        invalid.append(value)
        value = copy.deepcopy(self.plan)
        value["print"]["steps"][2]["file"] = str(self.codex / "skills/foreign/SKILL.md")
        invalid.append(value)
        value = copy.deepcopy(self.plan)
        value["print"]["steps"][0]["block"] += '\n[unknown]\ncommand = "touch marker"\n'
        invalid.append(value)
        value = copy.deepcopy(self.plan)
        hooks = json.loads(value["print"]["steps"][1]["block"])
        hooks["hooks"]["SessionStart"][0]["hooks"][0]["command"] += "; touch marker"
        value["print"]["steps"][1]["block"] = json.dumps(hooks)
        invalid.append(value)
        before = (self.codex / "config.toml").read_bytes()
        for value in invalid:
            with self.subTest(plan=invalid.index(value)):
                (self.package / "plan.json").write_text(json.dumps(value))
                with self.assertRaises(native_integrations.NativeIntegrationError):
                    native_integrations.apply("context-mode", self.codex)
                self.assertEqual(before, (self.codex / "config.toml").read_bytes())
                self.assertFalse((self.codex / "hooks.json").exists())

    def test_trust_uses_actual_merged_positions_and_preserves_foreign_fields(self):
        other = {"hooks": {event: [{"hooks": [{"type": "command", "command": "foreign"}]}]
                           for event in context_mode_integration.EVENTS}}
        (self.codex / "hooks.json").write_text(json.dumps(other))
        with (self.codex / "config.toml").open("a") as stream:
            stream.write('\n[hooks.state.foreign]\ntrusted_hash = "preserve"\n')
        native_integrations.apply("context-mode", self.codex)
        receipt = native_integrations._read_receipt("context-mode")
        self.assertEqual(2, receipt["schema"])
        self.assertTrue(all(key.endswith(":1:0") for key in receipt["trust"]))
        self.assertEqual({"trusted_hash": "preserve"}, native_integrations._toml(self.codex / "config.toml")["hooks"]["state"]["foreign"])
        native_integrations.unconfigure("context-mode", self.codex)
        self.assertEqual(other, json.loads((self.codex / "hooks.json").read_text()))
        self.assertEqual({"foreign": {"trusted_hash": "preserve"}}, native_integrations._toml(self.codex / "config.toml")["hooks"]["state"])

    def legacy_receipt(self):
        mcp = {"command": str(self.home / ".local/bin/context-mode"),
               "env": {"CONTEXT_MODE_PLATFORM": "codex", "CODEX_HOME": str(self.codex)}}
        hooks = json.loads((self.package / "configs/codex/hooks.json").read_text())["hooks"]
        native_integrations._mcp_add(self.codex, mcp)
        native_integrations._edit_hooks(self.codex, {}, hooks)
        value = {"schema": 1, "key": "context-mode", "codex_home": str(self.codex),
                 "desired_digest": "a" * 64, "mcp": mcp, "hooks": hooks, "hooks_file_created": True}
        path = native_integrations._receipt_path("context-mode")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))
        path.chmod(0o600)
        return value

    def test_legacy_receipt_migrates_mcp_env_six_hooks_and_command_ownership(self):
        legacy = self.legacy_receipt()
        self.assertEqual("update-required", native_integrations.inspect("context-mode", self.codex, enabled=True)["state"])
        native_integrations.apply("context-mode", self.codex)
        receipt = native_integrations._read_receipt("context-mode")
        self.assertEqual(str(self.codex), receipt["mcp"]["env"]["CODEX_HOME"])
        self.assertIn(str(self.home / ".local/bin"), receipt["mcp"]["env"]["PATH"].split(os.pathsep))
        self.assertNotIn("CONTEXT_MODE_PLATFORM", receipt["mcp"]["env"])
        self.assertEqual(set(context_mode_integration.EVENTS), set(receipt["hooks"]))
        self.assertEqual(context_mode_integration.SKILLS, set(receipt["skills"]))
        self.assertEqual("current", native_integrations.inspect("context-mode", self.codex, enabled=True)["state"])

    def test_failed_migration_restores_legacy_receipt_mcp_and_hooks(self):
        legacy = self.legacy_receipt()
        with patch.object(native_integrations, "_write_receipt", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(native_integrations.NativeIntegrationError, "已撤回"):
                native_integrations.apply("context-mode", self.codex)
        self.assertEqual(legacy, native_integrations._read_receipt("context-mode"))
        self.assertTrue(native_integrations._context_matches(self.codex, legacy))
        self.assertFalse(native_integrations._toml(self.codex / "config.toml").get("hooks", {}).get("state"))

    def trusted_legacy(self):
        legacy = self.legacy_receipt()
        events = {**context_mode_integration.EVENTS, "PreCompact": "pre_compact", "Stop": "stop"}
        rows, state = [], {}
        for event, entries in legacy["hooks"].items():
            key = f"{self.codex / 'hooks.json'}:{events[event]}:0:0"
            digest = "sha256:" + "b" * 64
            state[key] = {"trusted_hash": digest}
            rows.append({"key": key, "command": entries[0]["hooks"][0]["command"],
                         "matcher": entries[0].get("matcher"), "sourcePath": str(self.codex / "hooks.json"),
                         "currentHash": digest, "trustStatus": "trusted"})
        import tomlkit
        path = self.codex / "config.toml"
        document = tomlkit.parse(path.read_text())
        document["hooks"] = {"state": state}
        path.write_text(tomlkit.dumps(document))
        return legacy, rows, state

    def test_native_verified_legacy_trust_migrates_and_removes_retired_keys(self):
        _, rows, old_state = self.trusted_legacy()
        with patch.object(context_mode_integration, "list_hooks", return_value=[{"hooks": rows}]):
            native_integrations.apply("context-mode", self.codex)
        receipt = native_integrations._read_receipt("context-mode")
        state = native_integrations._toml(self.codex / "config.toml")["hooks"]["state"]
        self.assertEqual(receipt["trust"], state)
        self.assertEqual(4, len(state))
        self.assertNotIn(f"{self.codex / 'hooks.json'}:pre_compact:0:0", state)
        self.assertNotIn(f"{self.codex / 'hooks.json'}:stop:0:0", state)

    def test_legacy_trust_mismatch_preserves_all_files(self):
        _, rows, _ = self.trusted_legacy()
        rows[0]["currentHash"] = "sha256:" + "c" * 64
        before = [(self.codex / name).read_bytes() for name in ["config.toml", "hooks.json"]]
        with patch.object(context_mode_integration, "list_hooks", return_value=[{"hooks": rows}]):
            with self.assertRaises(native_integrations.NativeIntegrationError):
                native_integrations.apply("context-mode", self.codex)
        self.assertEqual(before, [(self.codex / name).read_bytes() for name in ["config.toml", "hooks.json"]])

    def test_legacy_replacement_preserves_following_trusted_foreign_hook(self):
        _, rows, _ = self.trusted_legacy()
        path = self.codex / "hooks.json"
        document = json.loads(path.read_text())
        foreign = {"hooks": [{"type": "command", "command": "foreign-owner"}]}
        document["hooks"]["SessionStart"].append(foreign)
        path.write_text(json.dumps(document))
        import tomlkit
        config = self.codex / "config.toml"
        state = tomlkit.parse(config.read_text())
        key = f"{path}:session_start:1:0"
        state["hooks"]["state"][key] = {"trusted_hash": "sha256:" + "c" * 64}
        config.write_text(tomlkit.dumps(state))
        with patch.object(context_mode_integration, "list_hooks", return_value=[{"hooks": rows}]):
            native_integrations.apply("context-mode", self.codex)
        self.assertEqual(foreign, json.loads(path.read_text())["hooks"]["SessionStart"][1])
        self.assertEqual({"trusted_hash": "sha256:" + "c" * 64}, native_integrations._toml(config)["hooks"]["state"][key])
        before = (path.read_bytes(), config.read_bytes())
        with self.assertRaises(native_integrations.NativeIntegrationError):
            native_integrations.unconfigure("context-mode", self.codex)
        self.assertEqual(before, (path.read_bytes(), config.read_bytes()))

    def test_failed_trusted_legacy_migration_restores_original_trust(self):
        legacy, rows, state = self.trusted_legacy()
        with patch.object(context_mode_integration, "list_hooks", return_value=[{"hooks": rows}]), \
             patch.object(native_integrations, "_write_receipt", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(native_integrations.NativeIntegrationError, "已撤回"):
                native_integrations.apply("context-mode", self.codex)
        self.assertEqual(state, native_integrations._toml(self.codex / "config.toml")["hooks"]["state"])
        self.assertEqual(legacy, native_integrations._read_receipt("context-mode"))

    def test_unknown_command_skill_blocks_without_partial_configuration(self):
        skill = self.codex / "skills/ctx-stats/SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("external")
        before = (self.codex / "config.toml").read_bytes()
        with self.assertRaises(native_integrations.NativeIntegrationError):
            native_integrations.apply("context-mode", self.codex)
        self.assertEqual(before, (self.codex / "config.toml").read_bytes())
        self.assertEqual("external", skill.read_text())

    def test_receipt_removal_failure_restores_entire_v2_configuration(self):
        native_integrations.apply("context-mode", self.codex)
        receipt = native_integrations._read_receipt("context-mode")
        replace = native_integrations.owned_files.replace
        def fail_receipt(path, after, before):
            if path == native_integrations._receipt_path("context-mode") and after is None:
                raise OSError("disk full")
            return replace(path, after, before)
        with patch.object(native_integrations.owned_files, "replace", side_effect=fail_receipt):
            with self.assertRaisesRegex(native_integrations.NativeIntegrationError, "已恢复"):
                native_integrations.unconfigure("context-mode", self.codex)
        self.assertEqual(receipt, native_integrations._read_receipt("context-mode"))
        self.assertTrue(native_integrations._context_matches(self.codex, receipt))

    def test_trust_or_command_drift_is_never_overwritten(self):
        native_integrations.apply("context-mode", self.codex)
        skill = self.codex / "skills/ctx-stats/SKILL.md"
        original = skill.read_text()
        skill.write_text("external drift")
        with self.assertRaises(native_integrations.NativeIntegrationError):
            native_integrations.unconfigure("context-mode", self.codex)
        skill.write_text(original)
        path = self.codex / "config.toml"
        path.write_text(path.read_text().replace("sha256:" + "a" * 64, "sha256:" + "b" * 64))
        with self.assertRaises(native_integrations.NativeIntegrationError):
            native_integrations.apply("context-mode", self.codex)

    def test_invalid_hooks_or_wrong_bin_are_rejected(self):
        (self.codex / "hooks.json").write_text("{broken")
        with self.assertRaises(native_integrations.NativeIntegrationError):
            native_integrations.apply("context-mode", self.codex)
        (self.codex / "hooks.json").unlink()
        entry = self.home / ".local/bin/context-mode"
        entry.unlink()
        entry.write_text("external")
        with self.assertRaises(native_integrations.NativeIntegrationError):
            native_integrations.apply("context-mode", self.codex)

    def test_codegraph_retains_its_native_owner(self):
        self.assertEqual(native_integrations.apply("codegraph", self.codex), "changed")
        self.assertEqual(native_integrations.inspect("codegraph", self.codex, enabled=True)["state"], "current")
        self.assertEqual(native_integrations.unconfigure("codegraph", self.codex), "changed")
        self.assertIn('command = "other"', (self.codex / "config.toml").read_text())

    def test_codegraph_adoption_repairs_command_and_preserves_extensions(self):
        import tomllib
        path = self.codex / "config.toml"
        path.write_text(path.read_text() + '\n[mcp_servers.codegraph]\ncommand = "/usr/bin/codegraph"\nargs = ["serve", "--mcp"]\nenabled_tools = ["query"]\n[mcp_servers.codegraph.env]\nDO_NOT_TRACK = "1"\n')
        before = tomllib.loads(path.read_text())
        with self.assertRaises(native_integrations.NativeIntegrationError):
            native_integrations.apply("codegraph", self.codex)
        native_integrations.apply("codegraph", self.codex, adopt=True)
        server = tomllib.loads(path.read_text())["mcp_servers"]["codegraph"]
        self.assertEqual(server["command"], str(self.bin / "codegraph"))
        self.assertEqual(server["enabled_tools"], ["query"])
        self.assertEqual(server["env"], {"DO_NOT_TRACK": "1"})
        native_integrations.unconfigure("codegraph", self.codex)
        self.assertEqual(tomllib.loads(path.read_text()), before)

    def test_siyuan_registers_helper_and_retracts_only_its_fields(self):
        import tomllib
        helper = self.codex / "skills/pennix-skills/pennix-siyuan-knowledge/scripts/mcp_headers.py"
        helper.parent.mkdir(parents=True)
        helper.write_text('import json\nprint(json.dumps({"url": "https://notes.example.test/mcp"}))\n')
        path = self.codex / "config.toml"
        native_integrations.apply("siyuan", self.codex)
        server = tomllib.loads(path.read_text())["mcp_servers"]["siyuan"]
        self.assertEqual(server["url"], "https://notes.example.test/mcp")
        self.assertEqual(native_integrations.inspect("siyuan", self.codex, enabled=True)["state"], "current")
        path.write_text(path.read_text() + '\n[mcp_servers.siyuan.env]\nEXTRA = "keep"\n')
        native_integrations.unconfigure("siyuan", self.codex)
        self.assertEqual(tomllib.loads(path.read_text())["mcp_servers"]["siyuan"], {"env": {"EXTRA": "keep"}})


if __name__ == "__main__":
    unittest.main()
