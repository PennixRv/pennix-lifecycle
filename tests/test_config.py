from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import codex_config
import config_model
import _engine as owner_engine
import path_env
import relay_owner


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
            "# Pennix workflow.config format: 1\n", "# Pennix workflow.config format: 2\n", 1
        ), encoding="utf-8")
        with self.assertRaises(config_model.ConfigError):
            config_model.load()

    def test_duplicate_assignment_is_rejected(self) -> None:
        path = config_model.init()
        path.write_text(path.read_text(encoding="utf-8") + "CONFIG_PENNIX_CODEX_GOALS=y\n", encoding="utf-8")
        with self.assertRaises(config_model.ConfigError):
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
            "PENNIX_CODEX_REMOTE_COMPACTION": "y",
            "PENNIX_CODEX_HOOKS": "y",
            "PENNIX_CODEX_GOALS": "y",
            "PENNIX_CODEX_MEMORIES": "n",
            "PENNIX_CODEX_AGENTS_ENABLED": "n",
        }
        self.assertEqual(codex_config.apply(config, values), "changed")
        self.assertIn('custom = "preserve"', config.read_text(encoding="utf-8"))
        self.assertIn("custom = true", config.read_text(encoding="utf-8"))
        status = codex_config.inspect(config, values)
        self.assertEqual(status["state"], "current")
        self.assertNotIn("other", str(status))
        self.assertEqual(codex_config.uninstall(config), "changed")
        self.assertIn('custom = "preserve"', config.read_text(encoding="utf-8"))


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

    def test_relay_endpoint_requires_host_and_port(self) -> None:
        relay_owner.validate_config("relay.example:9999", "relay.example:9999")
        with self.assertRaises(relay_owner.RelayError):
            relay_owner.validate_config("https://relay.example:9999", "relay.example:9999")


if __name__ == "__main__":
    unittest.main()
