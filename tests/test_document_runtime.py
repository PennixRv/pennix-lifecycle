"""Document dependencies share native package ownership and recovery arguments."""
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
import host
import pennix

ROOT = Path(__file__).resolve().parents[1]


class DocumentRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.catalog = engine.load_catalog(ROOT / "references/catalog.json")
        self.component = self.catalog["components"]["markitdown"]

    def test_uv_fixed_wheel_and_named_requirements_keep_extras_and_pins(self):
        options = dict(extras=["pdf", "xlsx"], requirements=["pypdf==7.0.0"])
        for package, requirement in [("/tmp/package.whl", "/tmp/package.whl[pdf,xlsx]"),
                                     ("markitdown==0.1.9", "markitdown[pdf,xlsx]==0.1.9")]:
            self.assertEqual(host.package_install_command("uv", package, **options),
                             ["uv", "tool", "install", "--force", "--python", "/usr/bin/python3",
                              "--no-python-downloads", requirement, "--with", "pypdf==7.0.0"])
        for invalid in ("pypdf", "pypdf>=7", "pypdf @ https://example.test/a.whl"):
            with self.assertRaises(ValueError):
                host.package_install_command("uv", "markitdown", requirements=[invalid])

    def test_uv_install_upgrade_and_retry_use_same_declared_runtime(self):
        c = self.component
        args = SimpleNamespace(codex_home=Path("/tmp/codex"), explicit_update=True,
                               candidate={"path": "/tmp/fixed.whl"})
        for operation in ("install", "upgrade", "install"):
            with patch.object(host, "detect_host", return_value={"supported": True, "installers": {"uv": "uv"}}), \
                 patch.object(engine, "probe_component", side_effect=[("missing", None), ("match", "0.1.9"), ("match", "0.1.9")]), \
                 patch.object(engine, "package_owned", return_value=False), \
                 patch.object(engine, "_write_package_receipt") as receipt, \
                 patch.object(engine.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run:
                self.assertEqual(engine.component_operation(args, {}, "markitdown", c, operation), "changed")
                self.assertEqual(run.call_args.args[0], host.package_install_command(
                    "uv", "/tmp/fixed.whl", extras=c["package"]["extras"], requirements=c["package"]["with"]))
                receipt.assert_called_once()
        with patch.object(host, "detect_host", return_value={"supported": True, "installers": {"uv": "uv"}}), \
             patch.object(engine, "probe_component", return_value=("missing", None)), \
             patch.object(engine, "package_owned", return_value=False), \
             patch.object(engine, "_write_package_receipt") as receipt, \
             patch.object(engine.subprocess, "run", return_value=SimpleNamespace(returncode=1)):
            with self.assertRaisesRegex(engine.BootstrapError, "install failed"):
                engine.component_operation(args, {}, "markitdown", c, "install")
            receipt.assert_not_called()

    def test_uv_identity_rejects_incomplete_or_changed_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tool = root / "markitdown"
            (tool / "bin").mkdir(parents=True)
            executable = tool / "bin/markitdown"
            executable.touch()
            c = copy.deepcopy(self.component)
            c["package"].update(extras=["pdf"], **{"with": ["pypdf==7.0.0"], "imports": {"pypdf": "pypdf"}})
            receipt = tool / "uv-receipt.toml"
            valid = ('[tool]\nrequirements = [{name="markitdown", extras=["pdf"]}, '
                     '{name="pypdf", specifier="==7.0.0"}]\n'
                     f'entrypoints = [{{name="markitdown", from="markitdown", install-path="{executable}"}}]\n')
            receipt.write_text(valid)
            with patch.object(engine, "_command", return_value=str(executable)), \
                 patch.object(engine, "_version", return_value="0.1.9"), \
                 patch.object(engine.subprocess, "run") as run:
                run.side_effect = [SimpleNamespace(returncode=0, stdout=str(root)), SimpleNamespace(returncode=0)]
                self.assertEqual(engine.package_identity(c)["name"], "markitdown")
                for bad in (valid.replace('extras=["pdf"]', 'extras=[]'),
                            valid.replace('==7.0.0', '==6.0.0')):
                    receipt.write_text(bad)
                    run.side_effect = [SimpleNamespace(returncode=0, stdout=str(root))]
                    with self.assertRaises(engine.BootstrapError):
                        engine.package_identity(c)
                receipt.write_text(valid)
                run.side_effect = [SimpleNamespace(returncode=0, stdout=str(root)), SimpleNamespace(returncode=1)]
                with self.assertRaises(engine.BootstrapError):
                    engine.package_identity(c)

    def test_language_data_has_native_identity_without_fake_executable(self):
        c = self.catalog["components"]["tesseract-chi-sim"]
        name = c["package"]["name"]
        def run(command, **kwargs):
            self.assertIn(command[1], {"-Q", "-Qn"})
            return SimpleNamespace(returncode=0, stdout=f"{name} 2:4.1.0-5\n")
        with patch.object(engine.subprocess, "run", side_effect=run):
            identity = engine.package_identity(c)
            self.assertEqual(identity["command"], "")
            self.assertEqual(engine._version(c, ""), "2:4.1.0-5")
        with patch.object(engine, "package_absent", return_value=True):
            self.assertEqual(engine.probe_component(c), ("missing", None))
        with patch.object(engine, "package_absent", return_value=False), \
             patch.object(engine, "package_identity", side_effect=engine.BootstrapError("bad origin")):
            self.assertEqual(engine.probe_component(c)[0], "blocked")

    def test_existing_external_document_dependency_is_kept_without_lock_or_receipt(self):
        c = self.catalog["components"]["poppler"]
        args = SimpleNamespace(component_lock={"components": {}})
        values = {"PENNIX_CODEX_HOME": "/tmp/codex", "PENNIX_PASEO_HOME": "/tmp/paseo",
                  "PENNIX_PASEO_LISTEN": "127.0.0.1:6767", "PENNIX_SECRETS_FILE": "/tmp/secrets"}
        with patch.object(pennix, "_load", return_value=(object(), Path("/tmp/config"), {"components": {"poppler": c}, "configuration_targets": {}})), \
             patch.object(config_model, "selected_components", return_value={"poppler"}), \
             patch.object(config_model, "config_value", side_effect=lambda _, k: values.get(k, "n")), \
             patch.object(config_model, "choice_value", return_value="off"), \
             patch.object(engine, "package_lock_migration", return_value=None), \
             patch.object(pennix, "_component_status", return_value={"state": "match", "ownership": "external"}), \
             patch.object(pennix, "_relay_plan", return_value=None), \
             patch.object(pennix.paseo_service, "inspect", return_value="absent"), \
             patch.object(pennix.paseo_service, "runtime", return_value={"state": "current", "running": False}), \
             patch.object(pennix.paseo_service, "observe", return_value={"state": "current", "restart_required": False}), \
             patch.object(pennix.connection_owners, "plan", return_value={}):
            result = pennix._plan(args)["components"]["poppler"]
        self.assertEqual((result["action"], result["ownership"]), ("keep", "external"))
        with patch.object(host, "detect_host", return_value={"supported": True}), \
             patch.object(engine, "probe_component", return_value=("match", "26.10.0-2")), \
             patch.object(engine, "package_owned", return_value=False), \
             patch.object(engine.subprocess, "run") as run:
            with self.assertRaisesRegex(engine.BootstrapError, "external"):
                engine.component_operation(SimpleNamespace(codex_home=Path("/tmp/codex")), {}, "poppler", c, "uninstall")
            run.assert_not_called()

    def test_catalog_rejects_unfixed_runtime_and_identity_policy_on_uv(self):
        for change in ({"with": ["pypdf"]}, {"identity_only": True}):
            catalog = json.loads((ROOT / "references/catalog.json").read_text())
            catalog["components"]["markitdown"]["package"].update(change)
            with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as stream:
                json.dump(catalog, stream); stream.flush()
                with self.assertRaises(engine.BootstrapError):
                    engine.load_catalog(Path(stream.name))

    def test_dependency_resolution_requires_full_upgrade_and_preserves_receipt_on_rejection(self):
        c = self.catalog["components"]["pandoc"]
        with patch.object(host, "detect_host", return_value={"supported": True}), \
             patch.object(engine, "probe_component", return_value=("missing", None)), \
             patch.object(engine, "package_owned", return_value=False), \
             patch.object(engine.subprocess, "run") as run, \
             patch.object(engine, "_write_package_receipt") as receipt:
            with self.assertRaisesRegex(engine.BootstrapError, "full system upgrade"):
                engine.component_operation(SimpleNamespace(codex_home=Path("/tmp/codex")), {}, "pandoc", c, "install")
            run.assert_not_called()
            receipt.assert_not_called()


if __name__ == "__main__":
    unittest.main()
