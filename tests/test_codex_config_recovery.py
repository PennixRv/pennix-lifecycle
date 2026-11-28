import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import apply_journal
import codex_config
import model_catalog
import owned_files

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"

CRASH = r'''
import os
from pathlib import Path
import apply_journal, codex_config, owned_files
home = Path.home() / ".codex"
path = home / "config.toml"
home.mkdir(mode=0o700)
path.write_text('model = "before"\nforeign = "keep"\n')
path.chmod(0o600)
journal = apply_journal.start("apply", None, "a" * 64, None,
    [{"id": "codex-config:configure", "component": "codex-config", "action": "configure", "target": {}}])
journal = codex_config.prepare_journal(journal, "codex-config:configure")
journal = apply_journal.started(journal, "codex-config:configure")
before = owned_files.read(path)
after = b'model = "after"\nforeign = "keep"\n'
record = owned_files.json_bytes({"schema": 3, "path": str(path), "before": {"model": "before"}, "after": {"model": "after"}})
target = os.environ["CRASH_POINT"]
replace = owned_files.replace
def hooked(pathname, content, previous, **kwargs):
    replace(pathname, content, previous, **kwargs)
    is_intent = pathname.name.startswith("codex-config-")
    is_journal = pathname == apply_journal.path() and b"codex_config_intent" in (content or b"")
    if ((target == "intent" and is_intent) or (target == "journal" and is_journal)
            or (target == "config" and pathname == path) or (target == "ownership" and pathname == codex_config._state_path())):
        os._exit(73)
owned_files.replace = hooked
if target == "before-intent":
    os._exit(73)
codex_config._commit(path, before, after, None, record)
os._exit(74)
'''

CATALOG_CRASH = r'''
import json, os, sys
from pathlib import Path
import apply_journal, codex_config, config_model, model_catalog, owned_files
sys.path.insert(0, str(Path(model_catalog.__file__).parents[1] / "tests"))
from test_model_catalog import ModelCatalogTests
fixture = ModelCatalogTests()
source = fixture.raw(fixture.source())
old = model_catalog.candidate(source, fixture.proof(source), b"old rule")
new = model_catalog.candidate(source, fixture.proof(source), b"new rule")
settings = config_model.init()
kconf, _ = config_model.load(str(settings))
kconf.syms["PENNIX_CODEX_MANAGED_CATALOG"].set_value("y")
values = config_model.values(kconf)
path = Path(os.environ["CODEX_HOME"]) / "config.toml"
owned_files.ensure_parent(path)
owned_files.replace(path, b'foreign = "keep"\n', None)
model_catalog.compatible = lambda *args: None
codex_config.apply(path, values, catalog_candidate=old)
(Path.home() / "catalogs.json").write_bytes(owned_files.json_bytes({
    "old": old["descriptor"], "new": new["descriptor"]}))
operation = os.environ["CATALOG_OPERATION"]
if operation == "repair":
    damaged = Path(model_catalog.reference(old["descriptor"]))
    damaged.chmod(0o600)
    damaged.write_bytes(b"synthetic damaged derived")
step_id = "codex-config:" + operation
journal = apply_journal.start({"configure": "update", "uninstall": "uninstall", "repair": "repair"}[operation],
    "codex-config", apply_journal.digest(settings), None,
    [{"id": step_id, "component": "codex-config", "action": operation, "target": {}}])
journal = codex_config.prepare_journal(journal, step_id)
apply_journal.started(journal, step_id)
replace = owned_files.replace
point = os.environ["CRASH_POINT"]
new_dir = Path(model_catalog.reference(new["descriptor"])).parent
old_dir = Path(model_catalog.reference(old["descriptor"])).parent
def hooked(destination, content, previous, **kwargs):
    replace(destination, content, previous, **kwargs)
    if ((point == "intent" and destination.name.startswith("codex-config-"))
            or (point == "journal" and destination == apply_journal.path()
                and b"codex_config_intent" in (content or b""))
            or (point == "source" and destination == new_dir / "source.json")
            or (point == "manifest" and destination == new_dir / "manifest.json")
            or (point == "config" and destination == path)
            or (point == "ownership" and destination == codex_config._state_path())
            or (point == "repair-derived" and destination == old_dir / "derived.json")
            or (point == "delete" and destination.parent == old_dir and content is None)):
        os._exit(73)
owned_files.replace = hooked
if operation == "configure":
    codex_config.apply(path, values, catalog_candidate=new)
elif operation == "repair":
    review = codex_config.catalog_repair_review(path)
    codex_config.repair_catalog(path, review["catalog_repair_sha256"])
else:
    codex_config.uninstall(path)
os._exit(74)
'''


