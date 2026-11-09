"""Isolated counterexamples from the lifecycle architecture audit."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import _engine
import candidates
import config_model
import connection_owners
import owned_files
import apply_journal
import pennix
import paseo_service
import relay_owner


class AuditRegressions(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        env = patch.dict(os.environ, {"HOME": str(self.root), "XDG_CONFIG_HOME": str(self.root / "config"),
                                     "XDG_STATE_HOME": str(self.root / "state")})
        env.start()
        self.addCleanup(env.stop)

    def test_strict_requests_cannot_silently_revert_to_defaults(self):
        path = config_model.init()
        original = path.read_text()
        for invalid in [original.replace('CONFIG_PENNIX_CONFIG_FORMAT="5"', 'CONFIG_PENNIX_CONFIG_FORMAT="4"'),
                        original.replace('CONFIG_PENNIX_COMPONENT_CODEX_CLI=y', 'CONFIG_PENNIX_COMPONENT_CODEX_CLI=m'),
                        original + "invalid syntax\n"]:
            path.write_text(invalid)
            with self.assertRaises(config_model.ConfigError):
                config_model.load()

    def test_config_prefix_environment_cannot_change_public_syntax(self):
        with patch.dict(os.environ, {"CONFIG_": "HOSTILE_"}):
            path = config_model.init()
            self.assertIn("CONFIG_PENNIX_COMPONENT_CODEX_CLI=y", path.read_text())
            self.assertEqual(config_model.load()[0].syms["PENNIX_COMPONENT_CODEX_CLI"].str_value, "y")

    def test_load_parses_only_the_validated_snapshot(self):
        path = config_model.init()
        original = path.read_bytes()
        read = config_model._read
        def replaced(target):
            raw = read(target)
            target.write_bytes(b"changed after snapshot")
            return raw
        with patch.object(config_model, "_read", side_effect=replaced):
            loaded, _ = config_model.load()
        self.assertEqual(loaded.snapshot, original)
        self.assertEqual(loaded.syms["PENNIX_COMPONENT_CODEX_CLI"].str_value, "y")

    def test_shared_target_survives_xdg_state_root_change(self):
        target = self.root / "codex"
        owned_files.bind_target(target, register=True)
        with patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "different-state")}):
            with self.assertRaises(owned_files.OwnershipError):
                owned_files.bind_target(target)
        with self.assertRaises(owned_files.OwnershipError):
            owned_files.bind_target(self.root / "another-codex")

    def test_nonzero_version_output_is_not_identity(self):
        result = subprocess.CompletedProcess([], 2, "tool 1.2.3", "")
        with patch.object(_engine.subprocess, "run", return_value=result):
            self.assertIsNone(_engine._version({}, "synthetic-command"))

    def test_official_pkginfo_uses_complete_version_and_epoch(self):
        component = {"lock_source": "arch:extra/example", "package": {"name": "example"}}
        with patch.object(candidates.owned_files, "download", side_effect=lambda url, path: path.write_bytes(b"signed")), \
             patch.object(candidates, "_run", return_value=subprocess.CompletedProcess([], 0, "preview", "")), \
             patch.object(candidates, "_arch_pkginfo", return_value=({"pkgname": "example", "pkgver": "2:1.0-3"}, [])):
            value = candidates._verify_arch_package(component, self.root, "example-1.0-3.pkg.tar.zst", "2:1.0-3", "https://fixture.test/package", None)
        self.assertEqual(value["version"], "2:1.0-3")
        for name in ["../outside.whl", "/absolute.whl", "x/y.whl", "..", ""]:
            with self.assertRaises(candidates.CandidateError):
                candidates._filename(name)

    def test_owner_cannot_replace_dispatch_fields_and_bad_identity_is_unknown(self):
        payload = {"operation_id": "expected"}
        reply = {"state": "current", "owned": True, "protocol_version": 1,
                 "capabilities": ["validate", "execution-effect"], "operation_id": "expected",
                 "effect": "none", "selected": False, "action": "uninstall", "target": "other"}
        with patch.object(connection_owners.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps(reply), "")):
            result = connection_owners._run(["synthetic"], "validate", payload)
            self.assertNotIn("selected", result)
            self.assertNotIn("action", result)
            self.assertNotIn("target", result)
        reply["operation_id"] = "wrong"
        with patch.object(connection_owners.subprocess, "run", return_value=subprocess.CompletedProcess([], 2, json.dumps(reply), "")):
            with self.assertRaises(connection_owners.ConnectionError) as failure:
                connection_owners._run(["synthetic"], "configure", payload)
            self.assertEqual(failure.exception.effect, "unknown")

    def test_journal_keeps_owner_evidence_through_exception_chaining(self):
        journal = apply_journal.start("apply", None, "a" * 64, None,
            [{"id": "connection:siyuan:configure", "component": "connection:siyuan", "action": "configure", "target": {}}])
        def rejected():
            try:
                raise OSError("private synthetic cause")
            except OSError as cause:
                raise connection_owners.ConnectionError("redacted", effect="none") from cause
        with self.assertRaises(connection_owners.ConnectionError):
            pennix._journal_mutate(journal, journal["steps"][0]["id"], rejected)
        self.assertEqual(apply_journal.read()["steps"][0]["status"], "pending")
        self.assertEqual(apply_journal.read()["steps"][0]["observed"], {"effect": "none"})

    def test_system_upgrade_replans_pending_actions_and_preserves_completion(self):
        journal = apply_journal.start("apply", None, "a" * 64, None, [
            {"id": "system-upgrade", "component": "system", "action": "upgrade", "target": {}},
            {"id": "tmux:upgrade", "component": "tmux", "action": "upgrade", "target": {"version": "old"}}])
        journal = apply_journal.verified(journal, "system-upgrade")
        updated = apply_journal.replan(journal, [
            {"id": "tmux:uninstall", "component": "tmux", "action": "uninstall", "target": {}}])
        self.assertEqual([(s["id"], s["status"]) for s in updated["steps"]],
                         [("system-upgrade", "verified"), ("tmux:uninstall", "pending")])

    def test_uninstall_failure_is_durable_and_cannot_replay_unknown(self):
        config = self.root / "workflow.config"
        config.write_text("same input")
        args = SimpleNamespace(component="tmux-config", confirm="tmux-config", all=False, json=True,
                               config_path=config, component_lock_path=self.root / "components.lock.json",
                               component_lock={"schema": 1, "components": {}})
        catalog = {"components": {"tmux-config": {"delivery": "static"}}, "configuration_targets": {}}
        with patch.object(pennix, "_load", return_value=(SimpleNamespace(), config, catalog)), \
             patch.object(config_model, "config_value", return_value=str(self.root / "codex")), \
             patch.object(config_model, "selected_components", return_value=set()), \
             patch.object(pennix, "_run_component", side_effect=pennix.CliError("synthetic partial failure")) as mutate:
            with self.assertRaisesRegex(pennix.CliError, "partial failure"):
                pennix.cmd_uninstall(args)
            self.assertEqual(apply_journal.read()["command"], "uninstall")
            self.assertEqual(apply_journal.read()["steps"][0]["status"], "unknown")
            with self.assertRaisesRegex(pennix.CliError, "副作用状态未知"):
                pennix.cmd_uninstall(args)
            self.assertEqual(mutate.call_count, 1)
        self.assertEqual(config.read_text(), "same input")

    def test_static_removal_preserves_body_bytes_and_blank_lines(self):
        template = self.root / "template"
        template.write_text("managed\n")
        path = self.root / ".tmux.conf"
        before, after = "# user\n\n\n", "\n\n# tail\n"
        path.write_text(before + f"{_engine.BEGIN_TMUX}\nmanaged\n{_engine.END_TMUX}" + after)
        with patch.object(_engine, "TEMPLATE_ROOT", self.root):
            _engine._operate_static("tmux-config", self.root / "codex", {"template": {"name": "template"}}, "uninstall")
        self.assertEqual(path.read_text(), before + after)

    def test_marketplace_with_other_plugin_is_never_removed(self):
        market = {"name": "shared", "root": str(self.root), "marketplaceSource": {"source": "https://example.test/repo.git"}}
        replies = [subprocess.CompletedProcess([], 0, json.dumps({"marketplaces": [market]})),
                   subprocess.CompletedProcess([], 0, json.dumps({"installed": [{"pluginId": "other@shared"}]}))]
        with patch.object(_engine, "_plugin_tree_clean", return_value=True), \
             patch.object(_engine.subprocess, "run", side_effect=replies) as run:
            with self.assertRaisesRegex(_engine.BootstrapError, "其他插件"):
                _engine._prepare_plugin_marketplace("codex", self.root, {"name": "shared",
                    "source": "https://example.test/repo.git", "managed_plugin": "owned@shared"})
            self.assertFalse(any("remove" in c.args[0] for c in run.call_args_list))

    def test_config_missing_new_visible_component_requires_review(self):
        path = config_model.init()
        path.write_text("\n".join(line for line in path.read_text().splitlines()
                                  if "CONFIG_PENNIX_COMPONENT_CODEX_CLI=" not in line) + "\n")
        with self.assertRaisesRegex(config_model.ConfigError, "组件选择缺失"):
            config_model.load()

    def test_paseo_candidate_preflight_is_isolated_and_rejects_unverified_sdk(self):
        with patch.object(paseo_service.subprocess, "run") as run:
            with self.assertRaisesRegex(paseo_service.PaseoServiceError, "0.15.3"):
                paseo_service.preflight_candidate({"version": "0.15.4", "path": "/synthetic.tgz"})
            run.assert_not_called()
        with patch.object(paseo_service.shutil, "which", return_value="/usr/bin/npm"), \
             patch.object(paseo_service.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run, \
             patch.object(paseo_service, "skills", return_value={"state": "compatible"}) as probe:
            paseo_service.preflight_candidate({"version": "0.15.3", "path": "/synthetic.tgz"})
            environment = run.call_args.kwargs["env"]
            self.assertNotEqual(environment["HOME"], str(self.root))
            self.assertTrue(environment["CODEX_HOME"].startswith(environment["HOME"] + "/"))
            self.assertEqual(environment["NPM_CONFIG_USERCONFIG"], "/dev/null")
            self.assertIn("--ignore-scripts", run.call_args.args[0])
            self.assertEqual(probe.call_args.kwargs["environment"], environment)

    def test_malformed_journal_is_a_diagnostic_failure(self):
        value = {"schema": 2, "command": [], "component": None, "config_digest": None,
                 "lock_digest": None, "steps": [], "transaction": "x", "identity": {}}
        with self.assertRaises(apply_journal.JournalError):
            apply_journal._validate(value)

    def test_pending_owner_uses_fixed_staged_native_validation_before_mutation(self):
        args = SimpleNamespace(kconf=object(), codex_home=self.root / "codex")
        spec = {"command": ["grok-search"]}
        value = {"connections": {"grok": {"state": "pending-owner"}}}
        catalog = {"configuration_targets": {"grok": spec}, "components": {"pennix-skills": {}}}
        steps = [{"component": "pennix-skills", "action": "install", "target": {}}]
        candidate = {"source": "collection", "ref": "a" * 40, "sha256": "b" * 64}
        with patch.object(candidates, "resolve", return_value=candidate), \
             patch.object(pennix.skill_collections, "_stage", return_value=(self.root, {"commands": {"grok-search": "native.js"}})) as stage, \
             patch.object(config_model, "config_value", return_value=str(self.root / "secrets")), \
             patch.object(connection_owners, "_payload", return_value={"operation_id": "bound"}), \
             patch.object(connection_owners, "_run", side_effect=connection_owners.ConnectionError("invalid input")) as validate, \
             patch.object(pennix, "_run_component") as mutate:
            with self.assertRaisesRegex(connection_owners.ConnectionError, "invalid input"):
                pennix._preflight_pending_owners(args, value, catalog, steps)
            mutate.assert_not_called()
            self.assertEqual(validate.call_args.args[:2], ([str(self.root / "native.js")], "validate"))
            self.assertEqual(stage.call_args.args[0]["_candidate_target"], candidate)
            self.assertNotEqual(stage.call_args.kwargs["environment"]["HOME"], str(self.root))

    def test_relay_external_config_is_rejected_before_native_writes(self):
        with patch.object(relay_owner, "_daemon_values", return_value={"daemon.relay.endpoint": "external:443"}), \
             patch.object(relay_owner, "_daemon_receipt", return_value=(self.root / "receipt", None)), \
             patch.object(relay_owner.subprocess, "run") as run:
            with self.assertRaisesRegex(relay_owner.RelayError, "归属证据"):
                relay_owner.configure_daemon(Path("paseo"), self.root, "new.test:443", "public.test:443", True, True)
            run.assert_not_called()

    def test_relay_receipt_cannot_authorize_unrelated_config_keys(self):
        record = {"schema": 1, "home": str(self.root), "before": {"unrelated.key": {"present": False, "value": None}},
                  "desired": {"unrelated.key": "owned"}}
        with patch.object(owned_files, "read_json", return_value=record):
            with self.assertRaisesRegex(relay_owner.RelayError, "归属字段"):
                relay_owner._daemon_receipt(self.root)

    def test_catalog_does_not_claim_unsupported_actions_or_marketplace_branch(self):
        source = json.loads(pennix.CATALOG.read_text())
        for mutate in [lambda value: value["components"]["pennix-workflow-state"]["actions"].update(reconcile="managed"),
                       lambda value: value["components"]["ponytail-plugin"]["plugin"]["marketplace"].update(branch="main"),
                       lambda value: value["components"]["cch-status"]["actions"].update(install=[]),
                       lambda value: value["components"]["cch-status"]["actions"].update(install="unsupported")]:
            value = json.loads(json.dumps(source)); mutate(value)
            path = self.root / "catalog.json"; path.write_text(json.dumps(value))
            with self.assertRaises(_engine.BootstrapError):
                _engine.load_catalog(path)

    def test_xdg_empty_values_use_home_defaults_and_relative_values_are_rejected(self):
        for name, suffix in [("XDG_CONFIG_HOME", ".config"), ("XDG_STATE_HOME", ".local/state"),
                             ("XDG_DATA_HOME", ".local/share"), ("XDG_CACHE_HOME", ".cache")]:
            fallback = self.root / suffix
            with patch.dict(os.environ, {name: ""}):
                self.assertEqual(owned_files.xdg_home(name, fallback), fallback)
            for invalid in ["relative", "~/relative", str(self.root / ".." / "other")]:
                with patch.dict(os.environ, {name: invalid}):
                    with self.assertRaises(owned_files.OwnershipError):
                        owned_files.xdg_home(name, fallback)

    def test_init_and_render_preserve_home_intent_across_devices(self):
        path = config_model.init(str(self.root / "workflow.config"))
        original = path.read_bytes()
        self.assertNotIn(str(self.root).encode(), original)
        self.assertIn(b'CONFIG_PENNIX_CODEX_HOME="${HOME}/.codex"', original)
        other = self.root / "other-device"
        with patch.dict(os.environ, {"HOME": str(other)}):
            loaded, _ = config_model.load(str(path))
            self.assertEqual(config_model.config_value(loaded, "PENNIX_CODEX_HOME"), str(other / ".codex"))
            config_model.write(loaded, path, before=loaded.snapshot)
            self.assertEqual(path.read_bytes(), original)

    def test_pacman_executable_owner_cannot_hide_wrong_package_origin(self):
        for source, flag in [("official", "-Qn"), ("aur", "-Qm")]:
            replies = [subprocess.CompletedProcess([], 0, "tool 1.0-1"),
                       subprocess.CompletedProcess([], 0, "tool"), subprocess.CompletedProcess([], 1, "")]
            with patch.object(_engine, "_command", return_value="/usr/bin/tool"), \
                 patch.object(_engine.subprocess, "run", side_effect=replies) as run:
                with self.assertRaisesRegex(_engine.BootstrapError, "origin differs"):
                    _engine.package_identity({"package": {"source": source, "name": "tool"}})
                self.assertEqual(run.call_args.args[0], ["pacman", flag, "tool"])

    def test_uninstall_rejects_unsupported_host_before_reading_configuration(self):
        with patch.object(_engine.host, "detect_host", return_value={"supported": False, "reason": "unsupported fixture"}), \
             patch.object(pennix, "_load") as load, patch("sys.stderr"):
            self.assertEqual(pennix.main(["uninstall", "cch-status", "--confirm", "cch-status"]), 2)
            load.assert_not_called()

    def test_system_upgrade_refreshes_owned_official_packages_even_when_deselected(self):
        args = SimpleNamespace(component_lock={}, component_lock_sources={})
        official = {"package": {"source": "official"}, "lock_source": "arch:core/tool"}
        catalog = {"components": {"official": official, "aur": {"package": {"source": "aur"}}}}
        with patch.object(_engine, "_package_receipt", return_value={"version": "1-1"}), \
             patch.object(_engine, "package_identity", return_value={"version": "2-1"}) as identity, \
             patch.object(_engine, "_write_package_receipt") as receipt, \
             patch.object(pennix.component_lock, "set_entry", return_value={}), \
             patch.object(pennix, "_write_lock") as lock:
            pennix._refresh_arch_receipts(args, catalog)
            identity.assert_called_once_with(official)
            receipt.assert_called_once_with("official", official, "2-1")
            lock.assert_called_once_with(args)
