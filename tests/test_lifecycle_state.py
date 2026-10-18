"""Regression checks for truthful plans, package ownership and owner ordering."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import _engine as engine
import config_model
import connection_owners
import pennix


class LifecycleStateTests(unittest.TestCase):
    def test_check_rejects_pending_connections_and_external_packages(self):
        value = {"components": {"package": {"selected": True, "action": "keep", "state": "match"}},
                 "connections": {"service": {"selected": True, "action": "keep", "state": "current"}}}
        args = SimpleNamespace(json=True)
        for group, key, field, changed in [("components", "package", "action", "blocked"),
                                            ("connections", "service", "state", "missing"),
                                            ("connections", "service", "action", "configure")]:
            current = copy.deepcopy(value)
            current[group][key][field] = changed
            with patch.object(pennix, "_plan", return_value=current), patch.object(pennix, "_emit"):
                self.assertEqual(pennix.cmd_check(args), 1)
        with patch.object(pennix, "_plan", return_value=value), patch.object(pennix, "_emit"):
            self.assertEqual(pennix.cmd_check(args), 0)

    def test_plugin_version_and_source_are_required(self):
        component = {"approved_version": "5.3.2", "plugin": {"id": "ponytail@ponytail",
                     "marketplace": {"source": "https://example.test/plugin.git", "ref": "a" * 40}}}
        installed = {"pluginId": "ponytail@ponytail", "enabled": True, "version": "5.3.1",
                     "source": {"url": "https://example.test/plugin.git", "ref": "a" * 40}}
        with patch.object(engine.shutil, "which", return_value="codex"), patch.object(engine.subprocess, "run") as run, \
             patch.object(engine, "_plugin_refs", return_value=True):
            run.return_value = SimpleNamespace(returncode=0, stdout=json.dumps({"installed": [installed]}))
            self.assertEqual(engine._plugin_status(component, Path("/tmp/codex"))[0], "outdated")
            installed["version"] = "5.3.2"
            run.return_value.stdout = json.dumps({"installed": [installed]})
            self.assertEqual(engine._plugin_status(component, Path("/tmp/codex"))[0], "match")
            self.assertEqual(run.call_args.kwargs["env"]["CODEX_HOME"], "/tmp/codex")

    def test_plugin_operation_refreshes_same_source_marketplace(self):
        component = {"plugin": {"id": "ponytail@ponytail",
                     "marketplace": {"name": "ponytail", "source": "https://example.test/plugin.git",
                                      "ref": "b" * 40}}}
        marketplace = {"name": "ponytail", "marketplaceSource": {
            "sourceType": "git", "source": "https://example.test/plugin.git"}}
        with patch.object(engine.shutil, "which", return_value="codex"), \
             patch.object(engine, "_plugin_status", side_effect=[("outdated", "5.3.1"), ("match", "5.3.2")]), \
             patch.object(engine.subprocess, "run") as run:
            run.side_effect = [
                SimpleNamespace(returncode=0, stdout=json.dumps({"marketplaces": [marketplace]})),
                SimpleNamespace(returncode=0),
                SimpleNamespace(returncode=0),
                SimpleNamespace(returncode=0),
            ]
            self.assertEqual(engine._plugin_operation(component, Path("/tmp/codex"), "upgrade"), "changed")
            self.assertEqual(run.call_args_list[1].args[0][:5],
                             ["codex", "plugin", "marketplace", "remove", "ponytail"])

    def test_rolling_packages_compare_native_full_versions(self):
        component = {"delivery": "package", "probe": "codex", "version_policy": "repository-latest"}
        with patch.object(engine, "_command", return_value="codex"), patch.object(engine, "_version", return_value="0.1.0"), \
             patch.object(engine, "package_identity", return_value={"version": "0.1.0-1"}), \
             patch.object(engine, "package_candidate", return_value="0.1.0-2"):
            self.assertEqual(engine.probe_component(component)[0], "outdated")

    def test_adoption_does_not_skip_requested_update(self):
        component = {"delivery": "package", "approved_version": "2.0.0", "probe": "test",
                     "package": {"source": "official", "name": "test"}}
        kconf = SimpleNamespace(syms={key: SimpleNamespace(tri_value=2) for key in
                                     ["PENNIX_ADOPT_EXISTING_PACKAGES", "PENNIX_UPDATE_COMPONENTS"]})
        args = SimpleNamespace(codex_home=Path("/tmp/codex"), kconf=kconf)
        with patch.object(engine.host, "detect_host", return_value={"supported": True, "installers": {"official": "pacman"}}), \
             patch.object(engine, "probe_component", side_effect=[("outdated", "1.0.0"), ("match", "2.0.0"), ("match", "2.0.0")]), \
             patch.object(engine, "package_owned", return_value=False), patch.object(engine, "package_identity"), \
             patch.object(engine, "_write_package_receipt") as receipt, patch.object(engine.subprocess, "run") as run:
            run.return_value = SimpleNamespace(returncode=0)
            self.assertEqual(engine.component_operation(args, {}, "test", component, "upgrade"), "changed")
            self.assertEqual(run.call_args.args[0], ["sudo", "pacman", "-S", "--needed", "--noconfirm", "test"])
            self.assertEqual(receipt.call_args.args[-1], "2.0.0")

    def test_connection_preflight_defers_only_missing_selected_owner(self):
        spec = {"select": "PENNIX_TARGET_CCH", "component": "cch-status", "owner": "cch", "command": ["missing-owner"],
                "values": {}, "secrets": {}}
        with patch.object(connection_owners, "_command", side_effect=connection_owners.ConnectionError("missing")), \
             patch.object(connection_owners.config_model, "config_value", return_value="y"):
            result = connection_owners.plan(None, {"configuration_targets": {"cch": spec}}, Path("/tmp/codex"),
                                            Path("/tmp/secrets"), installing={"cch-status"})["cch"]
            self.assertEqual(result["state"], "pending-owner")
            self.assertEqual(result["action"], "configure")
            with patch.object(connection_owners, "_secret_payload", side_effect=connection_owners.secret_store.SecretError("missing secret")):
                result = connection_owners.plan(None, {"configuration_targets": {"cch": spec}}, Path("/tmp/codex"),
                                                Path("/tmp/secrets"), installing={"cch-status"})["cch"]
                self.assertEqual(result["action"], "blocked")

    def test_functional_request_must_run_and_failures_are_not_success(self):
        spec = {"configuration_targets": {"service": {"functional_command": [sys.executable, "-c", "pass"]}}}
        connections = {"service": {"selected": True, "action": "keep"}}
        self.assertEqual(connection_owners.functional_checks(spec, connections, Path("/tmp/codex"))["service"]["state"], "passed")
        spec["configuration_targets"]["service"]["functional_command"][-1] = "raise SystemExit(1)"
        self.assertEqual(connection_owners.functional_checks(spec, connections, Path("/tmp/codex"))["service"]["state"], "failed")
        del spec["configuration_targets"]["service"]["functional_command"]
        self.assertEqual(connection_owners.functional_checks(spec, connections, Path("/tmp/codex"))["service"]["state"], "blocked")

    def test_all_uninstall_checks_owned_connections_before_any_mutation(self):
        args = SimpleNamespace(all=True, confirm_all="ALL", component=None)
        catalog = {"components": {}, "configuration_targets": {"service": {}}}
        with patch.object(pennix, "_load", return_value=(None, Path("/tmp/config"), catalog)), \
             patch.object(config_model, "config_value", return_value="/tmp/codex"), \
             patch.object(pennix, "_plan", return_value={"components": {}}), \
             patch.object(connection_owners, "inspect_target", return_value={"owned": True, "state": "invalid"}), \
             patch.object(connection_owners, "apply_target") as apply, \
             patch.object(pennix, "_run_component") as run:
            with self.assertRaises(pennix.CliError):
                pennix.cmd_uninstall(args)
            apply.assert_not_called()
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
