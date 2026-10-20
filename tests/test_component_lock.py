import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import component_lock


class ComponentLockTests(unittest.TestCase):
    def test_lock_roundtrip_is_private_and_rejects_unknown_or_changed_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "components.lock.json"
            sources = {"package": "npm:example/package:latest"}
            value = component_lock.read(path, sources)
            entry = {"source": sources["package"], "version": "1.2.3", "sha256": "a" * 64}
            component_lock.write(path, component_lock.set_entry(value, "package", entry, sources), sources)
            self.assertEqual(component_lock.read(path, sources)["components"]["package"], entry)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(component_lock.LockError):
                component_lock.read(path, {"package": "npm:example/package:beta"})
            with self.assertRaises(component_lock.LockError):
                component_lock.set_entry(value, "unknown", entry, sources)

    def test_lock_rejects_unsupported_shape_and_weak_target_identity(self):
        sources = {"package": "npm:example/package:latest"}
        for value in [
            {"schema": 2, "components": {}},
            {"schema": 1, "components": {"package": {"source": sources["package"]}}},
            {"schema": 1, "components": {"package": {"source": sources["package"], "sha256": "bad"}}},
            {"schema": 1, "components": {"other": {"source": "unknown", "version": "1"}}},
        ]:
            with self.subTest(value=value), self.assertRaises(component_lock.LockError):
                component_lock.validate(value, sources)

    def test_declared_source_alias_reads_legacy_lock_and_writes_canonical_source(self):
        canonical = "npm:example:beta"
        legacy = "npm:example:latest"
        sources = {"package": (canonical, legacy)}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "components.lock.json"
            legacy_entry = {"source": legacy, "version": "1.0.0", "sha256": "a" * 64}
            path.write_text(json.dumps({"schema": 1, "components": {"package": legacy_entry}}), encoding="utf-8")
            path.chmod(0o600)

            value = component_lock.read(path, sources)
            with self.assertRaises(component_lock.LockError):
                component_lock.read(path, {"package": (canonical, "npm:example:preview")})

            current_entry = {"source": canonical, "version": "1.1.0", "sha256": "b" * 64}
            updated = component_lock.set_entry(value, "package", current_entry, sources)
            component_lock.write(path, updated, sources)

            self.assertEqual(component_lock.read(path, sources)["components"]["package"], current_entry)


if __name__ == "__main__":
    unittest.main()
