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

    def test_inactive_complete_releases_are_cleaned_after_switch(self):
        release_owner.install(make_release(self.home / "source-1", "1.0.0"))
        releases = self.home / ".local/share/pennix-lifecycle/releases"
        make_release(releases / "0.9.0", "0.9.0")
        release_owner.install(make_release(self.home / "source-2", "1.0.1"))
        self.assertEqual([item.name for item in releases.iterdir()], ["1.0.1"])

    def test_unknown_resource_in_inactive_release_blocks_before_switch(self):
        release_owner.install(make_release(self.home / "source-1", "1.0.0"))
        releases = self.home / ".local/share/pennix-lifecycle/releases"
        stale = make_release(releases / "0.9.0", "0.9.0")
        (stale / "user-data").write_text("keep")
        with self.assertRaises(release_owner.ReleaseError):
            release_owner.install(make_release(self.home / "source-2", "1.0.1"))
        self.assertEqual((self.home / ".local/bin/pennix").resolve().parent.name, "1.0.0")
        self.assertTrue((stale / "user-data").exists())

    def test_old_installations_without_receipts_are_removed_only_after_switch(self):
        releases, entry = release_owner._locations()
        releases.mkdir(parents=True)
        entry.parent.mkdir(parents=True)
        for version in ("0.1.1", "0.1.6"):
            old = make_release(releases / version, version)
            (old / "VERSION").unlink()
            for name in (".gitignore", "README.md", "bootstrap.sh"):
                (old / name).write_text("old installation\n")
            (old / "tests").mkdir()
        entry.symlink_to(releases / "0.1.6/pennix")
        release_owner.install(make_release(self.home / "source", "0.2.0"))
        self.assertEqual(entry.resolve().parent.name, "0.2.0")
        self.assertEqual([item.name for item in releases.iterdir()], ["0.2.0"])

    def test_old_installation_with_extra_content_is_not_adopted(self):
        releases, entry = release_owner._locations()
        releases.mkdir(parents=True)
        entry.parent.mkdir(parents=True)
        old = make_release(releases / "0.1.6", "0.1.6")
        (old / "user-data").write_text("keep")
        entry.symlink_to(old / "pennix")
        with self.assertRaises(release_owner.ReleaseError):
            release_owner.install(make_release(self.home / "source", "0.2.0"))
        self.assertEqual(entry.resolve().parent, old)
        self.assertTrue((old / "user-data").exists())


if __name__ == "__main__":
    unittest.main()
