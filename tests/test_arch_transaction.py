import hashlib
import io
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest
import uuid


SOURCE = Path(__file__).resolve().parents[1]
ENABLE = os.environ.get("PENNIX_TEST_ARCH_TRANSACTION") == "1"


def run(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=30)


@unittest.skipUnless(ENABLE, "opt in on Arch with C compiler, libalpm, repo-add and GnuPG")
class ArchTransactionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        scratch = tempfile.TemporaryDirectory(prefix="pennix-arch-native-")
        cls.addClassCleanup(scratch.cleanup)
        cls.scratch = Path(scratch.name)
        cls.driver = cls.scratch / "test-only-arch-driver"
        run("cc", "-std=c17", "-Wall", "-Wextra", "-Werror", "-I", SOURCE / "native",
            SOURCE / "native/arch_transaction.c", SOURCE / "tests/arch_transaction_driver.c",
            "-lalpm", "-o", cls.driver)
        cls.keyring = cls.scratch / "keyring"
        cls.keyring.mkdir(mode=0o700)
        cls.addClassCleanup(lambda: run("gpgconf", "--homedir", cls.keyring, "--kill", "all"))
        run("gpg", "--homedir", cls.keyring, "--batch", "--pinentry-mode", "loopback",
            "--passphrase", "", "--quick-generate-key", "Pennix fixture <fixture@example.invalid>",
            "ed25519", "sign", "0")
        keys = run("gpg", "--homedir", cls.keyring, "--batch", "--with-colons", "--list-keys").stdout
        (cls.signer,) = (line.split(":")[9] for line in keys.splitlines() if line.startswith("fpr:"))

    def setUp(self):
        scratch = tempfile.TemporaryDirectory(dir=self.scratch)
        self.addCleanup(scratch.cleanup)
        self.work = Path(scratch.name)
        self.root = self.work / "root"
        self.db = self.work / "db"
        self.cache = self.work / "cache"
        self.repo = self.work / "repo"
        self.receipts = self.work / "receipts"
        self.hooks = self.work / "hooks"
        self.root.mkdir()
        (self.db / "sync").mkdir(parents=True)
        self.cache.mkdir(mode=0o700)
        self.repo.mkdir()
        self.receipts.mkdir(mode=0o700)
        self.hooks.mkdir(mode=0o700)
        self.log = self.work / "pacman.log"

    def signed_package(self, directory, name, version="1-1", *, contents=b"fixture\n", metadata=""):
        package = directory / f"{name}-{version}-any.pkg.tar.gz"
        pkginfo = (f"pkgname = {name}\npkgver = {version}\npkgdesc = fixture\n"
                   f"size = {len(contents)}\narch = any\n{metadata}").encode()
        with tarfile.open(package, "w:gz") as archive:
            for path, data in [(".PKGINFO", pkginfo), (f"usr/share/{name}", contents)]:
                member = tarfile.TarInfo(path)
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))
        run("gpg", "--homedir", self.keyring, "--batch", "--detach-sign", package)
        return package

    def stage(self, *packages):
        run("repo-add", "--include-sigs", self.repo / "extra.db.tar.gz", *packages)
        shutil.copyfile(self.repo / "extra.db.tar.gz", self.db / "sync/extra.db")
        for package in packages:
            self.stage_package(package)

    def stage_package(self, package):
        for file in (package, package.with_name(package.name + ".sig")):
            shutil.copyfile(file, self.cache / file.name)

    def transaction(self, *rows, target="sample", mode="normal", signer=None, receipt_id=None, digest="a" * 64):
        args = (self.driver, self.root, self.db, self.cache, self.keyring, self.log, mode, target,
                *(cell for row in rows for cell in (row if len(row) == 6 else (*row, ""))))
        result = subprocess.run(args, capture_output=True, text=True, timeout=30,
                                env={**os.environ, "PENNIX_TEST_LOCAL_SIGNER": self.signer if signer is None else signer,
                                     "PENNIX_TEST_RECEIPT_DIR": str(self.receipts),
                                     "PENNIX_TEST_RECEIPT_ID": receipt_id or uuid.uuid4().hex,
                                     "PENNIX_TEST_HOOK_DIR": str(self.hooks),
                                     "PENNIX_TEST_REQUEST_DIGEST": digest})
        if mode not in {"interrupted", "observe"}:
            self.assertFalse((self.db / "db.lck").exists())
        self.last_stderr = result.stderr
        return result.returncode

    def test_exact_install_and_system_upgrade(self):
        pkg = self.signed_package(self.repo, "sample")
        self.stage(pkg)
        row = ("extra", "sample", "", "1-1", hashlib.sha256(pkg.read_bytes()).hexdigest())
        self.assertEqual(self.transaction(row), 0, self.last_stderr)
        self.assertEqual((self.root / "usr/share/sample").read_bytes(), b"fixture\n")

        new = self.signed_package(self.repo, "sample", "2-1", contents=b"upgraded\n")
        self.stage(new)
        row = ("extra", "sample", "1-1", "2-1", hashlib.sha256(new.read_bytes()).hexdigest())
        self.assertEqual(self.transaction(row, target=""), 0, self.last_stderr)
        self.assertEqual((self.root / "usr/share/sample").read_bytes(), b"upgraded\n")
        self.assertTrue((self.db / "local/sample-2-1").is_dir())
        self.assertEqual(self.transaction(row, target=""), 1)
        old = ("extra", "sample", "2-1", "1-1", hashlib.sha256(pkg.read_bytes()).hexdigest())
        (self.repo / "extra.db.tar.gz").unlink()
        self.stage(pkg)
        self.assertEqual(self.transaction(old), 1)
        self.assertEqual((self.root / "usr/share/sample").read_bytes(), b"upgraded\n")

    def test_drift_and_unreviewed_native_dependency_do_not_commit(self):
        pkg = self.signed_package(self.repo, "sample", metadata="depend = runtime\n")
        dep = self.signed_package(self.repo, "runtime")
        self.stage(pkg, dep)
        pkg_sum = hashlib.sha256(pkg.read_bytes()).hexdigest()
        dep_row = ("extra", "runtime", "", "1-1", hashlib.sha256(dep.read_bytes()).hexdigest())
        exact = ("extra", "sample", "", "1-1", pkg_sum)
        for bad in [("core", "sample", "", "1-1", pkg_sum),
                    ("extra", "sample", "0-1", "1-1", pkg_sum),
                    ("extra", "sample", "", "2-1", pkg_sum),
                    ("extra", "sample", "", "1-1", "f" * 64),
                    dep_row]:
            with self.subTest(bad=bad):
                self.assertEqual(self.transaction(bad, dep_row), 1)
        self.assertEqual(self.transaction(exact), 1)
        self.assertFalse((self.root / "usr").exists())
        self.assertEqual(self.transaction(exact, dep_row), 0, self.last_stderr)
        self.assertTrue((self.root / "usr/share/runtime").is_file())

    def test_refuses_drift_in_baseline_outside_of_reviewed_additions(self):
        existing = self.signed_package(self.repo, "existing")
        sample = self.signed_package(self.repo, "sample")
        self.stage(existing, sample)
        existing_row = ("extra", "existing", "", "1-1", hashlib.sha256(existing.read_bytes()).hexdigest())
        self.assertEqual(self.transaction(existing_row, target="existing"), 0, self.last_stderr)
        row = ("extra", "sample", "", "1-1", hashlib.sha256(sample.read_bytes()).hexdigest())
        receipt = "3" * 32
        self.assertEqual(self.transaction(row, mode="stale-baseline", receipt_id=receipt), 1, self.last_stderr)
        self.assertEqual(self.transaction(row, mode="policy-drift", receipt_id=receipt), 1, self.last_stderr)
        self.assertFalse((self.receipts / receipt).exists())
        self.assertFalse((self.root / "usr/share/sample").exists())
        self.assertEqual(self.transaction(row, receipt_id=receipt), 0, self.last_stderr)

    def test_refuses_lock_contention_signed_byte_drift_and_relaxed_signatures(self):
        pkg = self.signed_package(self.repo, "sample")
        self.stage(pkg)
        row = ("extra", "sample", "", "1-1", hashlib.sha256(pkg.read_bytes()).hexdigest())
        self.assertEqual(self.transaction(row, mode="locked"), 1)
        self.assertEqual(self.transaction(row, mode="optional"), 1)

        alternate = self.work / "alternate"
        alternate.mkdir()
        different = self.signed_package(alternate, "sample", contents=b"not the reviewed bytes\n")
        shutil.copyfile(different, self.cache / pkg.name)
        shutil.copyfile(different.with_name(different.name + ".sig"), self.cache / (pkg.name + ".sig"))
        self.assertEqual(self.transaction(row), 1)

        shutil.copyfile(pkg, self.cache / pkg.name)
        self.assertEqual(self.transaction(row), 1)  # digest now matches but signature does not
        shutil.copyfile(pkg.with_name(pkg.name + ".sig"), self.cache / (pkg.name + ".sig"))
        (self.cache / pkg.name).unlink()
        self.assertEqual(self.transaction(row), 1)
        (self.cache / pkg.name).symlink_to(pkg)
        self.assertEqual(self.transaction(row), 1)
        self.assertFalse((self.root / "usr").exists())

    def test_unsatisfied_dep_refuses_in_native_prepare(self):
        pkg = self.signed_package(self.repo, "sample", metadata="depend = not-in-repo\n")
        self.stage(pkg)
        row = ("extra", "sample", "", "1-1", hashlib.sha256(pkg.read_bytes()).hexdigest())
        self.assertEqual(self.transaction(row), 1)
        self.assertFalse((self.root / "usr").exists())

    def test_refuses_replacement_question(self):
        pkg = self.signed_package(self.repo, "sample")
        self.stage(pkg)
        row = ("extra", "sample", "", "1-1", hashlib.sha256(pkg.read_bytes()).hexdigest())
        self.assertEqual(self.transaction(row), 0, self.last_stderr)
        replacement = self.signed_package(self.repo, "new-sample", metadata="replaces = sample\n")
        self.stage(replacement)
        row = ("extra", "new-sample", "", "1-1", hashlib.sha256(replacement.read_bytes()).hexdigest())
        self.assertEqual(self.transaction(row, target=""), 1)
        self.assertTrue((self.root / "usr/share/sample").is_file())
        self.assertFalse((self.root / "usr/share/new-sample").exists())

    def test_commit_failure_is_not_a_precommit_refusal(self):
        pkg = self.signed_package(self.repo, "sample")
        self.stage(pkg)
        row = ("extra", "sample", "", "1-1", hashlib.sha256(pkg.read_bytes()).hexdigest())
        existing = self.root / "usr/share/sample"
        existing.parent.mkdir(parents=True)
        existing.write_bytes(b"unowned\n")
        tx = "7" * 32
        self.assertEqual(self.transaction(row, receipt_id=tx), 2)
        self.assertEqual(self.transaction(row, mode="observe", receipt_id=tx), 2)
        self.assertEqual(self.transaction(row, receipt_id=tx), 2)
        self.assertEqual(existing.read_bytes(), b"unowned\n")

    def test_receipt_binds_digest_refuses_replay_and_reports_absence_as_unknown(self):
        pkg = self.signed_package(self.repo, "sample")
        self.stage(pkg)
        row = ("extra", "sample", "", "1-1", hashlib.sha256(pkg.read_bytes()).hexdigest())
        tx = "5" * 32
        self.assertEqual(self.transaction(row, mode="observe", receipt_id=tx), 2)
        self.assertEqual(self.transaction((*row[:4], "0" * 64), receipt_id=tx), 1)
        self.assertFalse((self.receipts / tx).exists())
        self.assertEqual(self.transaction(row, receipt_id=tx), 0, self.last_stderr)
        self.assertEqual(self.transaction(row, receipt_id=tx), 2)
        self.assertEqual(self.transaction(row, mode="observe", receipt_id=tx), 0)
        self.assertEqual(self.transaction(row, mode="observe", receipt_id=tx, digest="b" * 64), 2)
        alias = "6" * 32
        (self.receipts / alias).symlink_to(tx)
        self.assertEqual(self.transaction(row, mode="observe", receipt_id=alias), 2)
        self.assertEqual(self.transaction(row, receipt_id="../" + tx), 1)

    def test_package_versions_after_process_exit_do_not_prove_completed_transaction(self):
        pkg = self.signed_package(self.repo, "sample")
        self.stage(pkg)
        row = ("extra", "sample", "", "1-1", hashlib.sha256(pkg.read_bytes()).hexdigest())
        tx = "8" * 32
        self.assertEqual(self.transaction(row, mode="interrupted", receipt_id=tx), 91)
        self.assertTrue((self.db / "local/sample-1-1").is_dir())
        self.assertTrue((self.root / "usr/share/sample").is_file())
        self.assertTrue((self.db / "db.lck").exists())
        self.assertEqual(self.transaction(row, mode="observe", receipt_id=tx), 2)

    def test_failing_post_transaction_hook_does_not_issue_completion(self):
        pkg = self.signed_package(self.repo, "sample")
        self.stage(pkg)
        (self.hooks / "10-broken.hook").write_text(
            "[Trigger]\nOperation = Install\nType = Package\nTarget = sample\n"
            "[Action]\nWhen = PostTransaction\nExec = /nonexistent-pennix-fixture\n")
        row = ("extra", "sample", "", "1-1", hashlib.sha256(pkg.read_bytes()).hexdigest())
        tx = "9" * 32
        self.assertEqual(self.transaction(row, receipt_id=tx), 2, self.last_stderr)
        self.assertTrue((self.db / "local/sample-1-1").is_dir())
        self.assertEqual(self.transaction(row, mode="observe", receipt_id=tx), 2)

    def test_signed_local_package_only_adds_exact_reviewed_dependencies(self):
        dep = self.signed_package(self.repo, "runtime")
        self.stage(dep)
        pkg = self.signed_package(self.work, "local-app", metadata="depend = runtime\n")
        self.stage_package(pkg)
        row = ("", "local-app", "", "1-1", hashlib.sha256(pkg.read_bytes()).hexdigest(), pkg.name)
        required = ("extra", "runtime", "", "1-1", hashlib.sha256(dep.read_bytes()).hexdigest(), dep.name)
        self.assertEqual(self.transaction(row, target=""), 1)
        self.assertEqual(self.transaction(row, (*required[:-1], "wrong.pkg.tar.zst"), target=""), 1)
        for bad in [(*row[:1], "wrong-name", *row[2:]), (*row[:-1], "../" + pkg.name),
                    (*row[:-2], "0" * 64, pkg.name), (*row[:3], "2-1", *row[4:])]:
            with self.subTest(bad=bad):
                self.assertEqual(self.transaction(bad, required, target=""), 1)
        for signer in ("", "0" * len(self.signer)):
            self.assertEqual(self.transaction(row, required, target="", signer=signer), 1)
        self.assertFalse((self.root / "usr").exists())
        self.assertEqual(self.transaction(row, required, target=""), 0, self.last_stderr)
        self.assertTrue((self.root / "usr/share/local-app").is_file())
        self.assertTrue((self.root / "usr/share/runtime").is_file())
        self.assertEqual(self.transaction((*row[:2], "1-1", *row[3:]), target=""), 1)
        new = self.signed_package(self.work, "local-app", "2-1", contents=b"updated local package\n",
                                  metadata="depend = runtime\n")
        self.stage_package(new)
        row = ("", "local-app", "1-1", "2-1", hashlib.sha256(new.read_bytes()).hexdigest(), new.name)
        self.assertEqual(self.transaction(row, target=""), 0, self.last_stderr)
        self.assertEqual((self.root / "usr/share/local-app").read_bytes(), b"updated local package\n")

    def test_refuses_unsigned_local_file_and_same_name_official_package(self):
        dep = self.signed_package(self.repo, "runtime")
        self.stage(dep)
        pkg = self.signed_package(self.work, "local-app")
        self.stage_package(pkg)
        row = ("", "local-app", "", "1-1", hashlib.sha256(pkg.read_bytes()).hexdigest(), pkg.name)
        (self.cache / (pkg.name + ".sig")).unlink()
        self.assertEqual(self.transaction(row, target=""), 1)
        self.stage_package(pkg)
        official = self.signed_package(self.repo, "local-app", "2-1")
        self.stage(official)
        self.assertEqual(self.transaction(row, target=""), 1)
        self.assertFalse((self.root / "usr").exists())

    @unittest.skipUnless(os.environ.get("PENNIX_TEST_ARCH_BUILD") == "1",
                         "opt in with unprivileged bubblewrap and native makepkg")
    def test_install_artifact_built_by_unprivileged_makepkg(self):
        self.assertNotEqual(os.geteuid(), 0)
        dep = self.signed_package(self.repo, "runtime")
        self.stage(dep)
        build = self.work / "build"
        build.mkdir(mode=0o700)
        config = self.work / "pacman.conf"
        config.write_text("[options]\nArchitecture = auto\nDBPath = /tmp/pkgdb\n")
        (build / "PKGBUILD").write_text("""\
pkgname=local-app
pkgver=1
pkgrel=1
arch=(any)
license=(MIT)
options=(!debug !strip)
package() {
    awk '/^Uid:/ {exit ($3 == 0)}' /proc/self/status
    test ! -e /run/docker.sock
    test ! -e /var/lib/pacman/local
    test "$(readlink /proc/self/ns/net)" != "$PENNIX_TEST_HOST_NETNS"
    install -Dm644 /work/value "$pkgdir/usr/share/local-app/value"
}
""")
        (build / "value").write_bytes(b"built in isolation\n")
        run("/usr/bin/bwrap", "--unshare-all", "--unshare-user", "--disable-userns", "--new-session",
            "--die-with-parent", "--ro-bind", "/usr", "/usr", "--symlink", "usr/lib", "/lib",
            "--symlink", "usr/lib", "/lib64", "--symlink", "usr/bin", "/bin", "--proc", "/proc",
            "--dev", "/dev", "--tmpfs", "/tmp", "--ro-bind", "/etc/makepkg.conf", "/etc/makepkg.conf",
            "--ro-bind", config, "/etc/pacman.conf", "--bind", build, "/work", "--chdir", "/work",
            "--clearenv", "--setenv", "HOME", "/tmp", "--setenv", "PATH", "/usr/bin",
            "--setenv", "PENNIX_TEST_HOST_NETNS", os.readlink("/proc/self/ns/net"),
            "--", "/usr/bin/makepkg", "--cleanbuild", "--holdver", "--noconfirm",
            "--config", "/etc/makepkg.conf")
        (pkg,) = build.glob("local-app-1-1-any.pkg.tar.*")
        run("gpg", "--homedir", self.keyring, "--batch", "--detach-sign", pkg)
        self.stage_package(pkg)
        row = ("", "local-app", "", "1-1", hashlib.sha256(pkg.read_bytes()).hexdigest(), pkg.name)
        self.assertEqual(self.transaction(row, target=""), 0, self.last_stderr)
        self.assertEqual((self.root / "usr/share/local-app/value").read_bytes(), b"built in isolation\n")
        self.assertTrue((self.db / "local/local-app-1-1").is_dir())


if __name__ == "__main__":
    unittest.main()
