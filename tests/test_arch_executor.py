"""Exercise the privileged endpoint's wire validation without elevating privileges."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))
import owned_files

spec = importlib.util.spec_from_file_location("arch_commit", PROJECT / "native/arch_commit.py")
arch_commit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(arch_commit)


class ArchExecutorRequestTest(unittest.TestCase):
    transaction = "a" * 32
    uid = 1000

    def request(self):
        row = {"repo": "extra", "name": "sample", "installed": "1-1", "target": "2-1",
               "filename": "sample-2-1-any.pkg.tar.zst", "sha256": "2" * 64}
        return {"protocol": "pennix-arch-v1", "step": "system-upgrade", "uid": self.uid,
                "transaction": self.transaction, "target": {
                    "installed": {"sample": "1-1", "lib": "2-1"},
                    "native_review": {"requested": ["sample"], "transaction": [row], "policy": {}}}}

    def parse(self, value, *, digest=None):
        data = value if isinstance(value, bytes) else owned_files.json_bytes(value)
        return arch_commit.parse_request(data, self.transaction, digest or hashlib.sha256(data).hexdigest(), self.uid)

    def test_exact_request_binds_installed_set_and_arch_package(self):
        baseline, targets, rows, _, local = self.parse(self.request())
        self.assertEqual([(item.name, item.version) for item in baseline],
                         [(b"lib", b"2-1"), (b"sample", b"1-1")])
        self.assertEqual(targets, [b"sample"])
        self.assertEqual((rows[0].repo, rows[0].name, rows[0].version), (b"extra", b"sample", b"2-1"))
        self.assertEqual(local, {})

    def test_local_file_is_bound_to_fixed_approval_and_never_an_official_request(self):
        request = self.request()
        review = request["target"]["native_review"]
        row = review["transaction"][0]
        row["repo"] = None
        approval = {"version": row["target"], "filename": row["filename"], "sha256": row["sha256"],
                    "signer": "F" * 40, "ref": "a" * 40, "signature_sha256": "3" * 64}
        review["local"] = {"sample": approval}
        with self.assertRaises(ValueError):
            self.parse(request)
        review["requested"] = []
        _, targets, rows, _, local = self.parse(request)
        self.assertEqual(targets, [])
        self.assertIsNone(rows[0].repo)
        import arch_policy
        fixed_dir = Path("/var/lib/pennix-aur/sample")
        with patch.object(arch_policy, "local_package", return_value=(fixed_dir, approval)) as approved:
            self.assertEqual(arch_commit.local_approvals(local, arch_policy), ({"sample": fixed_dir}, b"F" * 40))
            approved.assert_called_once_with("sample")
            with self.assertRaises(ValueError):
                arch_commit.local_approvals({"sample": {**approval, "signer": "A" * 40}}, arch_policy)
        review["local"] = {"unreviewed": approval}
        with self.assertRaises(ValueError):
            self.parse(request)

    def test_digest_origin_argv_and_duplicate_fields_fail_closed(self):
        request = self.request()
        with self.assertRaises(ValueError):
            self.parse(request, digest="f" * 64)
        for key, bad in [("uid", 1001), ("uid", True), ("transaction", "b" * 32), ("step", "other")]:
            with self.subTest(key=key, bad=bad), self.assertRaises(ValueError):
                self.parse({**request, key: bad})
        duplicate = json.dumps(request).replace('"step":', '"step": "system-upgrade", "step":', 1)
        with self.assertRaises(ValueError):
            self.parse(duplicate.encode())

    def test_paths_unregistered_sources_and_unreviewed_requests_are_not_inputs(self):
        for key, bad in [("filename", "sample/archive.pkg.tar.zst"), ("repo", "local"),
                         ("installed", None), ("sha256", "g" * 64)]:
            request = self.request()
            request["target"]["native_review"]["transaction"][0][key] = bad
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.parse(request)
        request = self.request()
        request["target"]["native_review"]["requested"].append("unreviewed")
        with self.assertRaises(ValueError):
            self.parse(request)

    def test_signature_policy_does_not_allow_optional_or_untrusted_packages(self):
        default = ["PackageRequired", "PackageTrustedOnly", "DatabaseOptional", "DatabaseTrustedOnly"]
        self.assertEqual(arch_commit.siglevel(default), 1 | (1 << 10) | (1 << 11))
        for unsafe in ("PackageOptional", "PackageTrustAll", "DatabaseTrustAll"):
            with self.subTest(unsafe=unsafe), self.assertRaises(ValueError):
                arch_commit.siglevel([*default, unsafe])
