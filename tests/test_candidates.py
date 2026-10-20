import base64
import hashlib
import io
import json
from pathlib import Path
import sys
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import candidates


class CandidateTests(unittest.TestCase):
    @staticmethod
    def _archive_bytes(entries):
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w:gz") as archive:
            for name, value in entries:
                member = tarfile.TarInfo(name)
                member.size = len(value)
                archive.addfile(member, io.BytesIO(value))
        return output.getvalue()

    def test_beta_tag_resolves_context_mode_pennix_8_and_lock_digest(self):
        raw = b"context-mode pennix beta package"
        metadata = {"name": "@pennixrv/context-mode", "version": "1.0.169-pennix.8", "dist": {
            "tarball": "https://registry.test/context-mode.tgz",
            "integrity": "sha512-" + base64.b64encode(hashlib.sha512(raw).digest()).decode(),
        }}
        component = {"lock_source": "npm:@pennixrv/context-mode:beta", "package": {
            "name": "@pennixrv/context-mode", "registry": "https://registry.test/", "tag": "beta"}}
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(candidates, "_json", return_value=metadata) as query, \
                 patch.object(candidates.owned_files, "download", side_effect=lambda url, path: path.write_bytes(raw)):
                value = candidates.resolve_npm(component, Path(directory))
                locked = {"version": value["version"], "sha256": value["sha256"]}
                candidates.resolve_npm(component, Path(directory), locked)

        self.assertEqual(query.call_args_list[0].args[0], "https://registry.test/%40pennixrv%2Fcontext-mode/beta")
        self.assertEqual(value["version"], "1.0.169-pennix.8")
        self.assertEqual(value["source"], "npm:@pennixrv/context-mode:beta")
        self.assertEqual(value["sha256"], hashlib.sha256(raw).hexdigest())

    def test_npm_candidate_is_staged_and_both_registry_integrities_are_checked(self):
        raw = b"verified npm tarball"
        metadata = {"name": "example", "version": "2.0.0", "dist": {
            "tarball": "https://registry.test/example.tgz",
            "integrity": "sha512-" + base64.b64encode(hashlib.sha512(raw).digest()).decode(),
        }}
        component = {"lock_source": "npm:example:latest", "package": {
            "name": "example", "registry": "https://registry.test/", "tag": "latest"}}
        with tempfile.TemporaryDirectory() as directory:
            def fetch(url, path):
                self.assertEqual(url, "https://registry.test/example.tgz")
                path.write_bytes(raw)
            with patch.object(candidates, "_json", return_value=metadata) as query, \
                 patch.object(candidates.owned_files, "download", side_effect=fetch):
                value = candidates.resolve_npm(component, Path(directory))
                locked = {"version": "2.0.0", "sha256": value["sha256"]}
                candidates.resolve_npm(component, Path(directory), locked)
            self.assertEqual(query.call_args_list[0].args[0], "https://registry.test/example/latest")
            self.assertEqual(Path(value["path"]).read_bytes(), raw)
            self.assertEqual(value["sha256"], hashlib.sha256(raw).hexdigest())

    def test_locked_npm_candidate_rejects_digest_or_version_drift(self):
        raw = b"different tarball"
        metadata = {"name": "example", "version": "2.0.0", "dist": {
            "tarball": "https://registry.test/example.tgz",
            "integrity": "sha512-" + base64.b64encode(hashlib.sha512(raw).digest()).decode(),
        }}
        component = {"lock_source": "npm:example:latest", "package": {
            "name": "example", "registry": "https://registry.test/", "tag": "latest"}}
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(candidates, "_json", return_value=metadata), \
                 patch.object(candidates.owned_files, "download", side_effect=lambda url, path: path.write_bytes(raw)):
                with self.assertRaises(candidates.CandidateError):
                    candidates.resolve_npm(component, Path(directory), {"version": "2.0.0", "sha256": "0" * 64})
                with self.assertRaises(candidates.CandidateError):
                    candidates.resolve_npm(component, Path(directory), {"version": "1.0.0"})

    def test_archive_digest_uses_the_same_lock_identity_as_file_digest(self):
        digest = "a" * 64
        candidate = {"source": "git:example/repo:runtime", "ref": "b" * 40, "sha256": digest}
        locked = candidates.lock_entry(candidate)
        self.assertEqual(locked["sha256"], digest)
        self.assertEqual(candidates._check_target(candidate, locked), candidate)

    def test_archive_json_accepts_standard_npm_root_and_prefixed_root(self):
        payload = json.dumps({"name": "example", "version": "2.0.0"}).encode()
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "package.tgz"
            archive.write_bytes(self._archive_bytes([("package/package.json", payload)]))
            self.assertEqual(candidates._archive_json(archive, "package/package.json")["version"], "2.0.0")

            archive.write_bytes(self._archive_bytes([("release/package/package.json", payload)]))
            self.assertEqual(candidates._archive_json(archive, "package/package.json")["name"], "example")

    def test_release_candidate_rejects_duplicate_or_wrong_identity(self):
        payload = json.dumps({"name": "wrong", "version": "2.0.0"}).encode()
        archive_bytes = self._archive_bytes([("package/package.json", payload)])
        component = {"lock_source": "release:example", "package": {
            "name": "example", "asset": {"repo": "example/repo", "name_prefix": "pkg-", "suffix": ".tgz"}}}
        release = {"tagName": "v2.0.0", "assets": [{"name": "pkg-2.0.0.tgz"}]}

        def download(command, **_kwargs):
            Path(command[-1]).write_bytes(archive_bytes)
            return subprocess.CompletedProcess(command, 0, "", "")

        with tempfile.TemporaryDirectory() as directory, \
             patch.object(candidates, "_gh_release", return_value=release), \
             patch.object(candidates, "_run", side_effect=download):
            with self.assertRaises(candidates.CandidateError):
                candidates.resolve_release(component, Path(directory))

            duplicate = self._archive_bytes([
                ("package/package.json", json.dumps({"name": "example", "version": "2.0.0"}).encode()),
                ("package/package.json", json.dumps({"name": "example", "version": "2.0.0"}).encode()),
            ])
            archive = Path(directory) / "duplicate.tgz"
            archive.write_bytes(duplicate)
            with self.assertRaises(candidates.CandidateError):
                candidates._archive_json(archive, "package/package.json")


if __name__ == "__main__":
    unittest.main()
