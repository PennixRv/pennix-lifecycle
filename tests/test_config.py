from __future__ import annotations

import hashlib
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

PASEO_SKILL_NAMES = ["paseo", "paseo-advisor", "future-native-skill"]


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

    def test_omitted_disabled_components_remain_disabled_without_rewriting_input(self) -> None:
        path = config_model.init()
        loaded, _ = config_model.load()
        omitted = {symbol.name for symbol in loaded.unique_defined_syms
                   if symbol.name.startswith("PENNIX_COMPONENT_")
                   and symbol.visibility and symbol.str_value == "n"}
        self.assertIn("PENNIX_COMPONENT_TRELLIS_PASEO_ADAPTER", omitted)
        raw = "\n".join(line for line in path.read_text().splitlines()
                        if not any(f"CONFIG_{name}" in line for name in omitted)) + "\n"
        path.write_text(raw)
        loaded, _ = config_model.load()
        self.assertTrue(all(loaded.syms[name].str_value == "n" for name in omitted))
        self.assertEqual(path.read_text(), raw)

    def test_wsl_defconfig_is_a_portable_complete_input(self) -> None:
        path = config_model.init(
            str(self.home / "workflow.config"), force=True,
            defconfig=str(Path(__file__).resolve().parents[1] / "config/defconfig-wsl"),
        )
        loaded, _ = config_model.load(str(path))
        raw = config_model._parse_assignments((Path(__file__).resolve().parents[1] / "config/defconfig-wsl").read_text(), loaded)
        public = {symbol.name for symbol in loaded.unique_defined_syms if symbol.visibility}
        self.assertFalse(public - raw.keys(), f"defconfig 缺少公开字段: {sorted(public - raw.keys())}")
        catalog = owner_engine.load_catalog(Path(__file__).resolve().parents[1] / "references/catalog.json")
        declared = {symbol.name.removeprefix("PENNIX_COMPONENT_").lower().replace("_", "-")
                    for symbol in loaded.unique_defined_syms if symbol.name.startswith("PENNIX_COMPONENT_")}
        self.assertEqual(declared, {key for key, item in catalog["components"].items() if item["delivery"] != "state"})
        self.assertEqual(config_model.config_value(loaded, "PENNIX_CODEX_HOME"), str(self.home / ".codex"))
        self.assertEqual(config_model.config_value(loaded, "PENNIX_PASEO_HOME"), str(self.home / ".paseo"))
        self.assertEqual(config_model.config_value(loaded, "PENNIX_PASEO_RUN"), "n")
        self.assertEqual(config_model.choice_value(loaded, "PENNIX_PASEO_AUTOSTART_"), "login")
        self.assertEqual(config_model.config_value(loaded, "PENNIX_PASEO_RELAY"), "n")

    def test_catalog_does_not_claim_cch_is_a_public_npm_package(self) -> None:
        catalog = owner_engine.load_catalog(Path(__file__).resolve().parents[1] / "references/catalog.json")
        cch = catalog["components"]["cch-status"]
        self.assertEqual(cch["delivery"], "package")
        self.assertTrue(cch["package"]["asset"]["private"])
        self.assertEqual(cch["package"]["asset"]["repo"], "PennixRv/cch-codex-tmux-status")
        self.assertNotIn("registry", cch["package"])

    def test_context_mode_catalog_uses_beta_and_declares_only_legacy_lock_source(self) -> None:
        catalog = owner_engine.load_catalog(Path(__file__).resolve().parents[1] / "references/catalog.json")
        component = catalog["components"]["context-mode"]
        self.assertEqual(component["lock_source"], "npm:@pennixrv/context-mode:beta")
        self.assertEqual(component["lock_source_aliases"], ["npm:@pennixrv/context-mode:latest"])
        self.assertEqual(component["package"]["tag"], "beta")

    def test_trellis_uses_beta_with_legacy_latest_lock_alias(self) -> None:
        catalog = owner_engine.load_catalog(Path(__file__).resolve().parents[1] / "references/catalog.json")
        component = catalog["components"]["trellis-cli"]
        self.assertEqual(component["lock_source"], "npm:@pennixrv/trellis:beta")
        self.assertEqual(component["lock_source_aliases"], ["npm:@pennixrv/trellis:latest"])
        self.assertEqual(component["package"]["tag"], "beta")

    def test_catalog_rejects_invalid_lock_source_aliases(self) -> None:
        path = self.home / "catalog.json"
        for aliases in ([], [""], ["npm:example:beta"], ["legacy", "legacy"], "legacy"):
            catalog = {
                "schema": 3,
                "components": {"package": {
                    "delivery": "package",
                    "lock_source": "npm:example:beta",
                    "lock_source_aliases": aliases,
                    "actions": {},
                }},
                "configuration_targets": {},
            }
            path.write_text(json.dumps(catalog), encoding="utf-8")
            with self.subTest(aliases=aliases), self.assertRaises(owner_engine.BootstrapError):
                owner_engine.load_catalog(path)

    def test_retired_search_has_no_install_or_connection_registration(self) -> None:
        catalog = owner_engine.load_catalog(Path(__file__).resolve().parents[1] / "references/catalog.json")
        self.assertTrue({"tavily-cli", "tavily-hikari"}.isdisjoint(catalog["components"]))
        self.assertTrue({"grok", "grok-tavily", "hikari"}.isdisjoint(catalog["configuration_targets"]))
        kconf, _ = config_model.load(str(config_model.init()))
        self.assertEqual(config_model.config_value(kconf, "PENNIX_CODEX_WEB_SEARCH_LIVE"), "y")
        self.assertNotIn("PENNIX_TARGET_GROK", kconf.syms)

    def test_catalog_has_unique_dynamic_sources_and_unpinned_static_templates(self) -> None:
        catalog = owner_engine.load_catalog(Path(__file__).resolve().parents[1] / "references/catalog.json")
        dynamic = {key: value for key, value in catalog["components"].items()
                   if value["delivery"] in {"package", "plugin", "client", "collection"}}
        self.assertEqual(len(dynamic), 24)
        self.assertEqual(len({value["lock_source"] for value in dynamic.values()}), len(dynamic))
        for component in catalog["components"].values():
            if component.get("delivery") == "static" and "template" in component:
                self.assertNotIn("revision", component["template"])
                self.assertTrue(owner_engine._template(component))

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

    def test_reviewed_release_preserves_restored_provider_and_rejects_other_drift(self) -> None:
        config = self.home / "codex/config.toml"
        config.parent.mkdir()
        original = 'model_provider = "OpenAI"\n[model_providers.OpenAI]\nname = "OpenAI"\nbase_url = "https://proxy.example/v1"\ncapabilities.remote_compaction = "v2"\n'
        config.write_text(original)
        kconf = config_model._new()
        kconf.syms["PENNIX_CODEX_PROVIDER_CUSTOM"].set_value("y")
        kconf.syms["PENNIX_CODEX_PROVIDER_ID"].set_value("cchp")
        kconf.syms["PENNIX_CODEX_PROVIDER_URL"].set_value("https://proxy.example/v1")
        codex_config.apply(config, config_model.values(kconf))
        from tomlkit import parse
        doc = parse(config.read_text())
        doc['model_provider'] = 'OpenAI'
        del doc['model_providers']['cchp']
        config.write_text(doc.as_string())
        reviewed_bytes = config.read_bytes()
        digest = hashlib.sha256(reviewed_bytes).hexdigest()
        with self.assertRaises(codex_config.CodexConfigError):
            codex_config.apply(config, config_model.values(kconf), digest)
        kconf.syms["PENNIX_CODEX_PROVIDER_PRESERVE"].set_value("y")
        values = config_model.values(kconf)
        with self.assertRaises(codex_config.CodexConfigError):
            codex_config.apply(config, values)
        with self.assertRaises(codex_config.CodexConfigError):
            codex_config.apply(config, values, '0' * 64)
        doc['model_provider'] = 'Other'
        config.write_text(doc.as_string())
        with self.assertRaises(codex_config.CodexConfigError):
            codex_config.apply(config, values, hashlib.sha256(config.read_bytes()).hexdigest())
        doc['model_provider'] = 'OpenAI'
        doc['features']['goals'] = False
        config.write_text(doc.as_string())
        with self.assertRaises(codex_config.CodexConfigError):
            codex_config.apply(config, values, hashlib.sha256(config.read_bytes()).hexdigest())
        config.write_bytes(reviewed_bytes)
        self.assertEqual('changed', codex_config.apply(config, values, digest))
        self.assertEqual(config.read_bytes(), reviewed_bytes)
        self.assertEqual('current', codex_config.inspect(config, values)['state'])
        self.assertNotIn('model_provider', codex_config.inspect(config, values)['owned_fields'])
        self.assertEqual('no-op', codex_config.apply(config, values))

    def test_reviewed_release_requires_existing_receipt(self) -> None:
        config = self.home / 'codex/config.toml'
        config.parent.mkdir()
        config.write_text('model_provider = "OpenAI"\n')
        original = config.read_bytes()
        with self.assertRaises(codex_config.CodexConfigError):
            codex_config.apply(config, config_model.values(config_model._new()), hashlib.sha256(original).hexdigest())
        self.assertEqual(original, config.read_bytes())

    def test_retired_feature_migrates_only_owned_values(self) -> None:
        config = self.home / 'codex/config.toml'
        config.parent.mkdir()
        config.write_text('[features]\nremote_compaction_v2 = true\n')
        values = config_model.values(config_model._new())
        codex_config.apply(config, values)
        self.assertIn('remote_compaction_v2', config.read_text())
        state = self.home / 'state/pennix-lifecycle/codex-config.json'
        record = json.loads(state.read_text())
        record['before']['features.remote_compaction_v2'] = True
        record['after']['features.remote_compaction_v2'] = True
        state.write_text(json.dumps(record))
        self.assertEqual('pending', codex_config.inspect(config, values)['state'])
        codex_config.apply(config, values)
        self.assertNotIn('remote_compaction_v2', config.read_text())
        self.assertNotIn('features.remote_compaction_v2', json.loads(state.read_text())['after'])
        codex_config.uninstall(config)
        self.assertNotIn('remote_compaction_v2', config.read_text())

    def test_custom_provider_search_capability_follows_explicit_search_and_restores_baseline(self):
        config = self.home / "codex/config.toml"
        config.parent.mkdir()
        config.write_text('model_provider="custom"\n[model_providers.custom]\nname="keep"\nsupports_standalone_web_search=false\n')
        kconf = config_model._new()
        kconf.syms["PENNIX_CODEX_PROVIDER_CUSTOM"].set_value("y")
        kconf.syms["PENNIX_CODEX_PROVIDER_URL"].set_value("https://proxy.example/v1")
        key = "model_providers.custom.supports_standalone_web_search"
        for mode in ["LIVE", "CACHED"]:
            kconf.syms["PENNIX_CODEX_WEB_SEARCH_" + mode].set_value("y")
            values = config_model.values(kconf)
            self.assertIs(codex_config.expected(values)[key], True)
            codex_config.apply(config, values)
            self.assertEqual("no-op", codex_config.apply(config, values))
        for mode in ["DISABLED", "NATIVE"]:
            kconf.syms["PENNIX_CODEX_WEB_SEARCH_" + mode].set_value("y")
            values = config_model.values(kconf)
            self.assertNotIn(key, codex_config.expected(values))
            codex_config.apply(config, values)
            from tomlkit import parse
            self.assertIs(parse(config.read_text())["model_providers"]["custom"]["supports_standalone_web_search"], False)
        codex_config.uninstall(config)
        self.assertEqual(parse(config.read_text())["model_providers"]["custom"]["name"], "keep")

    def test_native_web_search_is_explicit_idempotent_and_preserves_baseline(self) -> None:
        config = self.home / "codex/config.toml"
        config.parent.mkdir()
        config.write_text('web_search="disabled"\nmodel="keep-model"\n')
        kconf = config_model._new()
        kconf.syms["PENNIX_CODEX_WEB_SEARCH_NATIVE"].set_value("y")
        self.assertNotIn("web_search", codex_config.expected(config_model.values(kconf)))
        for mode in ["LIVE", "CACHED", "DISABLED"]:
            kconf.syms["PENNIX_CODEX_WEB_SEARCH_" + mode].set_value("y")
            values = config_model.values(kconf)
            codex_config.apply(config, values)
            from tomlkit import parse
            document = parse(config.read_text())
            self.assertEqual(document["web_search"], mode.lower())
            self.assertEqual(document["model"], "keep-model")
            self.assertEqual("no-op", codex_config.apply(config, values))
        codex_config.uninstall(config)
        self.assertEqual(parse(config.read_text())["web_search"], "disabled")

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
            ".config/windsurf-code-search/config.json": b'{"apiKey":"windsurf-test-key","baseUrl":"https://example.invalid"}',
        }
        calls = []

        def fetch(service: str, remote_path: str) -> bytes:
            calls.append((service, remote_path))
            return fixtures[remote_path]

        updated = credential_import.import_from_ssh(
            secrets, "fixture", ["codex", "windsurf"], fetch=fetch
        )
        self.assertEqual(updated, {"codex.api_key", "windsurf.api_key"})
        self.assertEqual(secret_store.get(secrets, "codex", "api_key"), "codex-test-key")
        self.assertEqual(secret_store.get(secrets, "windsurf", "api_key"), "windsurf-test-key")
        self.assertEqual(calls, [("fixture", ".codex/auth.json"), ("fixture", ".config/windsurf-code-search/config.json")])
        self.assertNotIn("baseUrl", secret_store.load(secrets).get("windsurf", {}))
        with self.assertRaises(secret_store.SecretError):
            credential_import.import_from_ssh(
                secrets, "fixture", ["codex"],
                fetch=lambda _service, _path: b'{"OPENAI_API_KEY":"different-key"}',
            )

    def test_paseo_unit_quotes_paths_with_spaces_and_specifiers(self) -> None:
        unit = paseo_service._unit(Path("/home/penn/.local/bin/paseo"), Path("/home/penn/Paseo %home"))
        self.assertIn('ExecStart=/home/penn/.local/bin/paseo daemon start --home "/home/penn/Paseo %%home"', unit)

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

    def test_paseo_config_restore_places_string_flag_before_home(self) -> None:
        paseo = self.home / "bin/paseo"
        home = self.home / "paseo"
        with mock.patch.object(paseo_service.subprocess, "run",
                               return_value=SimpleNamespace(returncode=0, stdout="{}", stderr="")) as run, \
             mock.patch.object(paseo_service, "_config_get", return_value=(True, "127.0.0.1:6767")):
            paseo_service._restore_config(paseo, home, "daemon.listen", (True, "127.0.0.1:6767"))
        self.assertEqual(run.call_args.args[0], [
            str(paseo), "daemon", "config", "set", "daemon.listen", "127.0.0.1:6767",
            "--string", "--home", str(home), "--json",
        ])

    def test_paseo_config_commits_only_target_fields_and_restores_partial_failure(self) -> None:
        paseo = self.home / "bin/paseo"
        home = self.home / "paseo"
        state = {"daemon.listen": "0.0.0.0:6767", "unrelated.setting": {"keep": True}}

        def run(command, **_kwargs):
            action, key = command[3], command[4]
            if action == "get":
                return SimpleNamespace(returncode=0, stdout=json.dumps({"set": key in state,
                                                                        "value": state.get(key)}), stderr="")
            if action == "set":
                if key == "agents.skills.selection" and self.fail_selection:
                    return SimpleNamespace(returncode=1, stdout="", stderr="failure")
                raw = command[5]
                state[key] = raw if "--string" in command else json.loads(raw)
            elif action == "unset":
                state.pop(key, None)
            return SimpleNamespace(returncode=0, stdout="{}", stderr="")

        self.fail_selection = False
        def install_skills(*_args):
            if self.fail_selection:
                raise paseo_service.PaseoServiceError("synthetic native failure")
            state["agents.skills.selection"] = {"mode": "all"}
        with mock.patch.object(paseo_service.subprocess, "run", side_effect=run), \
             mock.patch.object(paseo_service, "skills", side_effect=install_skills):
            self.assertEqual(paseo_service.configure(paseo, home, "127.0.0.1:6767"), "changed")
        self.assertEqual(state, {
            "daemon.listen": "127.0.0.1:6767", "unrelated.setting": {"keep": True},
            "agents.skills.selection": {"mode": "all"},
        })
        state.pop("agents.skills.selection")
        state["daemon.listen"] = "0.0.0.0:6767"
        self.fail_selection = True
        with mock.patch.object(paseo_service.subprocess, "run", side_effect=run), \
             mock.patch.object(paseo_service, "skills", side_effect=install_skills):
            with self.assertRaises(paseo_service.PaseoServiceError):
                paseo_service.configure(paseo, home, "127.0.0.1:6767")
        self.assertEqual(state, {"daemon.listen": "127.0.0.1:6767", "unrelated.setting": {"keep": True}})

    def test_paseo_preflight_accepts_localhost_and_rejects_non_loopback_or_invalid_port(self) -> None:
        paseo_service.preflight(self.home / ".paseo", "localhost:6767", "off", True)
        with self.assertRaisesRegex(paseo_service.PaseoServiceError, "loopback"):
            paseo_service.preflight(self.home / ".paseo", "0.0.0.0:6767", "off", True)
        with self.assertRaisesRegex(paseo_service.PaseoServiceError, "端口"):
            paseo_service.preflight(self.home / ".paseo", "127.0.0.1:65536", "off", True)

    def test_paseo_preflight_rejects_missing_systemd_user_manager(self) -> None:
        with mock.patch.object(paseo_service.shutil, "which", return_value="/usr/bin/systemctl"), \
             mock.patch.object(paseo_service.subprocess, "run",
                               return_value=SimpleNamespace(returncode=1, stdout="", stderr="")):
            with self.assertRaisesRegex(paseo_service.PaseoServiceError, "systemd user manager"):
                paseo_service.preflight(self.home / ".paseo", "127.0.0.1:6767", "login", True)

    def test_paseo_observe_accepts_different_versions_only_after_native_status_proof(self) -> None:
        skills = self.home / ".agents" / "skills"
        for name in PASEO_SKILL_NAMES:
            directory = skills / name
            directory.mkdir(parents=True, exist_ok=True)
            content = f"{name}\n".encode()
            (directory / "SKILL.md").write_bytes(content)
            (directory / ".paseo-managed-files.json").write_text(
                json.dumps({"version": 1, "files": {
                    "SKILL.md": hashlib.sha256(content).hexdigest(),
                }}), encoding="utf-8")
        paseo = self.home / "bin/paseo"
        home = self.home / "paseo"
        with mock.patch.object(paseo_service, "skills", return_value={"available": PASEO_SKILL_NAMES}) as sdk, mock.patch.object(paseo_service, "_config_get", side_effect=[
                (True, "127.0.0.1:6767"), (True, {"mode": "all"})]):
            result = paseo_service.observe(
                paseo, home, "127.0.0.1:6767", "0.16.0",
                {"state": "current", "running": True, "daemon_version": "0.14.0"})
        self.assertEqual(result["state"], "current")
        self.assertEqual(result["skills_sync"]["state"], "current")
        self.assertEqual(result["skills_sync"]["managed_skills"], PASEO_SKILL_NAMES)
        self.assertFalse(result["restart_required"])
        self.assertEqual(result["versions"]["state"], "compatible")
        self.assertEqual([call.args[2] for call in sdk.call_args_list], ["probe", "status"])

    def test_paseo_observe_detects_changed_managed_skill_files(self) -> None:
        skills = self.home / ".agents" / "skills"
        for name in PASEO_SKILL_NAMES:
            directory = skills / name
            directory.mkdir(parents=True, exist_ok=True)
            content = f"{name}\n".encode()
            (directory / "SKILL.md").write_bytes(content)
            (directory / ".paseo-managed-files.json").write_text(
                json.dumps({"version": 1, "files": {
                    "SKILL.md": hashlib.sha256(content).hexdigest(),
                }}), encoding="utf-8")
        (skills / "paseo-advisor" / "SKILL.md").write_text("changed\n", encoding="utf-8")
        paseo = self.home / "bin/paseo"
        home = self.home / "paseo"
        with mock.patch.object(paseo_service, "skills", return_value={"available": PASEO_SKILL_NAMES}), mock.patch.object(paseo_service, "_config_get", side_effect=[
                (True, "127.0.0.1:6767"), (True, {"mode": "all"})]):
            result = paseo_service.observe(
                paseo, home, "127.0.0.1:6767", "0.14.0",
                {"state": "current", "running": True, "daemon_version": "0.14.0"})
        self.assertEqual(result["skills_sync"]["state"], "incomplete")
        self.assertNotIn("paseo-advisor", result["skills_sync"]["managed_skills"])
        self.assertTrue(result["restart_required"])

    def test_paseo_observe_does_not_treat_empty_skill_install_as_restart_repairable(self) -> None:
        paseo = self.home / "bin/paseo"
        home = self.home / "paseo"
        with mock.patch.object(paseo_service, "skills", return_value={"available": PASEO_SKILL_NAMES}), mock.patch.object(paseo_service, "_config_get", side_effect=[
                (True, "127.0.0.1:6767"), (True, {"mode": "all"})]):
            result = paseo_service.observe(
                paseo, home, "127.0.0.1:6767", "0.14.0",
                {"state": "current", "daemon_version": "0.14.0"})
        self.assertEqual(result["skills_sync"]["state"], "not-installed")
        self.assertEqual(result["state"], "pending")
        self.assertFalse(result["restart_required"])

    def test_paseo_runtime_reads_daemon_version_without_starting_disabled_service(self) -> None:
        paseo = self.home / "bin/paseo"
        home = self.home / "paseo"
        reply = SimpleNamespace(returncode=0, stdout=json.dumps({
            "home": str(home), "pid": None, "daemonVersion": "0.14.0"}), stderr="")
        with mock.patch.object(paseo_service.subprocess, "run", return_value=reply) as run:
            state = paseo_service.runtime(paseo, home, "off", False)
        self.assertEqual(state["daemon_version"], "0.14.0")
        self.assertEqual(state["state"], "current")
        self.assertEqual(run.call_count, 1)

    def test_paseo_runtime_requires_a_listening_reachable_daemon_when_enabled(self) -> None:
        paseo = self.home / "bin/paseo"
        home = self.home / "paseo"
        reply = SimpleNamespace(returncode=0, stdout=json.dumps({
            "home": str(home), "pid": 1234, "daemonVersion": "0.14.0",
            "localDaemon": "running", "listen": "127.0.0.1:6767",
            "connectedDaemon": "unreachable",
        }), stderr="")
        with mock.patch.object(paseo_service.subprocess, "run", return_value=reply), \
             mock.patch.object(paseo_service, "_systemctl"), \
             mock.patch.object(paseo_service.os, "kill"):
            state = paseo_service.runtime(paseo, home, "off", True)
        self.assertTrue(state["running"])
        self.assertFalse(state["healthy"])
        self.assertEqual(state["state"], "start-required")

    def test_paseo_busy_guard_rejects_malformed_agent_receipt(self) -> None:
        paseo = self.home / "bin/paseo"
        home = self.home / "paseo"
        with mock.patch.object(paseo_service.subprocess, "run", side_effect=[
                SimpleNamespace(returncode=0, stdout="[]", stderr=""),
                SimpleNamespace(returncode=0, stdout='[{"id":"hidden"}]', stderr=""),
        ]):
            with self.assertRaisesRegex(paseo_service.PaseoServiceError, "回执无效"):
                paseo_service.assert_no_active_work(paseo, home)

    def test_paseo_busy_guard_includes_background_workspaces(self) -> None:
        paseo = self.home / "bin/paseo"
        home = self.home / "paseo"
        with mock.patch.object(paseo_service.subprocess, "run", side_effect=[
                SimpleNamespace(returncode=0, stdout='[{"id":"background","background":true}]', stderr=""),
                SimpleNamespace(returncode=0, stdout="[]", stderr=""),
        ]) as run:
            with self.assertRaisesRegex(paseo_service.PaseoServiceError, "workspace=1"):
                paseo_service.assert_no_active_work(paseo, home)
        self.assertIn("--background", run.call_args_list[0].args[0])
        self.assertIn("-g", run.call_args_list[1].args[0])
        self.assertIn("--background", run.call_args_list[1].args[0])

    def test_paseo_busy_guard_counts_only_live_agent_statuses(self) -> None:
        paseo = self.home / "bin/paseo"
        home = self.home / "paseo"
        with mock.patch.object(paseo_service.subprocess, "run", side_effect=[
                SimpleNamespace(returncode=0, stdout="[]", stderr=""),
                SimpleNamespace(returncode=0, stdout='[{"id":"starting","status":"initializing"},{"id":"waiting","status":"idle"},{"id":"running","status":"running"},{"id":"failed","status":"error"}]', stderr=""),
        ]):
            with self.assertRaisesRegex(paseo_service.PaseoServiceError, "agent=3"):
                paseo_service.assert_no_active_work(paseo, home)

    def test_paseo_service_failed_install_restores_absent_unit(self) -> None:
        paseo = self.home / "bin/paseo"
        paseo.parent.mkdir()
        paseo.write_text("fixture", encoding="utf-8")
        home = self.home / "paseo"
        unit = self.home / "systemd/user/pennix-paseo.service"
        unit.parent.mkdir(parents=True)
        runtime = {"state": "current", "running": False, "healthy": False,
                   "enabled": False, "active": False, "linger": False}

        def systemctl(*args: str) -> None:
            if args == ("enable", unit.stem):
                raise paseo_service.PaseoServiceError("simulated partial enable failure")

        with mock.patch.object(paseo_service, "unit_path", return_value=unit), \
             mock.patch.object(paseo_service, "runtime", return_value=runtime), \
             mock.patch.object(paseo_service, "_config_get", side_effect=[
                 (True, "127.0.0.1:6767"), (True, {"mode": "all"}),
             ]), \
             mock.patch.object(paseo_service, "_systemctl", side_effect=systemctl) as run_systemctl:
            with self.assertRaisesRegex(paseo_service.PaseoServiceError, "partial enable"):
                paseo_service.apply("login", paseo, home, "127.0.0.1:6767", True)
        self.assertFalse(unit.exists())
        self.assertIn(("disable", unit.stem), [call.args for call in run_systemctl.call_args_list])
        self.assertNotIn(("stop", unit.stem), [call.args for call in run_systemctl.call_args_list])

    def test_paseo_service_failed_uninstall_preserves_disabled_inactive_state(self) -> None:
        paseo = self.home / "bin/paseo"
        home = self.home / "paseo"
        unit = self.home / "systemd/user/pennix-paseo.service"
        unit.parent.mkdir(parents=True)
        unit.write_text(paseo_service._unit(paseo, home), encoding="utf-8")
        paseo_service._write_receipt(paseo, home)
        runtime = {"state": "current", "running": False, "healthy": False,
                   "enabled": False, "active": False, "linger": False}
        calls = []

        def systemctl(*args: str) -> None:
            calls.append(args)
            if args == ("daemon-reload",) and calls.count(args) == 1:
                raise paseo_service.PaseoServiceError("simulated reload failure")

        original = unit.read_bytes()
        with mock.patch.object(paseo_service, "unit_path", return_value=unit), \
             mock.patch.object(paseo_service, "runtime", return_value=runtime), \
             mock.patch.object(paseo_service, "_systemctl", side_effect=systemctl):
            with self.assertRaisesRegex(paseo_service.PaseoServiceError, "reload failure"):
                paseo_service.apply("off", paseo, home, "127.0.0.1:6767", False)
        self.assertEqual(unit.read_bytes(), original)
        self.assertNotIn(("enable", "--now", unit.stem), calls)
        self.assertIn(("disable", unit.stem), calls)
        self.assertNotIn(("stop", unit.stem), calls)

    def test_paseo_install_preserves_healthy_independent_daemon(self) -> None:
        paseo = self.home / "bin/paseo"
        paseo.parent.mkdir()
        paseo.write_text("fixture", encoding="utf-8")
        home = self.home / "paseo"
        unit = self.home / "systemd/user/pennix-paseo.service"
        runtime = {"state": "start-required", "running": True, "healthy": True,
                   "enabled": False, "active": False, "linger": False}
        with mock.patch.object(paseo_service, "unit_path", return_value=unit), \
             mock.patch.object(paseo_service, "runtime", return_value=runtime), \
             mock.patch.object(paseo_service, "assert_no_active_work") as busy, \
             mock.patch.object(paseo_service, "_systemctl") as systemctl, \
             mock.patch.object(paseo_service, "_run_daemon") as daemon:
            self.assertEqual(paseo_service.apply("login", paseo, home, "127.0.0.1:6767", False), "changed")
        daemon.assert_not_called()
        busy.assert_not_called()
        self.assertEqual([call.args for call in systemctl.call_args_list],
                         [("daemon-reload",), ("enable", unit.stem)])
        with mock.patch.object(paseo_service, "unit_path", return_value=unit):
            self.assertEqual(paseo_service.inspect(paseo, home), "current")

    def test_relay_accepts_only_empty_or_native_disabled_baseline(self) -> None:
        keys = ['daemon.relay.enabled', 'daemon.relay.endpoint', 'daemon.relay.publicEndpoint',
                'daemon.relay.useTls', 'daemon.relay.publicUseTls']
        empty = dict.fromkeys(keys, relay_owner._MISSING)
        for enabled in [relay_owner._MISSING, False, True, 0]:
            values = {**empty, keys[0]:enabled}
            with self.subTest(enabled=enabled), \
                 mock.patch.object(relay_owner, '_daemon_values', return_value=values), \
                 mock.patch.object(relay_owner, '_daemon_receipt', return_value=(self.home/'receipt', None)):
                if enabled is False or enabled is relay_owner._MISSING:
                    self.assertEqual(relay_owner.inspect_daemon(Path('paseo'), self.home, 'relay:1', 'public:2', True, False), 'missing')
                else:
                    with self.assertRaises(relay_owner.RelayError):
                        relay_owner.inspect_daemon(Path('paseo'), self.home, 'relay:1', 'public:2', True, False)
        values = {**empty, keys[0]:False, keys[1]:'external:443'}
        with mock.patch.object(relay_owner, '_daemon_values', return_value=values), \
             mock.patch.object(relay_owner, '_daemon_receipt', return_value=(self.home/'receipt', None)):
            with self.assertRaises(relay_owner.RelayError):
                relay_owner.inspect_daemon(Path('paseo'), self.home, 'relay:1', 'public:2', True, False)
        with mock.patch.object(relay_owner, '_daemon_values', return_value={**empty, keys[0]:False}), \
             mock.patch.object(relay_owner, '_daemon_receipt', return_value=(self.home/'receipt', {'desired':values})):
            with self.assertRaises(relay_owner.RelayError):
                relay_owner.inspect_daemon(Path('paseo'), self.home, 'relay:1', 'public:2', True, False)

    def test_relay_daemon_config_restores_partial_write(self) -> None:
        paseo = self.home / "bin/paseo"
        paseo.parent.mkdir()
        paseo.write_text("#!/bin/sh\n", encoding="utf-8")
        state = {}

        def run(command, **_kwargs):
            if 'status' in command:
                return SimpleNamespace(returncode=0, stdout=json.dumps({'home': str(self.home / 'paseo'), 'pid': None}), stderr='')
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
            if 'status' in command:
                return SimpleNamespace(returncode=0, stdout=json.dumps({'home': str(self.home / 'paseo'), 'pid': None}), stderr='')
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
            self.assertEqual(
                relay_owner.inspect_daemon(paseo, self.home / "paseo", "relay:1", "public:2", True, False),
                "current",
            )


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_plan_does_not_print_nested_codex_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env = {**os.environ, "HOME": directory, "CODEX_HOME": str(Path(directory) / ".codex"),
                   "XDG_CONFIG_HOME": str(Path(directory) / "config"), "XDG_STATE_HOME": str(Path(directory) / "state")}
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
