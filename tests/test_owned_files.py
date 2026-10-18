import io
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import owned_files as files


class OwnedFilesTests(unittest.TestCase):
    def test_symlink_parent_and_concurrent_write_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "external"
            target.mkdir()
            (root / "link").symlink_to(target)
            with self.assertRaises(files.OwnershipError):
                files.replace(root / "link/file", b"new", None)
            path = target / "file"
            path.write_bytes(b"external")
            with self.assertRaises(files.OwnershipError):
                files.replace(path, b"new", b"old")
            self.assertEqual(path.read_bytes(), b"external")

    def test_archive_rejects_traversal_links_and_duplicates(self):
        for names in [["root/../escape"], ["root/link"], ["root/file", "root/file"]]:
            with self.subTest(names=names), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                archive = root / "input.tar.gz"
                with tarfile.open(archive, "w:gz") as outgoing:
                    for name in names:
                        entry = tarfile.TarInfo(name)
                        if name.endswith("link"):
                            entry.type = tarfile.SYMTYPE
                            entry.linkname = "/etc/passwd"
                        outgoing.addfile(entry, io.BytesIO(b""))
                with self.assertRaises(files.OwnershipError):
                    files.extract(archive, root / "output")
                self.assertFalse((root / "escape").exists())

    def test_private_json_rejects_duplicate_keys_and_public_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text('{"key":1,"key":2}')
            path.chmod(0o600)
            with self.assertRaises(files.OwnershipError):
                files.read_json(path)
            path.write_text('{}')
            path.chmod(0o644)
            with self.assertRaises(files.OwnershipError):
                files.read_json(path)
