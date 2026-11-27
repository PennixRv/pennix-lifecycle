"""Verify read-only review, exact authorization and diagnosed update failures."""

import argparse
from contextlib import ExitStack
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
import urllib.error
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import arch_policy
import arch_recovery
import aur_build
import candidates
import component_lock
import pennix
import update_inventory
from test_release_owner import make_release


class ReviewedArchRecovery(unittest.TestCase):
    transaction = "4" * 32
    target = {
        "installed": {"tool": "1-1", "unchanged": "3-1"},
        "native_review": {
            "policy": {"pacman_conf_sha256": "a" * 64, "hooks": {}},
            "requested": ["library"],
            "transaction": [
                {"repo": "extra", "name": "tool", "installed": "1-1", "target": "2-1",
                 "filename": "tool-2-1-any.pkg.tar.zst", "sha256": "b" * 64},
                {"repo": "core", "name": "library", "installed": None, "target": "1-1",
                 "filename": "library-1-1-any.pkg.tar.zst", "sha256": "c" * 64},
            ],
        },
    }
    wanted = {"tool": "2-1", "library": "1-1", "unchanged": "3-1"}

    def test_receipt_and_live_set_required_with_no_write_command(self):
        digest = arch_recovery.receipt_digest(self.transaction, self.target)
        with patch.object(arch_recovery.os, "getuid", return_value=os.getuid() + 1):
            self.assertNotEqual(digest, arch_recovery.receipt_digest(self.transaction, self.target))
        self.assertNotEqual(digest, arch_recovery.receipt_digest("5" * 32, self.target))
        with tempfile.TemporaryDirectory() as directory:
            def mock_native(receipt_ok=True, checks_ok=True):
                def native(argv, *, timeout, noninteractive):
                    self.assertTrue(noninteractive)
                    if argv[0] == "/usr/bin/sudo":
                        self.assertEqual(argv, ["/usr/bin/sudo", "-n", "--", str(arch_recovery.EXECUTOR),
                                                "observe", self.transaction, digest])
                        return subprocess.CompletedProcess(argv, not receipt_ok, "", "")
                    if argv == ["/usr/bin/pacman-conf", "DBPath"]:
                        return subprocess.CompletedProcess(argv, 0, directory + "\n", "")
                    self.assertEqual(argv, ["/usr/bin/pacman", "-Dk"])
                    return subprocess.CompletedProcess(argv, not checks_ok, "", "")
                return native

            for receipt_ok, checks_ok, versions, expected in [
                (True, True, [self.wanted, self.wanted], True),
                (False, True, [self.wanted, self.wanted], False),
                (True, False, [self.wanted, self.wanted], False),
                (True, True, [self.wanted, {**self.wanted, "unreviewed": "1-1"}], False),
                (True, True, [self.target["installed"]], False),
            ]:
                with self.subTest(receipt_ok=receipt_ok, checks_ok=checks_ok, versions=versions):
                    with patch.object(arch_recovery, "_trusted_observer", return_value=True), \
                            patch.object(update_inventory, "run", side_effect=mock_native(receipt_ok, checks_ok)), \
                            patch.object(update_inventory, "installed_arch", side_effect=versions) as installed:
                        observed = arch_recovery.observe(self.transaction, self.target)
                        self.assertEqual(observed is not None, expected)
                        if not receipt_ok:
                            installed.assert_not_called()

            (Path(directory) / "db.lck").symlink_to("missing")
            with patch.object(arch_recovery, "_trusted_observer", return_value=True), \
                    patch.object(update_inventory, "run", side_effect=mock_native()), \
                    patch.object(update_inventory, "installed_arch") as installed:
                self.assertIsNone(arch_recovery.observe(self.transaction, self.target))
                installed.assert_not_called()

    def test_untrusted_helper_never_runs(self):
        with patch.object(arch_recovery, "_trusted_observer", return_value=False), \
                patch.object(update_inventory, "run") as run:
            self.assertIsNone(arch_recovery.observe(self.transaction, self.target))
            run.assert_not_called()

    def test_review_uses_current_native_db_and_protected_cache_without_commit(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "sync").mkdir()
            cache = root / "cache"
            cache.mkdir()
            text = f"[options]\nCacheDir = {cache}\n"
            target = {**self.target, "upgrades": [], "real_sync_sha256": "sync-baseline"}
            rows = target["native_review"]["transaction"]
            for row in rows:
                for name in (row["filename"], row["filename"] + ".sig"):
                    (cache / name).write_text("fixture")
            with patch.object(arch_recovery, "executor_identity", return_value={"identity": "fixed"}), \
                    patch.object(arch_policy, "trusted_directory", return_value=True), \
                    patch.object(arch_policy, "snapshot", return_value=target["native_review"]["policy"]), \
                    patch.object(arch_recovery, "_db_lock", return_value=root / "db.lck"), \
                    patch.object(arch_recovery, "_trusted_file", side_effect=lambda path: path.stat().st_size > 0), \
                    patch.object(arch_recovery, "_file_sha256",
                                 side_effect=lambda path: next(row["sha256"] for row in rows if row["filename"] == path.name)), \
                    patch.object(update_inventory, "sync_digest", return_value="sync-baseline") as sync, \
                    patch.object(update_inventory, "_arch_transaction", return_value=rows) as transaction, \
                    patch.object(update_inventory, "run", return_value=subprocess.CompletedProcess([], 0, text, "")) as run:
                self.assertEqual(arch_recovery.review_executor(target), {"identity": "fixed"})
                run.assert_called_once_with(["/usr/bin/pacman-conf", "--verbose"], timeout=15, noninteractive=True)
                transaction.assert_called_once_with(root, target["installed"], [], ["library"], exact=True)
                sync.return_value = "drift"
                with self.assertRaisesRegex(arch_recovery.ArchCommitError, "实库状态已变化"):
                    arch_recovery.review_executor(target)
                self.assertEqual(transaction.call_count, 1)
                sync.return_value = "sync-baseline"
                (cache / (rows[0]["filename"] + ".sig")).unlink()
                with self.assertRaisesRegex(arch_recovery.ArchCommitError, "缓存缺少"):
                    arch_recovery.review_executor(target)

    def test_commit_requires_fixed_identity_and_independent_receipt(self):
        identity = {"arch-commit": "e" * 64}
        digest = arch_recovery.receipt_digest(self.transaction, self.target)
        proof = {"state": "native-commit-verified", "receipt_sha256": digest}
        with patch.object(arch_recovery, "executor_identity", return_value={}) as current, \
                patch.object(arch_recovery.subprocess, "run") as run, \
                patch.object(arch_recovery, "observe", return_value=proof) as observe:
            with self.assertRaises(arch_recovery.ArchCommitError) as failure:
                arch_recovery.commit(self.transaction, self.target, identity)
            self.assertEqual(failure.exception.effect, "none")
            run.assert_not_called()
            current.return_value = identity
            for status, stdout, effect in [
                (1, b"", "unknown"),
                (1, b"pennix-arch-refused-v1 " + b"0" * 64 + b"\n", "unknown"),
                (1, f"pennix-arch-refused-v1 {digest}\n".encode(), "none"),
                (2, b"", "unknown"),
            ]:
                with self.subTest(status=status, effect=effect):
                    run.return_value = subprocess.CompletedProcess([], status, stdout, b"")
                    with self.assertRaises(arch_recovery.ArchCommitError) as failure:
                        arch_recovery.commit(self.transaction, self.target, identity)
                    self.assertEqual(failure.exception.effect, effect)
            observe.assert_not_called()
            run.return_value = subprocess.CompletedProcess([], 0, b"", b"")
            self.assertEqual(arch_recovery.commit(self.transaction, self.target, identity), proof)
            self.assertEqual(run.call_args.args[0], ["/usr/bin/sudo", "-n", "--", str(arch_recovery.COMMITTER),
                                                     "commit", self.transaction, digest])
            self.assertEqual(run.call_args.kwargs["input"], arch_recovery.request_bytes(self.transaction, self.target))
            observe.assert_called_once_with(self.transaction, self.target)
            observe.return_value = None
            with self.assertRaises(arch_recovery.ArchCommitError) as failure:
                arch_recovery.commit(self.transaction, self.target, identity)
            self.assertEqual(failure.exception.effect, "unknown")

    def test_cli_only_records_started_exact_native_step(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"CODEX_HOME": f"{temp}/codex"}), \
                patch.object(pennix.owned_files, "state_root", return_value=Path(temp)), \
                patch.object(pennix, "_load", return_value=(None, Path(temp) / "config", {"components": {}})), \
                patch.object(pennix.config_model, "config_value", return_value=f"{temp}/codex"):
            conf = Path(temp) / "config"
            conf.write_text("config")
            lock = Path(temp) / "lock"
            steps = [
                {"id": "update-review", "component": "update-review", "action": "review",
                 "target": {"mode": "noninteractive-v1", "system": self.target}},
                {"id": "system-upgrade", "component": "system", "action": "upgrade", "target": self.target},
            ]
            journal = pennix.apply_journal
            active = journal.start("update-all", None, journal.digest(conf), None, steps)
            active = journal.verified(active, "update-review", {"inputs": "bound"})
            args = argparse.Namespace(resolve="completed", transaction=active["transaction"],
                                      step="system-upgrade", component_lock_path=lock, json=True)
            with patch.object(arch_recovery, "observe", return_value=None) as observe, \
                    patch.object(pennix, "_emit"):
                with self.assertRaisesRegex(pennix.CliError, "不重跑包事务"):
                    pennix.cmd_recover(args)
                observe.assert_not_called()
                active = journal.unknown(journal.started(active, args.step), args.step)
                with self.assertRaisesRegex(pennix.CliError, "不重跑包事务"):
                    pennix.cmd_recover(args)
                self.assertEqual(active, journal.read())
                observe.assert_called_once_with(active["transaction"], self.target)
                observe.return_value = {"state": "native-commit-verified"}
                self.assertEqual(pennix.cmd_recover(args), 0)
                self.assertIsNone(journal.read())
                archived = pennix.owned_files.read_json(Path(temp) / "transactions" / f"{args.transaction}.json")
                self.assertEqual(journal.step(archived, args.step)["observed"], observe.return_value)

    def test_reviewed_component_recovery_never_calls_legacy_package_install(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"CODEX_HOME": f"{temp}/codex"}), \
                patch.object(pennix.owned_files, "state_root", return_value=Path(temp)), \
                patch.object(pennix.config_model, "config_value", return_value=f"{temp}/codex"):
            conf, lock = Path(temp) / "config", Path(temp) / "lock"
            conf.write_text("config")
            steps = [
                {"id": "update-review", "component": "update-review", "action": "review",
                 "target": {"mode": "noninteractive-v1", "system": self.target}},
                {"id": "system-upgrade", "component": "system", "action": "upgrade", "target": self.target},
                {"id": "tool:install", "component": "tool", "action": "install", "target": {"version": "1-1"}},
            ]
            journal = pennix.apply_journal
            active = journal.start("update-all", None, journal.digest(conf), None, steps)
            active = journal.verified(active, "system-upgrade", {"state": "native-commit-verified"})
            active = journal.unknown(journal.started(active, "tool:install"), "tool:install")
            args = argparse.Namespace(resolve="completed", transaction=active["transaction"],
                                      step="tool:install", component_lock_path=lock, json=True)
            for source in ("official", "aur"):
                with self.subTest(source=source), \
                        patch.object(pennix, "_load", return_value=(
                            None, conf, {"components": {"tool": {"delivery": "package", "package": {"source": source}}}})), \
                        patch.object(pennix.owner_engine, "package_absent", return_value=True) as absent, \
                        patch.object(pennix, "_component_status", return_value={"state": "missing"}), \
                        patch.object(pennix, "_run_component") as install:
                    with self.assertRaisesRegex(pennix.CliError, "保留 unknown"):
                        pennix.cmd_recover(args)
                    self.assertEqual(journal.read(), active)
                    absent.assert_not_called()
                    install.assert_not_called()


