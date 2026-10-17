from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import codex_config
import config_model
import _engine as owner_engine
import path_env
import relay_owner
import pennix


class ConfigModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.old = {key: os.environ.get(key) for key in ("HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME")}
        os.environ["HOME"] = str(self.home)
        os.environ["XDG_CONFIG_HOME"] = str(self.home / "config")
        os.environ["XDG_STATE_HOME"] = str(self.home / "state")

    def tearDown(self) -> None:
        for key, value in self.old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temp.cleanup()

    def test_default_roundtrip_and_disabled_value_preservation(self) -> None:
        path = config_model.init()
        kconf, _ = config_model.load()
        kconf.syms["PENNIX_CODEX_HOME"].set_value(str(self.home / "codex"))
        kconf.syms["PENNIX_COMPONENT_FASTCTX"].set_value("n")
        config_model.write(kconf, path)
        loaded, _ = config_model.load()
        self.assertEqual(config_model.config_value(loaded, "PENNIX_CODEX_HOME"), str(self.home / "codex"))
        self.assertEqual(loaded.syms["PENNIX_COMPONENT_FASTCTX"].str_value, "n")

    def test_incompatible_header_is_rejected(self) -> None:
        path = config_model.init()
        path.write_text(path.read_text(encoding="utf-8").replace(
            "# Pennix workflow.config format: 2\n", "# Pennix workflow.config format: 1\n", 1
        ), encoding="utf-8")
        with self.assertRaises(config_model.ConfigError):
            config_model.load()

    def test_duplicate_assignment_is_rejected(self) -> None:
        path = config_model.init()
        path.write_text(path.read_text(encoding="utf-8") + "CONFIG_PENNIX_CODEX_GOALS=y\n", encoding="utf-8")
        with self.assertRaises(config_model.ConfigError):
            config_model.load()

    def test_choice_with_two_explicit_values_is_rejected(self) -> None:
        path = config_model.init()
        text = path.read_text(encoding="utf-8").replace(
            "# CONFIG_PENNIX_PASEO_AUTOSTART_LOGIN is not set\n", ""
        ).replace(
            "CONFIG_PENNIX_PASEO_AUTOSTART_OFF=y\n",
            "CONFIG_PENNIX_PASEO_AUTOSTART_OFF=y\nCONFIG_PENNIX_PASEO_AUTOSTART_LOGIN=y\n",
            1,
        )
        path.write_text(text, encoding="utf-8")
        with self.assertRaisesRegex(config_model.ConfigError, "choice"):
            config_model.load()

    def test_disabled_relay_fields_remain_editable(self) -> None:
        path = config_model.init()
        kconf, _ = config_model.load()
        kconf.syms["PENNIX_PASEO_RELAY"].set_value("y")
        kconf.syms["PENNIX_PASEO_RELAY_ENDPOINT"].set_value("relay.example:9999")
        config_model.write(kconf, path)
        self.assertEqual(config_model.values(kconf)["PENNIX_PASEO_RELAY_TLS"], "y")
        kconf, _ = config_model.load()
        kconf.syms["PENNIX_PASEO_RELAY"].set_value("n")
        config_model.write(kconf, path)
        kconf, _ = config_model.load()
        self.assertEqual(config_model.config_value(kconf, "PENNIX_PASEO_RELAY_ENDPOINT"), "relay.example:9999")

    def test_codex_writer_preserves_unowned_toml_and_redacts_status(self) -> None:
        config = self.home / "codex" / "config.toml"
        config.parent.mkdir(parents=True)
        config.write_text("custom = \"preserve\"\n[features]\ncustom = true\n[hooks]\nstate = {}\n[agents]\nother = true\n", encoding="utf-8")
        values = {
            "PENNIX_CODEX_PROVIDER_URL": "https://api.openai.com/v1",
            "PENNIX_CODEX_REMOTE_COMPACTION": "y",
            "PENNIX_CODEX_HOOKS": "y",
            "PENNIX_CODEX_GOALS": "y",
            "PENNIX_CODEX_MEMORIES": "n",
            "PENNIX_CODEX_AGENTS_ENABLED": "n",
        }
        self.assertEqual(codex_config.apply(config, values), "changed")
        self.assertIn('custom = "preserve"', config.read_text(encoding="utf-8"))
        self.assertIn("custom = true", config.read_text(encoding="utf-8"))
        self.assertNotIn("openai_base_url", config.read_text(encoding="utf-8"))
        self.assertIn('model_provider = "pennix"', config.read_text(encoding="utf-8"))
        status = codex_config.inspect(config, values)
        self.assertEqual(status["state"], "current")
        self.assertNotIn("other", str(status))
        self.assertEqual(codex_config.uninstall(config), "changed")
        self.assertIn('custom = "preserve"', config.read_text(encoding="utf-8"))

    def test_codex_writer_keeps_initial_baseline_across_reapply(self) -> None:
        config = self.home / "codex" / "config.toml"
        config.parent.mkdir(parents=True)
        config.write_text('model_provider = "user"\ncustom = "preserve"\n', encoding="utf-8")
        values = {
            "PENNIX_CODEX_PROVIDER_URL": "https://api.openai.com/v1",
            "PENNIX_CODEX_REMOTE_COMPACTION": "y",
            "PENNIX_CODEX_HOOKS": "y",
            "PENNIX_CODEX_GOALS": "y",
            "PENNIX_CODEX_MEMORIES": "n",
            "PENNIX_CODEX_AGENTS_ENABLED": "n",
        }
        changed = dict(values, PENNIX_CODEX_REMOTE_COMPACTION="n")
        self.assertEqual(codex_config.apply(config, values), "changed")
        self.assertEqual(codex_config.apply(config, changed), "changed")
        self.assertEqual(codex_config.uninstall(config), "changed")
        restored = config.read_text(encoding="utf-8")
        self.assertIn('model_provider = "user"', restored)
        self.assertIn('custom = "preserve"', restored)

    def test_codex_writer_rejects_drift_before_apply_or_uninstall(self) -> None:
        config = self.home / "codex" / "config.toml"
        config.parent.mkdir(parents=True)
        values = {
            "PENNIX_CODEX_PROVIDER_URL": "https://api.openai.com/v1",
            "PENNIX_CODEX_REMOTE_COMPACTION": "y",
            "PENNIX_CODEX_HOOKS": "y",
            "PENNIX_CODEX_GOALS": "y",
            "PENNIX_CODEX_MEMORIES": "n",
            "PENNIX_CODEX_AGENTS_ENABLED": "n",
        }
        codex_config.apply(config, values)
        config.write_text(config.read_text(encoding="utf-8").replace(
            'model_provider = "pennix"', 'model_provider = "user"', 1
        ), encoding="utf-8")
        with self.assertRaisesRegex(codex_config.CodexConfigError, "偏离"):
            codex_config.apply(config, values)
        with self.assertRaisesRegex(codex_config.CodexConfigError, "偏离"):
            codex_config.uninstall(config)

    def test_codex_writer_rejects_broken_symlink(self) -> None:
        config = self.home / "codex" / "config.toml"
        config.parent.mkdir(parents=True)
        config.symlink_to(self.home / "missing-config.toml")
        values = {
            "PENNIX_CODEX_PROVIDER_URL": "https://api.openai.com/v1",
            "PENNIX_CODEX_REMOTE_COMPACTION": "y",
            "PENNIX_CODEX_HOOKS": "y",
            "PENNIX_CODEX_GOALS": "y",
            "PENNIX_CODEX_MEMORIES": "n",
            "PENNIX_CODEX_AGENTS_ENABLED": "n",
        }
        with self.assertRaises(codex_config.CodexConfigError):
            codex_config.apply(config, values)

    def test_unknown_key_is_rejected_without_kconfiglib_shape_error(self) -> None:
        path = config_model.init()
        path.write_text(path.read_text(encoding="utf-8") + "CONFIG_UNKNOWN_KEY=y\n", encoding="utf-8")
        with self.assertRaisesRegex(config_model.ConfigError, "UNKNOWN_KEY"):
            config_model.load()

    def test_menuconfig_uses_installed_module_entrypoint(self) -> None:
        path = config_model.init()

        def fake_menuconfig(command, *, cwd, env, check):
            self.assertEqual(command[1:3], ["-m", "menuconfig"])
            self.assertEqual(env["KCONFIG_CONFIG"], str(Path(cwd) / ".config"))
            Path(env["KCONFIG_CONFIG"]).write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
            return subprocess.CompletedProcess(command, 0)

        with mock.patch.object(config_model.subprocess, "run", side_effect=fake_menuconfig):
            self.assertEqual(config_model.edit(), path)

    def test_enabled_connection_without_owner_inputs_is_blocked(self) -> None:
        config_model.init()
        kconf, _ = config_model.load()
        kconf.syms["PENNIX_TARGET_CCH"].set_value("y")
        plans = pennix._connection_plans(kconf)
        self.assertEqual(plans["integration-cch"]["state"], "blocked")
        self.assertIn("endpoint:PENNIX_CCH_URL", plans["integration-cch"]["detail"])


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_plan_does_not_print_nested_codex_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env = {**os.environ, "XDG_CONFIG_HOME": str(Path(directory) / "config"), "XDG_STATE_HOME": str(Path(directory) / "state")}
            root = Path(__file__).resolve().parents[1]
            subprocess.run([str(root / "pennix"), "config", "init"], cwd=root, env=env, check=True, capture_output=True, text=True)
            result = subprocess.run([str(root / "pennix"), "plan", "--json"], cwd=root, env=env, check=True, capture_output=True, text=True)
            self.assertNotIn("trusted_hash", result.stdout)
            self.assertNotIn("generate_memories", result.stdout)

    def test_path_injection_is_idempotent(self) -> None:
        zshrc = self.home / ".zshrc"
        zshrc.write_text('path=("/usr/bin" $path)\n', encoding="utf-8")
        original_home = Path.home
        Path.home = lambda: self.home  # type: ignore[method-assign]
        try:
            self.assertEqual(path_env.apply(str(self.home / ".local")), "changed")
            self.assertEqual(path_env.apply(str(self.home / ".local")), "no-op")
            self.assertEqual(zshrc.read_text(encoding="utf-8").count(path_env.BEGIN), 1)
            self.assertEqual(path_env.remove(), "changed")
        finally:
            Path.home = original_home  # type: ignore[method-assign]

    def test_external_collection_is_preserved(self) -> None:
        catalog = owner_engine.load_catalog(Path(__file__).resolve().parents[1] / "references" / "catalog.json")
        component = catalog["components"]["pennix-skills-extra"]
        destination = self.home / "codex" / "skills" / "pennix-skills-extra"
        for name in component["collection_contract"]["skills"]:
            (destination / name).mkdir(parents=True, exist_ok=True)
        self.assertFalse(owner_engine.collection_owned(component, self.home / "codex"))
        self.assertEqual(owner_engine._operate_collection(component, self.home / "codex", "uninstall"), "preserved")
        self.assertTrue(destination.is_dir())

    def test_pennix_skills_native_owner_is_never_scheduled_for_lifecycle_mutation(self) -> None:
        catalog = owner_engine.load_catalog(Path(__file__).resolve().parents[1] / "references" / "catalog.json")
        component = catalog["components"]["pennix-skills"]
        self.assertEqual(pennix._planned_action(component, "install"), ("native-owner", "install"))
        self.assertEqual(pennix._planned_action(component, "uninstall"), ("native-owner", "uninstall"))
        self.assertNotIn("pennix-skills", pennix._reverse_uninstall_order(catalog))
        with self.assertRaises(pennix.CliError):
            pennix._run_component(None, catalog, "pennix-skills", "install")

    def test_relay_endpoint_requires_host_and_port(self) -> None:
        relay_owner.validate_config("relay.example:9999", "relay.example:9999")
        with self.assertRaises(relay_owner.RelayError):
            relay_owner.validate_config("https://relay.example:9999", "relay.example:9999")

    def test_secrets_path_resolves_user_paths(self) -> None:
        previous = os.environ.get("HOME")
        os.environ["HOME"] = str(self.home)
        try:
            self.assertEqual(pennix._secrets_path("$HOME/private/secrets.toml"), self.home / "private/secrets.toml")
            with self.assertRaises(pennix.CliError):
                pennix._secrets_path("relative/secrets.toml")
        finally:
            if previous is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = previous


if __name__ == "__main__":
    unittest.main()
