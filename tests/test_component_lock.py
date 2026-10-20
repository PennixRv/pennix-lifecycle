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


if __name__ == "__main__":
    unittest.main()