class InventoryFailures(unittest.TestCase):
    component = {"id": "markitdown", "delivery": "package", "lock_source": "pypi:markitdown",
                 "package": {"source": "uv", "name": "markitdown"}}
    locked = {"source": "pypi:markitdown", "version": "0.1.9"}
    catalog = {"components": {"markitdown": component}}
    system = {"state": "current", "available": {}, "upgrades": [], "installed": {}}

    def report(self, observed, error=None):
        with patch.object(update_inventory, "arch", return_value=self.system), \
                patch.object(update_inventory, "available", return_value=self.locked, side_effect=error):
            return update_inventory.inspect(self.catalog, {"markitdown"}, {"markitdown": self.locked},
                                            {"components": {"markitdown": observed}, "connections": {}},
                                            system_upgrade=True)

    def test_source_failure_and_bad_payload_are_distinct_and_redacted(self):
        observed = {"state": "match", "ownership": "lifecycle", "action": "keep"}
        for error, code in [(urllib.error.URLError("private-origin"), "source-query-failed"),
                            (candidates.CandidateError("private-payload"), "candidate-invalid")]:
            with self.subTest(code=code):
                report = self.report(observed, error)
                item = report["components"]["markitdown"]
                self.assertEqual((item["state"], item["reason"]), ("unknown", code))
                self.assertEqual(item["installed"]["state"], "match")
                self.assertNotIn("private-", json.dumps(report))
                args = argparse.Namespace(json=True, review_system_changes=False)
                with patch.object(pennix.apply_journal, "read", return_value=None), \
                        patch.object(pennix, "_updates", return_value=(report, {}, self.catalog)), \
                        patch.object(pennix, "_emit"), patch("builtins.input") as confirm, \
                        patch.object(pennix, "cmd_apply") as apply:
                    with self.assertRaisesRegex(pennix.CliError, r"markitdown.*pennix updates") as failure:
                        pennix.cmd_update_all(args)
                    self.assertNotIn("reconcile", str(failure.exception))
                    confirm.assert_not_called()
                    apply.assert_not_called()
        self.assertEqual(self.report(observed)["components"]["markitdown"]["state"], "current")

    def test_local_drift_and_unmanaged_install_do_not_become_current(self):
        for observed, state, reason in [
            ({"state": "outdated", "ownership": "lifecycle"}, "unknown", "installed-identity-mismatch"),
            ({"state": "match", "ownership": "external"}, "unmanaged", "ownership-unverified"),
        ]:
            with self.subTest(reason=reason):
                item = self.report(observed)["components"]["markitdown"]
                self.assertEqual((item["state"], item["reason"]), (state, reason))
                self.assertIn("pennix plan", item["next"])
                self.assertNotIn("reconcile", item["next"])

        report = self.report({"state": "blocked", "ownership": "unknown", "detail": "private-native-message"})
        self.assertNotIn("private-native-message", json.dumps(report))

