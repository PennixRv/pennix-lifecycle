"""Exact drift acceptance and the independent Paseo autostart contract."""
import hashlib
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
import apply_journal
import component_lock
import connection_owners
import config_model
import owned_files
import path_env
import paseo_service
import pennix


class ConvergenceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.env = patch.dict(os.environ, {"HOME": str(self.root), "XDG_STATE_HOME": str(self.root / "state"),
                                         "XDG_CONFIG_HOME": str(self.root / "config")})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.component = {"delivery": "package", "lock_source": "aur:example-bin", "probe": "example",
                          "package": {"source": "aur", "name": "example-bin"}}
        self.old = {"source": "aur:example-bin", "version": "1.0-1"}
        self.identity = {"manager": "pacman", "name": "example-bin", "version": "2.0-1", "command": "/usr/bin/example"}
        self.receipt = {"schema": 1, "component": "example", "source": "aur", "name": "example-bin", "version": "1.0"}
        self.receipt_path = engine._package_receipt_path("example")
        owned_files.replace(self.receipt_path, owned_files.json_bytes(self.receipt), None)

    def test_package_candidate_requires_old_receipt_lock_source_and_native_owner(self):
        with patch.object(engine, "package_identity", return_value=self.identity), patch.object(engine, "_version", return_value="2.0"):
            target, identity, receipt = engine.reconcile_candidate("example", self.component, self.root, self.old)
            self.assertEqual(target, {"source": self.old["source"], "version": "2.0-1"})
            self.assertEqual(identity["probe_version"], "2.0")
            self.assertEqual(receipt, self.receipt)
            for old in ({"source": "aur:other", "version": "1.0-1"}, {**self.old, "version": "0.1-1"}):
                with self.assertRaises(engine.BootstrapError):
                    engine.reconcile_candidate("example", self.component, self.root, old)
            self.receipt_path.unlink()
            with self.assertRaises(engine.BootstrapError):
                engine.reconcile_candidate("example", self.component, self.root, self.old)

    def test_package_diagnostics_preserve_old_management_when_native_package_changed(self):
        component = {**self.component, "_lock_target": self.old, "native_config": {"owner": "example"}}
        with patch.object(engine, "package_identity", return_value=self.identity):
            status = pennix._package_diagnostics("example", component, "outdated")
        self.assertTrue(status["managed_record"])
        self.assertEqual(status["receipt_version"], "1.0")
        self.assertEqual(status["observed_identity"]["version"], "2.0-1")
        self.assertEqual(status["locked_identity"]["version"], "1.0-1")
        self.assertEqual(status["mismatch_reason"], "observed_identity_differs_from_lock")

    def run_reconcile(self, *, fail_lock=False, confirmation=None):
        config = self.root / "workflow.config"
        lock = self.root / "components.lock.json"
        if not config.exists():
            config.write_text("same input")
            component_lock.write(lock, {"schema": 1, "components": {"example": self.old}}, {"example": self.old["source"]})
        args = SimpleNamespace(component="example", json=True)
        def load(_args):
            args.config_path = config
            args.component_lock_path = lock
            args.component_lock_sources = {"example": self.old["source"]}
            args.component_lock = component_lock.read(lock, args.component_lock_sources)
            return SimpleNamespace(), config, {"components": {"example": self.component}}
        replace = owned_files.replace
        def write(path, content, before, **kwargs):
            if path == lock and fail_lock:
                raise OSError("synthetic partial lock failure")
            return replace(path, content, before, **kwargs)
        with patch.object(pennix, "_load", side_effect=load), \
             patch.object(pennix.config_model, "selected_components", return_value={"example"}), \
             patch.object(pennix.config_model, "config_value", return_value=str(self.root)), \
             patch.object(engine, "package_identity", return_value=self.identity), \
             patch.object(engine, "_version", return_value="2.0"), \
             patch.object(sys.stdin, "isatty", return_value=True), patch.object(sys.stdout, "isatty", return_value=True), \
             patch("builtins.input", side_effect=confirmation or (lambda _: "yes")), \
             patch.object(pennix, "_emit"), patch.object(owned_files, "replace", side_effect=write):
            return pennix.cmd_reconcile(args)

    def test_reconcile_recovers_partial_receipt_lock_without_installing(self):
        with self.assertRaisesRegex(OSError, "partial lock"):
            self.run_reconcile(fail_lock=True)
        self.assertEqual(apply_journal.read()["steps"][0]["status"], "unknown")
        self.assertEqual(owned_files.read_json(self.receipt_path)["version"], "2.0")
        self.assertEqual(self.run_reconcile(), 0)
        self.assertIsNone(apply_journal.read())
        self.assertEqual(json.loads((self.root / "components.lock.json").read_text())["components"]["example"]["version"], "2.0-1")

    def test_confirmation_drift_stops_before_journal_or_receipt_change(self):
        def confirm(_):
            (self.root / "workflow.config").write_text("concurrent input")
            return "yes"
        with self.assertRaisesRegex(pennix.CliError, "确认期间"):
            self.run_reconcile(confirmation=confirm)
        self.assertIsNone(apply_journal.read())
        self.assertEqual(owned_files.read_json(self.receipt_path), self.receipt)

    def test_resume_refuses_new_installed_identity(self):
        with self.assertRaises(OSError):
            self.run_reconcile(fail_lock=True)
        self.identity["version"] = "3.0-1"
        with self.assertRaisesRegex(pennix.CliError, "实际身份"):
            self.run_reconcile()
        self.assertIsNotNone(apply_journal.read())

    def test_windsurf_owner_inputs_do_not_read_central_secret(self):
        spec = {"values": {}, "secrets": {"api_key": "windsurf.api_key"}}
        with patch.object(connection_owners.config_model, "config_value", return_value="y"), \
             patch.object(connection_owners.secret_store, "get", side_effect=AssertionError("central secret read")):
            self.assertEqual(connection_owners._inputs("windsurf", spec, object(), self.root), ({"credential_mode": "owner"}, {}))

    def test_plugin_reconcile_reads_installed_cache_independently_of_old_marketplace(self):
        source = "https://example.test/ponytail.git"
        component = {"lock_source": "codex-marketplace:fixture", "plugin": {
            "id": "ponytail@ponytail", "marketplace": {"name": "ponytail", "source": source}}}
        market = self.root / "market"
        cache = self.root / "plugins/cache/ponytail/ponytail/5.3.3"
        for path, version in ((market, "5.3.2"), (cache, "5.3.3")):
            (path / ".codex-plugin").mkdir(parents=True)
            (path / ".codex-plugin/plugin.json").write_text(json.dumps({"name": "ponytail", "version": version}))
        installed = [{"pluginId": "ponytail@ponytail", "enabled": True, "version": "5.3.3", "source": {"url": source}}]
        dirty = False
        def native(args, **_kwargs):
            if args[0] == "git":
                text = ("?? changed.txt\n" if dirty else "") if "status" in args else ("a" if args[2] == str(market) else "b") * 40
            elif args[1:3] == ["plugin", "list"]:
                text = json.dumps({"installed": installed})
            else:
                text = json.dumps({"marketplaces": [{"name": "ponytail", "root": str(market), "marketplaceSource": {"source": source}}]})
            return SimpleNamespace(returncode=0, stdout=text, stderr="")
        with patch.object(engine.shutil, "which", return_value="/usr/bin/codex"), patch.object(engine.subprocess, "run", side_effect=native):
            self.assertIsNone(engine._plugin_snapshot(component, self.root))
            self.assertEqual(engine._plugin_snapshot(component, self.root, installed_only=True)["ref"], "b" * 40)
            dirty = True
            self.assertIsNone(engine._plugin_snapshot(component, self.root, installed_only=True))
            dirty = False
            installed.append({"pluginId": "another@ponytail"})
            self.assertIsNone(engine._plugin_snapshot(component, self.root, installed_only=True))

    def test_plugin_alignment_uses_only_native_exact_ref_and_checks_failure(self):
        component = {"plugin": {"id": "ponytail@ponytail", "marketplace": {"name": "ponytail", "source": "https://example.test/p.git"}}}
        target = {"source": "codex-marketplace:fixture", "version": "5.3.3", "ref": "b" * 40}
        with patch.object(engine, "_plugin_snapshot", side_effect=[None, target, target]), \
             patch.object(engine, "_prepare_plugin_marketplace"), patch.object(engine.shutil, "which", return_value="codex"), \
             patch.object(engine.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run:
            engine.align_plugin(component, self.root, target)
        self.assertEqual(run.call_args_list[0].args[0], ["codex", "plugin", "marketplace", "add", "https://example.test/p.git", "--ref", "b" * 40, "--json"])
        self.assertEqual(run.call_args_list[1].args[0], ["codex", "plugin", "add", "ponytail@ponytail", "--json"])
        with patch.object(engine, "_plugin_snapshot", side_effect=[None, target]), \
             patch.object(engine, "_prepare_plugin_marketplace"), patch.object(engine.shutil, "which", return_value="codex"), \
             patch.object(engine.subprocess, "run", return_value=SimpleNamespace(returncode=1)):
            with self.assertRaisesRegex(engine.BootstrapError, "journal retained"):
                engine.align_plugin(component, self.root, target)

    def test_stopped_daemon_is_compatible_with_enabled_autostart(self):
        paseo, home = self.root / "paseo", self.root / "paseo-home"
        replies = [SimpleNamespace(returncode=0, stdout=json.dumps({"pid": None, "home": str(home)}), stderr=""),
                   SimpleNamespace(returncode=0, stdout="LoadState=loaded\nUnitFileState=enabled\nActiveState=inactive\nResult=success\n", stderr="")]
        with patch.object(paseo_service.subprocess, "run", side_effect=replies):
            state = paseo_service.runtime(paseo, home, "login", False)
        self.assertEqual(state["state"], "current")
        self.assertFalse(state["running"])
        with patch.object(paseo_service, "_config_get", side_effect=[(True, "127.0.0.1:6767"), (True, {"mode": "all"})]), \
             patch.object(paseo_service, "_managed_skills", return_value=list(paseo_service.OFFICIAL_SKILLS)):
            observed = paseo_service.observe(paseo, home, "127.0.0.1:6767", "1.0", state)
        self.assertEqual(observed["state"], "current")
        self.assertEqual(observed["versions"]["state"], "not_run")
        self.assertFalse(observed["restart_required"])

    def test_bus_failure_is_unknown_not_disabled(self):
        replies = [SimpleNamespace(returncode=0, stdout='{"pid":null}', stderr=""),
                   SimpleNamespace(returncode=1, stdout="", stderr="Failed to connect to bus")]
        with patch.object(paseo_service.subprocess, "run", side_effect=replies):
            self.assertEqual(paseo_service.runtime(self.root / "paseo", self.root, "login", False)["state"], "unknown")

    def test_plan_keeps_stopped_login_autostart_and_existing_path(self):
        path = config_model.init(str(self.root / "workflow.config"), defconfig=str(
            Path(__file__).resolve().parents[1] / "config/defconfig-wsl"))
        kconf, _ = config_model.load(str(path))
        args = SimpleNamespace(component_lock={"components": {}})
        state = {"state": "current", "running": False, "healthy": False, "enabled": True}
        observed = {"state": "current", "restart_required": False, "skills_sync": {"state": "current"}}
        path_env.apply(str(self.root / ".local"))
        with patch.object(pennix, "_load", return_value=(kconf, path, {"components": {}})), \
             patch.object(paseo_service, "inspect", return_value="current"), \
             patch.object(paseo_service, "runtime", return_value=state), \
             patch.object(paseo_service, "observe", return_value=observed), \
             patch.object(pennix, "_relay_plan", return_value=None), \
             patch.object(connection_owners, "plan", return_value={}):
            plan = pennix._plan(args)
        self.assertEqual(plan["components"]["paseo-autostart"]["action"], "keep")
        self.assertFalse(plan["components"]["paseo-autostart"]["run"])
        self.assertEqual(plan["side_effects"]["path"]["action"], "keep")

    def test_legacy_unit_is_recognized_and_migrates_without_stopping_independent_daemon(self):
        paseo, home = self.root / "paseo", self.root / "paseo-home"
        paseo.touch()
        unit = paseo_service.unit_path()
        legacy = paseo_service._legacy_unit(paseo, home).encode()
        owned_files.replace(unit, legacy, None)
        receipt = {"schema": 1, "paseo": str(paseo), "home": str(home),
                   "unit_digest": hashlib.sha256(legacy).hexdigest()}
        owned_files.replace(paseo_service._receipt_path(), owned_files.json_bytes(receipt), None)
        self.assertEqual(paseo_service.inspect(paseo, home), "migration-required")
        runtime = {"state": "current", "running": True, "healthy": True, "enabled": True, "active": False}
        with patch.object(paseo_service, "runtime", return_value=runtime), \
             patch.object(paseo_service, "_systemctl") as systemctl, patch.object(paseo_service, "_run_daemon") as daemon:
            paseo_service.apply("login", paseo, home, "127.0.0.1:6767", False)
        daemon.assert_not_called()
        self.assertEqual([call.args for call in systemctl.call_args_list], [("daemon-reload",)])
        self.assertEqual(paseo_service.inspect(paseo, home), "current")
        with patch.object(paseo_service, "runtime", return_value=runtime), \
             patch.object(paseo_service, "_systemctl") as systemctl, patch.object(paseo_service, "_run_daemon") as daemon:
            paseo_service.apply("off", paseo, home, "127.0.0.1:6767", False)
        daemon.assert_not_called()
        self.assertNotIn("--now", str(systemctl.call_args_list))

    def test_native_restart_never_proxies_through_systemd_or_revives_stopped_daemon(self):
        for running in (False, True):
            state = {"state": "current", "running": running, "healthy": running}
            with patch.object(paseo_service, "runtime", return_value=state), \
                 patch.object(paseo_service, "assert_no_active_work"), \
                 patch.object(paseo_service, "_run_daemon") as daemon, patch.object(paseo_service, "_systemctl") as systemctl:
                paseo_service.restart(self.root / "paseo", self.root, "login")
            systemctl.assert_not_called()
            self.assertEqual(daemon.call_count, int(running))

    def test_active_legacy_migration_guards_work_and_recovers_partial_stop(self):
        paseo, home = self.root / "paseo", self.root / "paseo-home"
        paseo.touch()
        unit = paseo_service.unit_path()
        legacy = paseo_service._legacy_unit(paseo, home).encode()
        owned_files.replace(unit, legacy, None)
        owned_files.replace(paseo_service._receipt_path(), owned_files.json_bytes({
            "schema": 1, "paseo": str(paseo), "home": str(home),
            "unit_digest": hashlib.sha256(legacy).hexdigest()}), None)
        state = {"state": "current", "running": True, "healthy": True,
                 "enabled": True, "active": True, "pid": os.getpid()}
        group = Path(f"/proc/{os.getpid()}/cgroup").read_text().splitlines()[0].split(":", 2)[-1]
        with patch.object(paseo_service, "runtime", return_value=state), \
             patch.object(paseo_service.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=group)), \
             patch.object(paseo_service, "assert_no_active_work", side_effect=paseo_service.PaseoServiceError("active work")), \
             patch.object(paseo_service, "_systemctl") as systemctl:
            with self.assertRaisesRegex(paseo_service.PaseoServiceError, "active work"):
                paseo_service.apply("login", paseo, home, "127.0.0.1:6767", False)
            systemctl.assert_not_called()
        def control(*args):
            if args[0] == "stop":
                raise paseo_service.PaseoServiceError("partial stop failure")
        with patch.object(paseo_service, "runtime", return_value=state), \
             patch.object(paseo_service.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=group)), \
             patch.object(paseo_service, "assert_no_active_work"), \
             patch.object(paseo_service, "_systemctl", side_effect=control) as systemctl:
            with self.assertRaisesRegex(paseo_service.PaseoServiceError, "恢复失败"):
                paseo_service.apply("login", paseo, home, "127.0.0.1:6767", False)
        self.assertNotIn(("start", unit.stem), [call.args for call in systemctl.call_args_list])
        self.assertEqual(unit.read_bytes(), legacy)


if __name__ == "__main__":
    unittest.main()
