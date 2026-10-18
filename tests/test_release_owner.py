from pathlib import Path
import os
import tempfile
import unittest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import release_owner


def make_release(root: Path, version: str) -> Path:
    root.mkdir()
    (root / "VERSION").write_text(version + "\n", encoding="ascii")
    (root / "pennix").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (root / "pennix").chmod(0o700)
    for directory in ("scripts", "references", "config", "templates"):
        path = root / directory
        path.mkdir()
        (path / "owned").write_text(version, encoding="ascii")
    return root


class ReleaseOwnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.old = {key: os.environ.get(key) for key in ("HOME", "XDG_DATA_HOME", "XDG_STATE_HOME")}
        os.environ["HOME"] = str(self.home)
        os.environ["XDG_DATA_HOME"] = str(self.home / ".local/share")
        os.environ["XDG_STATE_HOME"] = str(self.home / ".local/state")
        self.original_home = Path.home
        Path.home = lambda: self.home

    def tearDown(self):
        Path.home = self.original_home
        for key, value in self.old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temp.cleanup()

    def test_upgrade_keeps_only_active_release_and_uninstall_removes_it(self):
        first = make_release(self.home / "source-1", "1.0.0")
        self.assertEqual(release_owner.install(first), "installed 1.0.0")
        entry = self.home / ".local/bin/pennix"
        self.assertEqual(entry.resolve(), self.home / ".local/share/pennix-lifecycle/releases/1.0.0/pennix")

        second = make_release(self.home / "source-2", "1.0.1")
        self.assertEqual(release_owner.install(second), "installed 1.0.1")
        releases = self.home / ".local/share/pennix-lifecycle/releases"
        self.assertEqual([path.name for path in releases.iterdir()], ["1.0.1"])
        self.assertEqual(release_owner.uninstall(), "uninstalled")
        self.assertFalse(entry.exists())
        self.assertFalse(releases.exists())

    def test_drifted_active_release_blocks_replacement(self):
        first = make_release(self.home / "source-1", "1.0.0")
        release_owner.install(first)
        current = (self.home / ".local/bin/pennix").resolve().parent
        (current / "references/owned").write_text("user change", encoding="utf-8")
        second = make_release(self.home / "source-2", "1.0.1")
        with self.assertRaises(release_owner.ReleaseError):
            release_owner.install(second)
        self.assertEqual((self.home / ".local/bin/pennix").resolve().parent, current)


if __name__ == "__main__":
    unittest.main()