class ArchInventoryReview(unittest.TestCase):
    def native_reply(self, stdout='', stderr='', code=0):
        return subprocess.CompletedProcess([], code, stdout, stderr)

    def test_exact_print_keeps_full_dependency_set_and_refuses_ambiguous_candidates(self):
        digest = 'a' * 64
        candidate = f'extra\ttool\t2-1\ttool-2-1-x86_64.pkg.tar.zst\t{digest}\tNone\n'
        dependency = f'core\tlibx\t2-1\tlibx-2-1-x86_64.pkg.tar.zst\t{digest}\tNone\n'
        with patch.object(update_inventory, 'run', return_value=self.native_reply(candidate + dependency)) as run:
            rows = update_inventory._arch_transaction('/fixture', {'libx': '1-1'}, [], ['tool'], exact=True)
        self.assertEqual([(row['name'], row['installed'], row['sha256']) for row in rows],
                         [('libx', '1-1', digest), ('tool', None, digest)])
        self.assertIn('%r\t%n\t%v\t%f\t%h\t%R', run.call_args.args[0])
        self.assertTrue(run.call_args.kwargs['noninteractive'])
        for bad in [candidate.replace(digest, ''), candidate.replace('tool-2', '../tool-2'),
                    candidate.replace('None', 'older-tool'), candidate + candidate]:
            with self.subTest(row=bad), patch.object(update_inventory, 'run', return_value=self.native_reply(bad)):
                with self.assertRaises(update_inventory.UpdateError):
                    update_inventory._arch_transaction('/fixture', {}, [], ['tool'], exact=True)

    def test_no_candidates_still_resolves_native_transaction_but_never_inspects_hook_policy(self):
        def run(command, **kwargs):
            if command[0].endswith('pacman-conf'):
                return self.native_reply(f'{directory}\n')
            if '-Q' in command:
                return self.native_reply('base 1-1\n')
            if '-Sl' in command:
                return self.native_reply('core base 1-1 [installed]\n')
            if '-Qu' in command:
                return self.native_reply(code=1)
            return self.native_reply()

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(update_inventory, 'run', side_effect=run) as calls, \
                patch.object(update_inventory, '_arch_policy') as policy:
            result = update_inventory.arch(review_full=True)
        self.assertEqual(result['state'], 'current')
        self.assertNotIn('native_review', result)
        self.assertTrue(any('-Sup' in call.args[0] for call in calls.call_args_list))
        policy.assert_not_called()

    def test_plan_does_not_accept_policy_changed_during_native_resolution(self):
        def run(command, **kwargs):
            if command[0].endswith('pacman-conf'):
                return self.native_reply(f'{directory}\n')
            if '-Q' in command:
                return self.native_reply('base 1-1\n')
            if '-Sl' in command:
                return self.native_reply('core base 2-1 [installed: 1-1]\n')
            if '-Qu' in command:
                return self.native_reply('base 1-1 -> 2-1\n')
            if '-Sup' in command:
                return self.native_reply(f'core\tbase\t2-1\tbase-2-1-any.pkg.tar.zst\t{"a" * 64}\t\n')
            return self.native_reply()

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(update_inventory, 'run', side_effect=run), \
                patch.object(update_inventory, '_arch_policy', side_effect=[
                    {'hooks': {'base.hook': {'masked': True}}}, {'hooks': {}}]):
            result = update_inventory.arch(review_full=True)
        self.assertEqual(result['state'], 'blocked')
        self.assertIn('Hook', result['detail'])
        self.assertNotIn('native_review', result)

    def test_hook_priority_masking_and_unsafe_file_refusal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            system, local = root / 'defaults', root / 'overrides'
            system.mkdir()
            local.mkdir()
            for name in ('20-base.hook', '50-overridden.hook', '80-masked.hook'):
                (system / name).write_text('default hook')
            replacement = local / '50-overridden.hook'
            replacement.write_text('local hook')
            (local / '80-masked.hook').symlink_to('/dev/null')
            real_stat, real_fstat = Path.lstat, os.fstat

            def stat_like_system(result, mode=None):
                return SimpleNamespace(st_uid=0, st_mode=result.st_mode if mode is None else mode,
                                       st_size=result.st_size, st_dev=result.st_dev, st_ino=result.st_ino)

            def fixture_lstat(path):
                return stat_like_system(real_stat(path), stat.S_IFDIR | 0o755 if path in root.parents else None)

            with patch.object(Path, 'lstat', fixture_lstat), \
                    patch.object(os, 'fstat', lambda fd: stat_like_system(real_fstat(fd))):
                hooks = arch_policy.effective_hooks([system, local])
                self.assertEqual(hooks['80-masked.hook'], {'masked': True})
                self.assertNotEqual(hooks['50-overridden.hook'], hooks['20-base.hook'])
                self.assertEqual(hooks['50-overridden.hook'],
                                 arch_policy.effective_hooks([local])['50-overridden.hook'])
                replacement.chmod(0o666)
                with self.assertRaises(arch_policy.PolicyError):
                    arch_policy.effective_hooks([system, local])

    def test_recheck_rejects_exact_rows_or_policy_changes(self):
        native = {'requested': ['tool'], 'transaction': [], 'policy': {'hooks': {}}}
        before = {'state': 'current', 'installed': {}, 'upgrades': [], 'native_review': native,
                  'real_sync_sha256': 'before'}
        args = SimpleNamespace(update_review={'system': before, 'available': {}})
        with patch.object(update_inventory, 'arch', return_value={
                **before, 'native_review': {**native, 'policy': {'hooks': {'fixture.hook': {}}}}}) as arch:
            with self.assertRaisesRegex(pennix.CliError, 'Arch 包工件或原生配置改变'):
                pennix._verify_update_candidates(args, {})
            arch.assert_called_once_with(include_packages=['tool'], review_full=True)
            arch.return_value = {**before, 'real_sync_sha256': 'changed'}
            with self.assertRaisesRegex(pennix.CliError, 'Arch 包工件或原生配置改变'):
                pennix._verify_update_candidates(args, {})


