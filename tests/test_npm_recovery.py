"""A failed npm postcondition must not strand a verifiable recorded upgrade."""
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import _engine as engine
import owned_files


class NpmRecoveryTests(unittest.TestCase):
    def test_fixed_archive_recovers_only_trusted_unmodified_install(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
                "HOME": directory, "XDG_STATE_HOME": str(Path(directory) / "state")}):
            root = Path(directory)
            prefix = root / "prefix"
            package = prefix / "lib/node_modules/example"
            package.mkdir(parents=True)
            files = {"package.json": json.dumps({"name": "example", "version": "2.0"}).encode(),
                     "bin.js": b"console.log('2.0');"}
            archive = root / "fixed.tgz"
            with tarfile.open(archive, "w:gz") as bundle:
                for name, content in files.items():
                    (package / name).write_bytes(content)
                    entry = tarfile.TarInfo("package/" + name)
                    entry.size = len(content)
                    bundle.addfile(entry, io.BytesIO(content))
            component = {"delivery": "package", "lock_source": "npm:example:latest",
                         "probe": "example", "_lock_target": {"version": "2.0"},
                         "package": {"source": "npm", "name": "example", "prefix": str(prefix)}}
            old = {"source": "npm:example:latest", "version": "1.0"}
            receipt_path = engine._package_receipt_path("example")
            receipt = {"schema": 1, "component": "example", "source": "npm", "name": "example", "version": "1.0"}
            def reset():
                before = owned_files.read(receipt_path, private=True)
                owned_files.replace(receipt_path, owned_files.json_bytes(receipt), before)
            with patch.object(engine, "package_identity", return_value={"version": "2.0"}), \
                 patch.object(engine, "probe_component", return_value=("match", "2.0")), \
                 patch.object(engine, "_prefix", return_value=prefix):
                with self.assertRaisesRegex(engine.BootstrapError, "trusted"):
                    engine.recover_npm_receipt("example", component, old, archive)
                reset()
                with self.assertRaisesRegex(engine.BootstrapError, "trusted"):
                    engine.recover_npm_receipt("example", component, {**old, "source": "npm:other:latest"}, archive)
                (package / "bin.js").write_bytes(b"tampered")
                with self.assertRaisesRegex(engine.BootstrapError, "differ"):
                    engine.recover_npm_receipt("example", component, old, archive)
                self.assertEqual(owned_files.read_json(receipt_path), receipt)
                (package / "bin.js").unlink()
                outside = root / "redirected.js"
                outside.write_bytes(files["bin.js"])
                (package / "bin.js").symlink_to(outside)
                with self.assertRaisesRegex(engine.BootstrapError, "redirected"):
                    engine.recover_npm_receipt("example", component, old, archive)
                (package / "bin.js").unlink()
                (package / "bin.js").write_bytes(files["bin.js"])
                engine.recover_npm_receipt("example", component, old, archive)
                self.assertEqual(owned_files.read_json(receipt_path)["version"], "2.0")


if __name__ == "__main__":
    unittest.main()