class CodexConfigRecoveryTests(unittest.TestCase):
    def environment(self, root):
        return {"HOME": str(root), "CODEX_HOME": str(root / ".codex"),
                "XDG_STATE_HOME": str(root / "state"), "PYTHONPATH": str(SCRIPTS)}

    def test_process_exit_at_every_config_commit_boundary_is_recoverable_in_the_same_journal(self):
        for point in ("before-intent", "intent", "journal", "config", "ownership", "done"):
            with self.subTest(point=point), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                env = {**os.environ, **self.environment(root), "CRASH_POINT": point}
                killed = subprocess.run([sys.executable, "-c", CRASH], env=env, capture_output=True)
                self.assertEqual(killed.returncode, 74 if point == "done" else 73, killed.stderr)
                with patch.dict(os.environ, self.environment(root)):
                    journal = apply_journal.read()
                    step = journal["steps"][0]
                    path = root / ".codex/config.toml"
                    transaction = journal["transaction"]
                    # No reference was committed before the first active mutation.
                    if point in {"before-intent", "intent"}:
                        evidence = codex_config.recover(path, journal, step, "not-started")
                        self.assertFalse(evidence["desired_applied"])
                    else:
                        if point in {"ownership", "done"}:
                            self.assertTrue(codex_config.recover(path, journal, step, "completed")["desired_applied"])
                        else:
                            with self.assertRaises(codex_config.CodexConfigError):
                                codex_config.recover(path, journal, step, "completed")
                        # Unrelated concurrent fields survive restoring the exact managed field.
                        path.write_text(path.read_text() + 'concurrent = "preserve"\n')
                        restored = codex_config.recover(path, journal, step, "restored")
                        self.assertFalse(restored["desired_applied"])
                        self.assertIn('concurrent = "preserve"', path.read_text())
                    self.assertIn('model = "before"', path.read_text())
                    self.assertIn('foreign = "keep"', path.read_text())
                    self.assertIsNone(owned_files.read(codex_config._state_path(), private=True))
                    self.assertEqual(apply_journal.read()["transaction"], transaction)

    def test_external_changes_or_missing_bound_intent_are_not_overwritten(self):
        for change in ("config", "record", "intent"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                env = {**os.environ, **self.environment(root), "CRASH_POINT": "config"}
                result = subprocess.run([sys.executable, "-c", CRASH], env=env, capture_output=True)
                self.assertEqual(result.returncode, 73, result.stderr)
                with patch.dict(os.environ, self.environment(root)):
                    journal = apply_journal.read()
                    step = journal["steps"][0]
                    path = root / ".codex/config.toml"
                    if change == "config":
                        path.write_text('model = "external"\nforeign = "keep"\n')
                    elif change == "record":
                        owned_files.replace(codex_config._state_path(), b'{"external":true}', None)
                    else:
                        codex_config._intent_path(journal["transaction"], step["id"]).unlink()
                    before = path.read_bytes()
                    with self.assertRaises(codex_config.CodexConfigError):
                        codex_config.recover(path, journal, step, "restored")
                    self.assertEqual(path.read_bytes(), before)
                    self.assertEqual(apply_journal.read(), journal)

    def test_numeric_value_cannot_satisfy_a_boolean_recovery_postcondition(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.dict(os.environ, self.environment(root)):
                path = root / ".codex/config.toml"
                owned_files.replace(path, b"[features]\nhooks = false\n", None)
                journal = apply_journal.start("apply", None, "a" * 64, None, [{
                    "id": "codex-config:configure", "component": "codex-config", "action": "configure", "target": {}}])
                journal = codex_config.prepare_journal(journal, "codex-config:configure")
                apply_journal.started(journal, "codex-config:configure")
                record = owned_files.json_bytes({"schema": 3, "path": str(path),
                    "before": {"features.hooks": False}, "after": {"features.hooks": True}})
                codex_config._commit(path, path.read_bytes(), b"[features]\nhooks = true\n", None, record)
                journal = apply_journal.read()
                path.write_text("[features]\nhooks = 1\n")
                before = path.read_bytes()
                with self.assertRaisesRegex(codex_config.CodexConfigError, "第三方"):
                    codex_config.recover(path, journal, journal["steps"][0], "completed")
                self.assertEqual(path.read_bytes(), before)

    def test_catalog_generation_switch_survives_real_process_exit_at_each_commit_boundary(self):
        for point in ("intent", "journal", "source", "manifest", "config", "ownership", "done"):
            with self.subTest(point=point), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                environment = {**os.environ, **self.environment(root),
                               "CATALOG_OPERATION": "configure", "CRASH_POINT": point}
                killed = subprocess.run([sys.executable, "-c", CATALOG_CRASH], env=environment, capture_output=True)
                self.assertEqual(killed.returncode, 74 if point == "done" else 73, killed.stderr)
                with patch.dict(os.environ, self.environment(root)), patch.object(model_catalog, "compatible"):
                    descriptors = json.loads((root / "catalogs.json").read_bytes())
                    path = root / ".codex/config.toml"
                    journal = apply_journal.read()
                    step = journal["steps"][0]
                    if point == "intent":
                        codex_config.recover(path, journal, step, "not-started")
                    else:
                        if point in {"ownership", "done"}:
                            self.assertTrue(codex_config.recover(path, journal, step, "completed")["desired_applied"])
                        elif point in {"source", "manifest"}:
                            with self.assertRaisesRegex(codex_config.CodexConfigError, "已经开始"):
                                codex_config.recover(path, journal, step, "not-started")
                        codex_config.recover(path, journal, step, "restored")
                    self.assertEqual(codex_config._load_record(path)["catalog"], descriptors["old"])
                    model_catalog.read_generation(descriptors["old"])
                    self.assertFalse(Path(model_catalog.reference(descriptors["new"])).parent.exists())

    def test_catalog_uninstall_deletion_can_finish_only_through_same_bound_recovery(self):
        for point in ("config", "ownership", "delete", "done"):
            with self.subTest(point=point), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                environment = {**os.environ, **self.environment(root),
                               "CATALOG_OPERATION": "uninstall", "CRASH_POINT": point}
                killed = subprocess.run([sys.executable, "-c", CATALOG_CRASH], env=environment, capture_output=True)
                self.assertEqual(killed.returncode, 74 if point == "done" else 73, killed.stderr)
                with patch.dict(os.environ, self.environment(root)), patch.object(model_catalog, "compatible"):
                    path = root / ".codex/config.toml"
                    journal = apply_journal.read()
                    step = journal["steps"][0]
                    old = json.loads((root / "catalogs.json").read_bytes())["old"]
                    if point == "config":
                        codex_config.recover(path, journal, step, "restored")
                        model_catalog.read_generation(old)
                    else:
                        if point == "delete":
                            with self.assertRaises(codex_config.CodexConfigError):
                                codex_config.recover(path, journal, step, "restored")
                        self.assertTrue(codex_config.recover(path, journal, step, "completed")["desired_applied"])
                        self.assertIsNone(codex_config._load_record(path))
                        self.assertNotIn("model_catalog_json", path.read_text())
                        self.assertFalse(Path(model_catalog.reference(old)).parent.exists())

    def test_catalog_repair_crash_completes_exact_outputs_and_never_restores_unknown_damage(self):
        for point in ("intent", "journal", "repair-derived", "done", "third-value"):
            with self.subTest(point=point), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                environment = {**os.environ, **self.environment(root),
                               "CATALOG_OPERATION": "repair",
                               "CRASH_POINT": "journal" if point == "third-value" else point}
                killed = subprocess.run([sys.executable, "-c", CATALOG_CRASH], env=environment, capture_output=True)
                self.assertEqual(killed.returncode, 74 if point == "done" else 73, killed.stderr)
                with patch.dict(os.environ, self.environment(root)), patch.object(model_catalog, "compatible"):
                    path = root / ".codex/config.toml"
                    descriptor = json.loads((root / "catalogs.json").read_bytes())["old"]
                    derived = Path(model_catalog.reference(descriptor))
                    journal = apply_journal.read()
                    step = journal["steps"][0]
                    if point == "intent":
                        codex_config.recover(path, journal, step, "not-started")
                        self.assertEqual(derived.read_bytes(), b"synthetic damaged derived")
                        continue
                    if point == "third-value":
                        derived.write_bytes(b"third-party content")
                        with self.assertRaisesRegex(codex_config.CodexConfigError, "第三方"):
                            codex_config.recover(path, journal, step, "completed")
                        self.assertEqual(derived.read_bytes(), b"third-party content")
                        continue
                    with self.assertRaisesRegex(codex_config.CodexConfigError, "未知损坏"):
                        codex_config.recover(path, journal, step, "restored")
                    if point == "journal":
                        codex_config.recover(path, journal, step, "not-started")
                    else:
                        with self.assertRaisesRegex(codex_config.CodexConfigError, "已写入"):
                            codex_config.recover(path, journal, step, "not-started")
                    self.assertTrue(codex_config.recover(path, journal, step, "completed")["desired_applied"])
                    model_catalog.read_generation(descriptor)
                    self.assertEqual(codex_config._load_record(path)["catalog"], descriptor)


if __name__ == "__main__":
    unittest.main()