class NoninteractiveReviews(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        env = patch.dict(os.environ, {"HOME": directory.name, "XDG_STATE_HOME": str(self.root / "state"),
                                      "XDG_DATA_HOME": str(self.root / "data")})
        env.start()
        self.addCleanup(env.stop)

    def test_aur_preview_never_builds_and_drift_requires_new_confirmation(self):
        config = self.root / "workflow.config"
        config.write_text("selected")
        component = {"package": {"source": "aur", "name": "agent-of-empires-bin"}}
        form = {"package": "agent-of-empires-bin", "recipe": "fixture", "sources": {}}

        def load(args):
            args.config_path = config
            return None, config, {"components": {"aoe": component}}

        with patch.object(pennix, "_load", side_effect=load), \
                patch.object(pennix.config_model, "selected_components", return_value={"aoe"}), \
                patch.object(pennix, "_review_target", return_value="7" * 64), \
                patch.object(aur_build, "inspect", return_value=(self.root, form)), \
                patch.object(aur_build, "build", return_value={"sha256": "a" * 64}) as build:
            command = ["aur-prepare", "aoe", "--json"]
            with patch.object(pennix.owned_files, "locked", side_effect=AssertionError("preview locked")), \
                    patch("sys.stdout", new_callable=io.StringIO) as output:
                self.assertEqual(pennix.main([*command, "--plan"]), 0)
            review = json.loads(output.getvalue())
            self.assertFalse(review["installs_packages"])
            build.assert_not_called()
            confirm = [*command, "--confirm", review["review_sha256"]]
            form["recipe"] = "drifted"
            with patch("sys.stderr", new_callable=io.StringIO) as error:
                self.assertEqual(pennix.main(confirm), 2)
                self.assertIn("没有执行配方", error.getvalue())
            build.assert_not_called()
            form["recipe"] = "fixture"
            with patch("sys.stdout", new_callable=io.StringIO) as output:
                self.assertEqual(pennix.main(confirm), 0)
            self.assertEqual(json.loads(output.getvalue())["artifact"]["sha256"], "a" * 64)
            build.assert_called_once_with(self.root, form)
            self.assertIsNone(pennix.apply_journal.read())

    def test_aur_source_review_only_accepts_distinct_pinned_inputs(self):
        digest = hashlib.sha256(b"input").hexdigest()
        (self.root / "fixture.patch").write_bytes(b"input")
        metadata = (f"source = fixture.patch\nsha256sums = {digest}\n"
                    f"source_x86_64 = a.tgz::https://example.invalid/a.tgz\nsha256sums_x86_64 = {digest}\n")
        self.assertEqual(aur_build.inspect_sources(metadata, "x86_64", self.root),
                         {"fixture.patch": {"sha256": digest},
                          "a.tgz": {"url": "https://example.invalid/a.tgz", "sha256": digest}})
        for invalid in (metadata.replace(digest, "SKIP"), metadata.replace("https://", "git+https://"),
                        metadata.replace("a.tgz::", "fixture.patch::"), metadata.replace("a.tgz::", "../a.tgz::")):
            with self.subTest(invalid=invalid), self.assertRaises(candidates.CandidateError):
                aur_build.inspect_sources(invalid, "x86_64", self.root)

    def test_paseo_preview_and_execution_precondition_use_different_evidence(self):
        home = self.root / 'paseo'
        package = {'source': 'github:paseo', 'version': '2', 'sha256': 'a' * 64}
        key = 'paseo-cli'
        catalog = {'components': {key: {'package': {'source': 'npm'}}}}
        args = SimpleNamespace(update_review={'system': {'upgrades': []}},
                               update_targets={key: package}, component_lock={'components': {}})
        stopped = {'home': str(home), 'pid': None, 'localDaemon': 'stopped', 'connectedDaemon': 'not_probed'}
        plan = {'components': {key: {'selected': True, 'action': 'upgrade', 'candidate_target': package},
                               'paseo-autostart': {'selected': False, 'action': 'keep', 'unit_state': 'absent',
                                                  'run': False}},
                'side_effects': {'path': {'action': 'keep'}}}
        service = plan['components']['paseo-autostart']
        with patch.object(pennix.paseo_service, 'unit_path', return_value=self.root / 'missing.service'), \
                patch.object(pennix.paseo_service.os, 'kill'), \
                patch.object(pennix.paseo_service.subprocess, 'run') as probe:
            for receipt, permitted in [
                    (stopped, True), ({**stopped, 'home': '/another-instance'}, False),
                    ({'home': str(home), 'pid': None}, False),
                    ({**stopped, 'connectedDaemon': 'reachable'}, False),
                    ({**stopped, 'pid': 100, 'localDaemon': 'running', 'connectedDaemon': 'reachable',
                      'listen': '127.0.0.1:6767'}, False)]:
                with self.subTest(receipt=receipt):
                    probe.return_value = subprocess.CompletedProcess([], 0, json.dumps(receipt), '')
                    service['runtime'] = pennix.paseo_service.runtime(self.root / 'paseo-bin', home, 'off', False)
                    steps = pennix._reviewed_update_steps(plan, args, catalog)
                    self.assertEqual([step['id'] for step in steps], ['update-review', 'paseo-cli:upgrade'])
                    self.assertEqual(pennix._paseo_cli_execution_ready(service), permitted)
                    self.assertEqual(probe.call_args.args[0][1:3], ['daemon', 'status'])

    def test_recovery_view_keeps_sensitive_digest_private(self):
        active = {'transaction': 'fixture', 'steps': [{'id': 'update-review',
                   'target': {'catalog_digest': 'visible', 'secrets_digest': 'private'}}]}
        with patch.object(pennix.apply_journal, 'read', return_value=active), \
                patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(pennix.main(['recover', '--json']), 0)
        self.assertNotIn('private', output.getvalue())
        self.assertIn('visible', output.getvalue())
        self.assertIn('secrets_digest', active['steps'][0]['target'])

    def test_explicit_review_cannot_issue_or_replay_while_a_journal_exists(self):
        with patch.object(pennix.apply_journal, 'read', return_value={'command': 'update-all'}), \
                patch.object(pennix, '_updates') as query, patch.object(pennix, 'cmd_apply') as apply:
            for mode in (['--plan'], ['--confirm', 'a' * 64]):
                args = pennix.parser().parse_args(['update', '--all', *mode])
                with self.assertRaisesRegex(pennix.CliError, 'recover'):
                    pennix.cmd_update(args)
            query.assert_not_called()
            apply.assert_not_called()

    def test_reviewed_journal_never_falls_into_legacy_apply_path(self):
        active = {'command': 'update-all', 'steps': [
            {'id': 'update-review', 'target': {'mode': 'noninteractive-v1', 'targets': {}}},
            {'id': 'system-upgrade', 'status': 'pending'},
        ]}
        with patch.object(pennix.apply_journal, 'read', return_value=active), \
                patch.object(pennix, 'cmd_apply') as apply:
            for flags in ([], ['--review-system-changes']):
                with self.subTest(flags=flags):
                    args = pennix.parser().parse_args(['update', '--all', *flags])
                    with self.assertRaisesRegex(pennix.CliError, 'recover.*不通过旧入口重放'):
                        pennix.cmd_update(args)
            apply.assert_not_called()

    def test_tool_preview_does_not_lock_or_write_and_exact_confirmation_switches(self):
        old = make_release(self.root / 'old', '1.0.0')
        new = make_release(self.root / 'new', '1.0.1')
        pennix.release_owner.install(old)
        _, entry = pennix.release_owner._locations()
        target = {'version': '1.0.1', 'digest': 'sha256:' + 'a' * 64}
        with patch.object(pennix, 'ROOT', entry.resolve().parent), \
                patch.object(pennix.release_owner, 'latest', return_value=target), \
                patch.object(pennix, '_review_target', return_value='a' * 64) as target_id, \
                patch.object(pennix.release_owner, 'update', side_effect=lambda _: pennix.release_owner._install(new)) as update, \
                patch.object(pennix.owner_engine.host, 'detect_host', return_value={'supported': True}), \
                patch('builtins.input', side_effect=AssertionError('unexpected prompt')):
            with patch.object(pennix.owned_files, 'locked', side_effect=AssertionError('preview locked')), \
                    patch('sys.stdout', new_callable=io.StringIO) as output:
                self.assertEqual(pennix.main(['tool', 'update', '--plan', '--json']), 0)
            digest = json.loads(output.getvalue())['review_sha256']
            self.assertEqual(entry.resolve().parent.name, '1.0.0')
            update.assert_not_called()
            target_id.return_value = 'b' * 64
            with patch('sys.stdout', new_callable=io.StringIO), patch('sys.stderr', new_callable=io.StringIO):
                self.assertNotEqual(pennix.main(['tool', 'update', '--confirm', digest]), 0)
            update.assert_not_called()
            target_id.return_value = 'a' * 64
            with patch('sys.stdout', new_callable=io.StringIO):
                self.assertEqual(pennix.main(['tool', 'update', '--confirm', digest]), 0)
            update.assert_called_once_with(target)
            self.assertEqual(entry.resolve().parent.name, '1.0.1')

    def test_all_preview_and_confirmation_only_write_frozen_user_steps(self):
        self._check_full_review("trellis-cli")

    def test_running_paseo_does_not_block_preview_but_blocks_execution(self):
        self._check_full_review("paseo-cli")

    def test_official_confirm_recovers_interrupted_lock_checkpoint_without_replay(self):
        self._check_arch_recovery(with_aur=False)

    def test_aur_confirm_uses_fixed_transaction_and_recovers_exact_package_lock(self):
        self._check_arch_recovery(with_aur=True)

    def _check_arch_recovery(self, *, with_aur):
        journal = pennix.apply_journal
        conf, lock, codex = (self.root / name for name in ("workflow.config", "components.lock.json", "codex"))
        conf.write_text("config")
        component = {"id": "tmux", "delivery": "package", "lock_source": "pacman:tmux",
                     "package": {"source": "official", "name": "tmux"}}
        new_component = {**component, "id": "yazi", "lock_source": "pacman:yazi",
                         "package": {"source": "official", "name": "yazi"}}
        catalog = {"components": {"tmux": component, "yazi": new_component}, "configuration_targets": {}}
        sources = {key: spec["lock_source"] for key, spec in catalog["components"].items()}
        previous = {"source": component["lock_source"], "version": "1-1"}
        expected = {**previous, "version": "2-1"}
        new = {"source": new_component["lock_source"], "version": "3-1"}
        component_lock.write(lock, {"schema": 1, "components": {"tmux": previous}}, sources)
        target = {"state": "current", "installed": {"tmux": "1-1"},
                  "upgrades": [{"name": "tmux", "installed": "1-1", "available": "2-1"}],
                  "native_review": {"requested": ["yazi"], "policy": {}, "transaction": [
                      {"repo": "extra", "name": "tmux", "installed": "1-1", "target": "2-1",
                       "filename": "tmux-2-1-any.pkg.tar.zst", "sha256": "a" * 64},
                      {"repo": "extra", "name": "yazi", "installed": None, "target": "3-1",
                       "filename": "yazi-3-1-any.pkg.tar.zst", "sha256": "e" * 64}]}}
        report = {"system": target, "system_upgrade_enabled": True,
                  "components": {"tmux": {"state": "update", "available": expected},
                                 "yazi": {"state": "update", "available": new}}}
        installed = {"tmux": previous["version"]}
        expected_locks = {"tmux": expected, "yazi": new}
        aur_name = "agent-of-empires-bin"
        aur = {"version": "4-1", "ref": "a" * 40, "sha256": "9" * 64, "signer": "B" * 40,
               "filename": aur_name + "-4-1-any.pkg.tar.zst", "signature_sha256": "0" * 64}
        if with_aur:
            aur_source = {"source": "aur:" + aur_name, "version": aur["version"], "ref": aur["ref"]}
            aur_component = {"id": "aoe", "delivery": "package", "lock_source": aur_source["source"],
                             "package": {"name": aur_name, "source": "aur"}}
            catalog["components"]["aoe"] = aur_component
            sources["aoe"] = aur_source["source"]
            expected_locks["aoe"] = {**aur_source, "sha256": aur["sha256"]}
            report["components"]["aoe"] = {"state": "update", "available": aur_source}
        identity = {"arch-commit": "b" * 64}
        journal_start = journal.digest(lock)

        def load(args):
            args.config_path, args.config_snapshot = conf, conf.read_bytes()
            args.codex_home, args.kconf = codex, SimpleNamespace(syms={})
            args.component_lock_path, args.component_lock_snapshot = lock, lock.read_bytes()
            args.component_lock, args.component_lock_sources = component_lock.read(lock, sources), sources
            return args.kconf, conf, catalog

        def plan(args):
            load(args)
            current = args.component_lock["components"]["tmux"]["version"] == expected["version"]
            return {"config": str(conf), "connections": {}, "components": {
                "tmux": {"selected": True, "action": "keep" if current else "system-refresh"},
                "yazi": {"selected": True,
                         "action": "keep" if "yazi" in args.component_lock["components"] else "install"},
                **({"aoe": {"selected": True, "action": "keep" if "aoe" in args.component_lock["components"]
                            else "install"}} if with_aur else {}),
                "paseo-autostart": {"selected": False, "action": "keep", "unit_state": "absent",
                                    "mode": "off", "run": False, "runtime": {"running": False}}},
                "side_effects": {"system_upgrade": True, "path": {"action": "keep"}}}

        def commit(transaction, submitted, executor):
            expected_target = pennix._with_aur_artifacts(target, {aur_name: aur}) if with_aur else target
            self.assertEqual(submitted, expected_target)
            self.assertEqual(executor, identity)
            self.assertEqual(journal.step(journal.read(), "system-upgrade")["status"], "started")
            installed.update(tmux=expected["version"], yazi=new["version"])
            if with_aur:
                installed["aoe"] = aur["version"]
            return {"state": "native-commit-verified", "receipt_sha256": arch_recovery.receipt_digest(transaction, submitted)}

        with ExitStack() as stack:
            for obj, name, effect in [(pennix, "_load", load), (pennix, "_plan", plan),
                    (pennix, "_updates", lambda args: (report, plan(args), catalog)),
                    (pennix.config_model, "config_value", lambda _, key: str(codex if key == "PENNIX_CODEX_HOME"
                                                                           else self.root / "unused")),
                    (pennix.owner_engine.host, "detect_host", lambda: {"supported": True}),
                    (pennix.owner_engine, "package_identity", lambda spec: {"version": installed[spec["id"]]})]:
                stack.enter_context(patch.object(obj, name, side_effect=effect))
            stack.enter_context(patch.object(pennix, "_review_target", return_value="c" * 64))
            stack.enter_context(patch.object(pennix, "_verify_update_candidates"))
            stack.enter_context(patch.object(candidates, "resolve"))
            stack.enter_context(patch.object(arch_recovery, "aur_candidate", return_value=aur))
            stack.enter_context(patch("builtins.input", side_effect=AssertionError("unexpected prompt")))
            legacy = stack.enter_context(patch.object(pennix.subprocess, "run", side_effect=AssertionError("unexpected subprocess")))
            stack.enter_context(patch.dict(os.environ, {"CODEX_HOME": str(codex)}))
            pennix.owner_engine._write_package_receipt("tmux", component, installed["tmux"])
            review = stack.enter_context(patch.object(arch_recovery, "review_executor", return_value=identity))
            writer = stack.enter_context(patch.object(arch_recovery, "commit", side_effect=commit))
            observer = stack.enter_context(patch.object(arch_recovery, "observe", return_value=None))
            command = ["update", "--all", "--json"]
            target["native_review"]["requested"].append("unselected")
            with patch("sys.stderr", new_callable=io.StringIO) as error:
                self.assertEqual(pennix.main([*command, "--plan"]), 2)
            self.assertIn("与选定组件不一致", error.getvalue())
            review.assert_not_called()
            target["native_review"]["requested"].remove("unselected")
            with patch("sys.stdout", new_callable=io.StringIO) as output:
                self.assertEqual(pennix.main([*command, "--plan"]), 0)
            preview = json.loads(output.getvalue())
            self.assertEqual([step["action"] for step in preview["steps"]], ["upgrade", "record"])
            if with_aur:
                self.assertEqual(preview["steps"][0]["candidate"]["native_review"]["local"], {aur_name: aur})
            self.assertTrue(preview["system_upgrade"])
            self.assertNotIn("lock_after", json.dumps(preview))
            self.assertIsNone(pennix.owner_engine._package_receipt("yazi", new_component))
            self.assertIsNone(journal.read())
            writer.assert_not_called()
            confirm = [*command, "--confirm", preview["review_sha256"]]
            review.return_value = {"arch-commit": "d" * 64}
            with patch("sys.stderr", new_callable=io.StringIO) as error:
                self.assertEqual(pennix.main(confirm), 2)
            self.assertIn("摘要不匹配", error.getvalue())
            writer.assert_not_called()
            self.assertIsNone(journal.read())
            review.return_value = identity
            with patch.object(journal, "checkpoint_lock", side_effect=OSError("interrupted checkpoint")), \
                    patch("sys.stderr", new_callable=io.StringIO) as error:
                self.assertEqual(pennix.main(confirm), 2)
            self.assertIn("interrupted checkpoint", error.getvalue())
            active = journal.read()
            native = journal.step(active, "system-upgrade")
            record = journal.step(active, "system-upgrade:record")
            self.assertEqual(native["status"], "verified")
            self.assertEqual(record["status"], "unknown")
            self.assertEqual(active["lock_digest"], journal_start)
            self.assertEqual(journal.digest(lock), record["target"]["lock_after"])
            self.assertEqual(component_lock.read(lock, sources)["components"], expected_locks)
            recover = ["recover", "--transaction", active["transaction"], "--step", record["id"],
                       "--resolve", "completed", "--json"]
            with patch("sys.stderr", new_callable=io.StringIO) as error:
                self.assertEqual(pennix.main(recover), 2)
            self.assertIn("没有同一 Arch 提交", error.getvalue())
            self.assertEqual(journal.read(), active)
            observer.return_value = native["observed"]
            with patch("sys.stdout", new_callable=io.StringIO):
                self.assertEqual(pennix.main(recover), 0)
            self.assertIsNone(journal.read())
            self.assertEqual(pennix.owner_engine._package_receipt("tmux", component)["version"], expected["version"])
            self.assertEqual(pennix.owner_engine._package_receipt("yazi", new_component)["version"], new["version"])
            writer.assert_called_once_with(active["transaction"], native["target"], identity)
            if with_aur:
                self.assertEqual(pennix.owner_engine._package_receipt("aoe", aur_component)["version"], aur["version"])
            self.assertEqual(observer.call_count, 2)
            legacy.assert_not_called()

    def _check_full_review(self, key):
        conf, lock = self.root / 'workflow.config', self.root / 'components.lock.json'
        conf.write_text('original config')
        component = {'id': key, 'delivery': 'package', 'lock_source': f'npm:{key}',
                     'package': {'source': 'npm', 'name': key}}
        previous = {'source': f'npm:{key}', 'version': '1', 'sha256': 'a' * 64}
        target = {**previous, 'version': '2', 'sha256': 'b' * 64}
        catalog = {'components': {key: component}, 'configuration_targets': {}}
        systems = {'state': 'current', 'installed': {'fixture': '1'}, 'upgrades': []}
        report = {'components': {key: {'state': 'update', 'available': target}},
                  'system': systems, 'system_upgrade_enabled': True}
        sources = {key: component['lock_source']}
        component_lock.write(lock, {'schema': 1, 'components': {key: previous}}, sources)
        codex = self.root / 'codex'
        kconf = SimpleNamespace(syms={})
        is_paseo = key == "paseo-cli"
        running = is_paseo
        unit_enabled = is_paseo

        def runtime(*_):
            return {'state': 'current', 'running': running, 'stopped_verified': not running,
                    'daemon_version': '1' if running else None, 'pid': 42 if running else None,
                    'enabled': unit_enabled}

        def load(args):
            args.config_path, args.config_snapshot = conf, conf.read_bytes()
            args.codex_home, args.kconf = codex, kconf
            args.component_lock_path, args.component_lock_snapshot = lock, lock.read_bytes()
            args.component_lock, args.component_lock_sources = component_lock.read(lock, sources), sources
            return kconf, conf, catalog

        def plan(args):
            load(args)
            targets = getattr(args, 'update_targets', {})
            return {'config': str(conf), 'connections': {}, 'components': {
                key: {'selected': True, 'state': 'match', 'action': 'upgrade' if targets else 'keep',
                      'candidate_target': targets.get(key)},
                'paseo-autostart': {'selected': is_paseo, 'action': 'keep',
                                    'unit_state': 'current' if is_paseo else 'absent',
                                    'mode': 'login' if is_paseo else 'off', 'run': False, 'listen': '',
                                    'runtime': runtime(),
                                    **({'paseo_config': {'state': 'current', 'listen': {'state': 'current'},
                                        'restart_required': False,
                                        'versions': {'cli': '1', 'daemon': '1' if running else None,
                                                     'state': 'current' if running else 'not_run'}}}
                                       if is_paseo else {})},
            }, 'side_effects': {'system_upgrade': True, 'path': {'action': 'keep'}}}

        with ExitStack() as stack:
            for obj, name, effect in [(pennix, '_load', load), (pennix, '_plan', plan),
                    (pennix, '_updates', lambda args: (report, plan(args), catalog)),
                    (pennix.config_model, 'config_value', lambda *_: str(self.root / 'secrets')),
                    (pennix.owner_engine.host, 'detect_host', lambda: {'supported': True}),
                    (pennix.paseo_service, 'runtime', runtime),
                    (candidates, 'resolve', lambda *_: target)]:
                stack.enter_context(patch.object(obj, name, side_effect=effect))
            stack.enter_context(patch.object(pennix, '_review_target', return_value='a' * 64))
            stack.enter_context(patch.object(pennix, '_verify_update_candidates'))
            stack.enter_context(patch('builtins.input', side_effect=AssertionError('unexpected prompt')))
            for fn in ('skills', 'preflight', 'preflight_candidate', 'assert_no_active_work'):
                stack.enter_context(patch.object(pennix.paseo_service, fn))
            native = stack.enter_context(patch.object(pennix, '_run_component', return_value='changed'))
            system = stack.enter_context(patch.object(pennix.subprocess, 'run', side_effect=AssertionError('unexpected subprocess')))
            command = ['update', '--all', '--json']
            with patch.object(pennix.owned_files, 'locked', side_effect=AssertionError('preview locked')), \
                    patch('sys.stdout', new_callable=io.StringIO) as output:
                self.assertEqual(pennix.main([*command, '--plan']), 0)
            preview = json.loads(output.getvalue())
            self.assertEqual(preview['steps'], [{'component': key, 'action': 'upgrade', 'candidate': target}])
            self.assertFalse(preview['system_upgrade'])
            self.assertNotIn('secrets_digest', json.dumps(preview))
            self.assertIsNone(pennix.apply_journal.read())
            native.assert_not_called()
            confirm = [*command, '--confirm', preview['review_sha256']]
            if is_paseo:
                self.assertFalse(preview['execution_ready'])
                self.assertFalse(preview['service_restart'])
                self.assertEqual(preview['execution_preconditions'], ['paseo-daemon-stopped'])
                with patch('sys.stderr', new_callable=io.StringIO) as error:
                    self.assertEqual(pennix.main(confirm), 2)
                self.assertIn('daemon 已停止', error.getvalue())
                self.assertIsNone(pennix.apply_journal.read())
                native.assert_not_called()
                running = False
                with patch('sys.stdout', new_callable=io.StringIO) as output:
                    self.assertEqual(pennix.main([*command, '--plan']), 0)
                stopped_preview = json.loads(output.getvalue())
                self.assertTrue(stopped_preview['execution_ready'])
                self.assertEqual(stopped_preview['review_sha256'], preview['review_sha256'])
                unit_enabled = False
                with patch('sys.stderr', new_callable=io.StringIO) as error:
                    self.assertEqual(pennix.main(confirm), 2)
                self.assertIn('摘要不匹配', error.getvalue())
                self.assertIsNone(pennix.apply_journal.read())
                native.assert_not_called()
                unit_enabled = True
            for changed, revert in [
                (lambda: conf.write_text('changed config'), lambda: conf.write_text('original config')),
                (lambda: systems['upgrades'].append({'name': 'fixture'}), lambda: systems['upgrades'].clear()),
            ]:
                changed()
                with patch('sys.stdout', new_callable=io.StringIO), patch('sys.stderr', new_callable=io.StringIO):
                    self.assertNotEqual(pennix.main(confirm), 0)
                native.assert_not_called()
                self.assertIsNone(pennix.apply_journal.read())
                revert()
            with patch('sys.stdout', new_callable=io.StringIO):
                self.assertEqual(pennix.main(confirm), 0)
            self.assertEqual(component_lock.read(lock, sources)['components'], {key: target})
            self.assertIsNone(pennix.apply_journal.read())
            self.assertEqual(native.call_args.args[2:4], (key, 'upgrade'))
            system.assert_not_called()
