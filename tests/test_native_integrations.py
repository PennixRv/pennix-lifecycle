from pathlib import Path
import json
import os
import tempfile
import unittest
from unittest.mock import patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import native_integrations
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
    content = config.read_text() if config.exists() else ""
    marker = "\n[mcp_servers.context-mode]\n"
    if args[:3] == ["mcp", "add", "context-mode"]:
        environment = [args[i + 1].split("=", 1) for i, arg in enumerate(args) if arg == "--env"]
        content += marker + "command = " + json.dumps(args[-1]) + "\n[mcp_servers.context-mode.env]\n"
        content += "".join(key + " = " + json.dumps(value) + "\n" for key, value in environment)
    elif args == ["mcp", "remove", "context-mode"]:
        content = content.split(marker)[0]
    else:
        sys.exit(2)
    config.write_text(content)
else:
    if args[0] == "install":
        config.write_text('[mcp_servers.codegraph]\ncommand = "codegraph"\nargs = ["serve", "--mcp"]\n')
    else:
        config.write_text('[mcp_servers.other]\ncommand = "other"\n')
'''


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
            "HOME": str(self.home), "PATH": f"{self.bin}:{os.environ['PATH']}",
            "XDG_STATE_HOME": str(self.root / "state"), "CODEX_HOME": str(self.codex),
        })
        self.environment.start()
        self.codex.mkdir(parents=True)
        (self.codex / "config.toml").write_text('[features]\nhooks = true\n[mcp_servers.other]\ncommand = "other"\n')
        self.package = self.home / ".local/lib/node_modules/@pennixrv/context-mode"
        (self.package / "configs/codex").mkdir(parents=True)
        (self.package / "package.json").write_text(json.dumps({"name": "@pennixrv/context-mode", "version": "1.0.0"}))
        (self.package / "cli.bundle.mjs").write_text("// fixture\n")
        hook_config = {"hooks": {event: [{"hooks": [{"type": "command", "command": f"context-mode hook codex {event.lower()}"}]}]
                                 for event in native_integrations.EVENTS}}
        (self.package / "configs/codex/hooks.json").write_text(json.dumps(hook_config))
        entry = self.home / ".local/bin/context-mode"
        entry.parent.mkdir(parents=True)
        entry.symlink_to(self.package / "cli.bundle.mjs")

    def tearDown(self):
        self.environment.stop()
        self.temp.cleanup()

    def test_install_upgrade_idempotence_and_uninstall_preserve_other_owners(self):
        other = {"extra": "preserve", "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "trellis hook"}]}]}}
        (self.codex / "hooks.json").write_text(json.dumps(other))
        self.assertEqual(native_integrations.inspect("context-mode", self.codex, enabled=True)["state"], "absent")
        self.assertEqual(native_integrations.apply("context-mode", self.codex), "changed")
        self.assertEqual(native_integrations.apply("context-mode", self.codex), "no-op")
        (self.package / "package.json").write_text(json.dumps({"name": "@pennixrv/context-mode", "version": "1.1.0"}))
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
        path.write_text(path.read_text().replace("hook codex stop", "hook codex different"))
        for operation in (native_integrations.apply, native_integrations.unconfigure):
            with self.assertRaises(native_integrations.NativeIntegrationError):
                operation("context-mode", self.codex)
        self.assertTrue(native_integrations._receipt_path("context-mode").exists())

    def test_receipt_failure_retracts_only_this_installation(self):
        before = (self.codex / "config.toml").read_text()
        with patch.object(native_integrations, "_write_receipt", side_effect=OSError("disk full")):
            with self.assertRaises(native_integrations.NativeIntegrationError):
                native_integrations.apply("context-mode", self.codex)
        self.assertEqual((self.codex / "config.toml").read_text(), before)
        self.assertFalse((self.codex / "hooks.json").exists())

    def test_unconfigure_uses_receipt_even_if_package_is_missing(self):
        native_integrations.apply("context-mode", self.codex)
        (self.package / "package.json").unlink()
        native_integrations.unconfigure("context-mode", self.codex)
        self.assertIsNone(native_integrations._server("context-mode", self.codex))

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
