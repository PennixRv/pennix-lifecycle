from __future__ import annotations

import json
import os
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import codex_config
import config_model
import credential_import
import _engine as owner_engine
import path_env
import relay_owner
import paseo_service
import secret_store


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
        kconf.syms["PENNIX_COMPONENT_CONTEXT_MODE"].set_value("n")
        config_model.write(kconf, path)
        loaded, _ = config_model.load()
        self.assertEqual(config_model.config_value(loaded, "PENNIX_CODEX_HOME"), str(self.home / "codex"))
        self.assertEqual(loaded.syms["PENNIX_COMPONENT_CONTEXT_MODE"].str_value, "n")

    def test_incompatible_header_is_rejected(self) -> None:
        path = config_model.init()
        path.write_text(path.read_text(encoding="utf-8").replace(
            config_model.HEADER, "# Pennix workflow.config format: 1\n", 1
        ), encoding="utf-8")
        with self.assertRaises(config_model.ConfigError):
            config_model.load()

    def test_wsl_defconfig_is_a_portable_complete_input(self) -> None:
        path = config_model.init(
            str(self.home / "workflow.config"), force=True,
            defconfig=str(Path(__file__).resolve().parents[1] / "config/defconfig-wsl"),
        )
        loaded, _ = config_model.load(str(path))
        self.assertGreater(len(config_model.values(loaded)), 100)
        self.assertEqual(config_model.config_value(loaded, "PENNIX_CODEX_HOME"), str(self.home / ".codex"))
        self.assertEqual(config_model.config_value(loaded, "PENNIX_PASEO_HOME"), str(self.home / ".paseo"))
        self.assertEqual(config_model.config_value(loaded, "PENNIX_PASEO_RELAY"), "n")

    def test_catalog_does_not_claim_cch_is_a_public_npm_package(self) -> None:
        catalog = owner_engine.load_catalog(Path(__file__).resolve().parents[1] / "references/catalog.json")
        cch = catalog["components"]["cch-status"]
        self.assertEqual(cch["delivery"], "native")
        self.assertNotIn("package", cch)
        self.assertIn("not published", cch["native_owner_reason"])

    def test_agents_template_upgrade_requires_unchanged_owned_bytes(self) -> None:
        target = self.home / "codex/AGENTS.md"
        component = {"template": {"name": "AGENTS.md.install"}}
        with mock.patch.object(owner_engine, "_template", return_value="first\n"):
            owner_engine._operate_static("codex-agents", target.parent, component, "install")
        with mock.patch.object(owner_engine, "_template", return_value="second\n"):
            self.assertEqual(owner_engine._static_state("codex-agents", target.parent, component), "outdated")
            owner_engine._operate_static("codex-agents", target.parent, component, "upgrade")
            self.assertEqual(target.read_text(), "second\n")
            target.write_text("user changed\n")
            with self.assertRaises(owner_engine.BootstrapError):
                owner_engine._operate_static("codex-agents", target.parent, component, "upgrade")
            self.assertEqual(target.read_text(), "user changed\n")

    def test_agents_explicit_review_digest_rejects_changes(self) -> None:
        import hashlib
        target = self.home / "codex/AGENTS.md"
        target.parent.mkdir()
        target.write_bytes(b"reviewed user rules\n")
        reviewed = hashlib.sha256(target.read_bytes()).hexdigest()
        component = {"template": {"name": "AGENTS.md.install"}}
        with mock.patch.object(owner_engine, "_template", return_value="new rules\n"):
            with self.assertRaises(owner_engine.BootstrapError):
                owner_engine._operate_static("codex-agents", target.parent, component, "upgrade", "0" * 64)
            self.assertEqual(target.read_bytes(), b"reviewed user rules\n")
            owner_engine._operate_static("codex-agents", target.parent, component, "upgrade", reviewed)
        self.assertEqual(target.read_bytes(), b"new rules\n")

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
        kconf, _ = config_model.load(str(config_model.init()))
        values = config_model.values(kconf)
        values.update({"PENNIX_CODEX_REMOTE_COMPACTION": "y", "PENNIX_CODEX_HOOKS": "y",
                       "PENNIX_CODEX_GOALS": "y", "PENNIX_CODEX_MEMORIES_NATIVE": "y",
                       "PENNIX_CODEX_AGENTS_NATIVE": "y"})
        self.assertEqual(codex_config.apply(config, values), "changed")
        self.assertIn('custom = "preserve"', config.read_text(encoding="utf-8"))
        self.assertIn("custom = true", config.read_text(encoding="utf-8"))
        status = codex_config.inspect(config, values)
        self.assertEqual(status["state"], "current")
        self.assertNotIn("other", str(status))
        self.assertEqual(codex_config.uninstall(config), "changed")
        self.assertIn('custom = "preserve"', config.read_text(encoding="utf-8"))

    def test_codex_config_keeps_original_baseline_and_refuses_drift(self) -> None:
        config = self.home / "codex" / "config.toml"
        config.parent.mkdir(parents=True)
        config.write_text("[features]\ngoals = false\ncustom = true\n", encoding="utf-8")
        config_path = config_model.init()
        kconf, _ = config_model.load(str(config_path))
        values = config_model.values(kconf)
        self.assertEqual("changed", codex_config.apply(config, values))
        state = self.home / "state/pennix-lifecycle/codex-config.json"
        first = state.read_bytes()
        self.assertEqual("no-op", codex_config.apply(config, values))
        self.assertEqual(first, state.read_bytes())
        config.write_text(config.read_text().replace("goals = true", "goals = false") + "outside = true\n", encoding="utf-8")
        with self.assertRaises(codex_config.CodexConfigError):
            codex_config.uninstall(config)
        self.assertIn("outside = true", config.read_text())

    def test_codex_custom_url_key_uses_native_auth_command(self) -> None:
        kconf = config_model._new()
        kconf.syms["PENNIX_CODEX_PROVIDER_CUSTOM"].set_value("y")
        kconf.syms["PENNIX_CODEX_AUTH_API_KEY"].set_value("y")
        kconf.syms["PENNIX_CODEX_PROVIDER_URL"].set_value("https://proxy.example/v1")
        expected = codex_config.expected(config_model.values(kconf))
        self.assertEqual(expected["model_provider"], "custom")
        self.assertEqual(expected["model_providers.custom.base_url"], "https://proxy.example/v1")
        self.assertNotIn("model_providers.custom.requires_openai_auth", expected)
        self.assertEqual(expected["model_providers.custom.auth.command"], str(Path.home() / ".local/bin/pennix"))
        self.assertEqual(expected["model_providers.custom.auth.args"][-2:], ["--secrets-file", str(self.home / ".config/pennix/secrets.toml")])

    def test_codex_provider_rejects_credentials_in_base_url(self) -> None:
        kconf = config_model._new()
        kconf.syms["PENNIX_CODEX_PROVIDER_CUSTOM"].set_value("y")
        kconf.syms["PENNIX_CODEX_PROVIDER_URL"].set_value("https://user:pass@proxy.example/v1")
        with self.assertRaises(codex_config.CodexConfigError):
            codex_config.expected(config_model.values(kconf))

    def test_secrets_require_private_file_and_known_keys(self) -> None:
        directory = self.home / "private"
        directory.mkdir(mode=0o700)
        secrets = directory / "secrets.toml"
        secrets.write_text('[codex]\napi_key = "test-only-key"\n', encoding="utf-8")
        secrets.chmod(0o600)
        self.assertEqual(secret_store.get(secrets, "codex", "api_key"), "test-only-key")
        secrets.chmod(0o644)
        with self.assertRaises(secret_store.SecretError):
            secret_store.load(secrets)

    def test_secrets_reject_ordinary_endpoint_fields(self) -> None:
        directory = self.home / "private"
        directory.mkdir(mode=0o700)
        secrets = directory / "secrets.toml"
        secrets.write_text('[siyuan]\nendpoint = "https://example.invalid"\n', encoding="utf-8")
        secrets.chmod(0o600)
        with self.assertRaises(secret_store.SecretError):
            secret_store.load(secrets)

    def test_ssh_credential_import_reads_only_fixed_sources(self) -> None:
        directory = self.home / "private"
        directory.mkdir(mode=0o700)
        secrets = directory / "secrets.toml"
        fixtures = {
            ".codex/auth.json": b'{"OPENAI_API_KEY":"codex-test-key"}',
            ".config/grok-search/config.json": b'{"apiKey":"grok-test-key","tavilyApiKey":"tavily-test-key","baseUrl":"https://example.invalid"}',
        }
        calls = []

        def fetch(service: str, remote_path: str) -> bytes:
            calls.append((service, remote_path))
            return fixtures[remote_path]

        updated = credential_import.import_from_ssh(
            secrets, "fixture", ["codex", "grok"], fetch=fetch
        )
        self.assertEqual(updated, {"codex.api_key", "grok.api_key", "grok.tavily_api_key"})
        self.assertEqual(secret_store.get(secrets, "codex", "api_key"), "codex-test-key")
        self.assertEqual(secret_store.get(secrets, "grok", "api_key"), "grok-test-key")
        self.assertEqual(calls, [("codex", ".codex/auth.json"), ("grok", ".config/grok-search/config.json")])
        self.assertNotIn("baseUrl", secret_store.load(secrets).get("grok", {}))
        with self.assertRaises(secret_store.SecretError):
            credential_import.import_from_ssh(
                secrets, "fixture", ["codex"],
                fetch=lambda _service, _path: b'{"OPENAI_API_KEY":"different-key"}',
            )

    def test_paseo_unit_quotes_paths_with_spaces_and_specifiers(self) -> None:
        unit = paseo_service._unit(Path("/home/penn/.local/bin/paseo"), Path("/home/penn/Paseo %home"))
        self.assertIn('ExecStart=/home/penn/.local/bin/paseo daemon run --home "/home/penn/Paseo %%home"', unit)

    def test_paseo_unit_requires_lifecycle_receipt(self) -> None:
        paseo = self.home / "bin/paseo"
        home = self.home / "paseo"
        path = paseo_service.unit_path()
        path.parent.mkdir(parents=True)
        path.write_text(paseo_service._unit(paseo, home), encoding="utf-8")
        self.assertEqual(paseo_service.inspect(paseo, home), "external")
        paseo_service._write_receipt(paseo, home)
        self.assertEqual(paseo_service.inspect(paseo, home), "current")
        path.write_text("[Service]\n", encoding="utf-8")
        self.assertEqual(paseo_service.inspect(paseo, home), "drifted")

    def test_relay_daemon_config_restores_partial_write(self) -> None:
        paseo = self.home / "bin/paseo"
        paseo.parent.mkdir()
        paseo.write_text("#!/bin/sh\n", encoding="utf-8")
        state = {}

        def run(command, **_kwargs):
            if "get" in command:
                key = command[command.index("get") + 1]
                return SimpleNamespace(returncode=0, stdout='{"set": %s, "value": %s}' % (
                    "true" if key in state else "false", repr(state.get(key)).replace("'", '"') if key in state else "null"
                ), stderr="")
            if "set" in command:
                key = command[command.index("set") + 1]
                if key == "daemon.relay.publicEndpoint":
                    return SimpleNamespace(returncode=1, stdout="", stderr="failure")
                value = command[command.index("set") + 2]
                state[key] = value if "--string" in command else value == "true"
                return SimpleNamespace(returncode=0, stdout="{}", stderr="")
            key = command[command.index("unset") + 1]
            state.pop(key, None)
            return SimpleNamespace(returncode=0, stdout="{}", stderr="")

        with mock.patch.object(relay_owner.subprocess, "run", side_effect=run):
            with self.assertRaises(relay_owner.RelayError) as error:
                relay_owner.configure_daemon(paseo, self.home / "paseo", "relay:1", "public:2", True, False)
        self.assertIn("已恢复", str(error.exception))
        self.assertEqual(state, {})

    def test_relay_daemon_config_verifies_readback_before_activation(self) -> None:
        paseo = self.home / "bin/paseo"
        paseo.parent.mkdir()
        paseo.write_text("#!/bin/sh\n", encoding="utf-8")
        state = {}

        def run(command, **_kwargs):
            if "get" in command:
                key = command[command.index("get") + 1]
                value = state.get(key)
                encoded = "null" if key not in state else ("true" if value is True else "false" if value is False else f'"{value}"')
                return SimpleNamespace(returncode=0, stdout=f'{{"set": {str(key in state).lower()}, "value": {encoded}}}', stderr="")
            if "set" in command:
                key = command[command.index("set") + 1]
                value = command[command.index("set") + 2]
                state[key] = value if "--string" in command else value == "true"
                return SimpleNamespace(returncode=0, stdout="{}", stderr="")
            key = command[command.index("unset") + 1]
            state.pop(key, None)
            return SimpleNamespace(returncode=0, stdout="{}", stderr="")

        with mock.patch.object(relay_owner.subprocess, "run", side_effect=run):
            self.assertEqual(
                relay_owner.configure_daemon(paseo, self.home / "paseo", "relay:1", "public:2", True, False),
                "changed",
            )
        self.assertTrue(state["daemon.relay.enabled"])


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

    def test_disabled_native_components_are_not_reported_as_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(__file__).resolve().parents[1]
            workflow = Path(directory) / "workflow.config"
            env = {
                **os.environ,
                "HOME": directory,
                "PATH": os.environ["PATH"],
                "XDG_CONFIG_HOME": str(Path(directory) / "config"),
                "XDG_STATE_HOME": str(Path(directory) / "state"),
            }
            subprocess.run(
                [str(root / "pennix"), "config", "init", "--config", str(workflow)],
                cwd=root, env=env, check=True, capture_output=True, text=True,
            )
            result = subprocess.run(
                [str(root / "pennix"), "plan", "--config", str(workflow), "--json"],
                cwd=root, env=env, check=True, capture_output=True, text=True,
            )
            self.assertEqual(json.loads(result.stdout)["components"]["cch-status"]["action"], "keep")

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

    def test_path_with_shell_metacharacters_is_quoted_and_uninstall_keeps_it(self) -> None:
        zshrc = self.home / ".zshrc"
        original_home = Path.home
        Path.home = lambda: self.home  # type: ignore[method-assign]
        try:
            prefix = self.home / "npm $(touch nope) 'quoted'"
            path_env.apply(str(prefix))
            content = zshrc.read_text(encoding="utf-8")
            expected = f"path=({shlex.quote(str(prefix / 'bin'))} $path)"
            self.assertIn(expected, content)
            self.assertFalse((self.home / "nope").exists())
            self.assertEqual(path_env.apply(str(prefix)), "no-op")
        finally:
            Path.home = original_home  # type: ignore[method-assign]

    def test_external_collection_is_preserved(self) -> None:
        catalog = owner_engine.load_catalog(Path(__file__).resolve().parents[1] / "references" / "catalog.json")
        component = catalog["components"]["pennix-skills-extra"]
        destination = self.home / "codex" / "skills" / "pennix-skills-extra"
        destination.mkdir(parents=True)
        (destination / "unregistered-skill").mkdir()
        self.assertFalse(owner_engine.collection_owned(component, self.home / "codex"))
        self.assertEqual(owner_engine._operate_collection(component, self.home / "codex", "uninstall"), "preserved")
        self.assertTrue(destination.is_dir())

    def test_relay_endpoint_requires_host_and_port(self) -> None:
        relay_owner.validate_config("relay.example:9999", "relay.example:9999")
        with self.assertRaises(relay_owner.RelayError):
            relay_owner.validate_config("https://relay.example:9999", "relay.example:9999")


if __name__ == "__main__":
    unittest.main()
