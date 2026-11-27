"""Trust boundaries and replay protection for selected host tool provisioning."""
from pathlib import Path
from types import SimpleNamespace
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import pennix
import system_devel_tools as tools
import update_inventory


class SystemDevelToolsTests(unittest.TestCase):
    def test_full_manifest_and_unselected_discovery(self):
        manifest = tools.requirements()
        self.assertEqual(len(manifest["packages"]), 32)
        with patch.object(tools, "installed_packages") as installed:
            self.assertEqual(tools.inspect(False)["state"], "not-selected")
            installed.assert_not_called()
        empty = tools.inspect(True, {})
        self.assertEqual(set(empty["missing"]), set(manifest["packages"]))
        self.assertEqual(empty["packages"]["clang"]["commands"], ["clang", "clang++", "clang-tidy"])

    def test_distinguishes_path_shadow_and_does_not_treat_it_as_missing(self):
        small = {"packages": {"gcc": ["gcc"]}, "conditional": {}}
        with patch.object(tools, "requirements", return_value=small), \
             patch.object(Path, "is_file", return_value=True), patch.object(tools.os, "access", return_value=True), \
             patch.object(tools, "_owner", return_value="gcc"), patch.object(tools.shutil, "which", return_value="/project/bin/gcc"):
            result = tools.inspect(True, {"gcc": "1"})
            self.assertEqual(result["state"], "entry-mismatch")
            self.assertEqual(result["missing"], [])
            self.assertEqual(result["packages"]["gcc"]["entries"]["gcc"]["state"], "path-override")
        with patch.object(tools, "requirements", return_value=small), \
             patch.object(Path, "is_file", return_value=True), patch.object(tools.os, "access", return_value=True), \
             patch.object(tools, "_owner", return_value="unexpected"), patch.object(tools.shutil, "which", return_value="/usr/bin/gcc"):
            result = tools.inspect(True, {"gcc": "1"})
            self.assertEqual(result["packages"]["gcc"]["entries"]["gcc"]["state"], "unverified-system-command")

    def test_native_preview_requires_exact_upgrades_official_candidates_and_no_prompts(self):
        upgrade = [{"name": "libx", "available": "2"}]
        cases = [
            ("extra\tmeson\t1\ncore\tlibx\t2\n", "", True),
            ("extra\tmeson\t1\n", "", False),
            ("external\tmeson\t1\ncore\tlibx\t2\n", "", False),
            ("extra\tmeson\t1\ncore\tlibx\t2\n", ":: Replace old? [Y/n]", False),
            ("extra\tmeson\t1\ncore\tlibx\t2\ncore\tlibx\t3\n", "", False),
        ]
        for text, diagnostics, expected in cases:
            with self.subTest(text=text, diagnostics=diagnostics), patch.object(
                        update_inventory, "run", return_value=subprocess.CompletedProcess([], 0, text, diagnostics)) as run:
                if not expected:
                    with self.assertRaises(update_inventory.UpdateError):
                        update_inventory._arch_transaction("/tempdb", {"libx": "1"}, upgrade, ["meson"])
                else:
                    rows = update_inventory._arch_transaction("/tempdb", {"libx": "1"}, upgrade, ["meson"])
                    self.assertEqual(rows, [
                        {"repo": "core", "name": "libx", "installed": "1", "target": "2"},
                        {"repo": "extra", "name": "meson", "installed": None, "target": "1"},
                    ])
                self.assertTrue(run.call_args.kwargs["noninteractive"])
                self.assertIn("-Sup", run.call_args.args[0])
                self.assertNotIn("-Syu", run.call_args.args[0])

    def test_noop_and_no_tty_never_start_package_writes(self):
        args = SimpleNamespace(json=True)
        for state, expect_error in [("ready", False), ("not-selected", False), ("missing", True)]:
            with self.subTest(state=state), patch.object(pennix, "_tools_projection", return_value=(
                    {"tooling": {"state": state}, "transaction_pending": False}, None, {})), \
                 patch.object(pennix, "_start_transaction") as start, patch.object(pennix, "_emit"):
                if expect_error:
                    with self.assertRaisesRegex(pennix.CliError, "TTY"):
                        pennix._apply_system_devel_tools(args)
                else:
                    self.assertEqual(pennix._apply_system_devel_tools(args), 0)
                start.assert_not_called()

    def test_recovery_uses_native_journal_and_never_replays_unknown_packages(self):
        case = tools.STEP
        args = SimpleNamespace(json=True, resolve="completed")
        with tempfile.TemporaryDirectory() as directory, patch.object(
                pennix.apply_journal, "path", return_value=Path(directory) / "journal"), \
             patch.object(pennix, "_finish_scoped_tools") as finish, patch.object(pennix, "_emit"):
            active = pennix.apply_journal.start("apply", tools.SCOPE, "a" * 64, None, [
                {"id": case, "component": tools.SCOPE, "action": "provision", "target": {"before": {}}},
                {"id": f"{case}:record", "component": tools.SCOPE, "action": "record", "target": {}},
            ])
            step = active["steps"][1]
            observed = pennix.apply_journal.started(active, case)
            with self.assertRaisesRegex(pennix.CliError, "不重放"):
                pennix._recover_scoped_tools(args, {}, observed, step)
            self.assertEqual(pennix.apply_journal.read(), observed)
            finish.assert_not_called()
            observed = pennix.apply_journal.verified(observed, case, {"effect": "reviewed-packages-installed"})
            self.assertEqual(pennix._recover_scoped_tools(args, {}, observed, step), 0)
            finish.assert_called_once()
            self.assertIsNone(pennix.apply_journal.read())
