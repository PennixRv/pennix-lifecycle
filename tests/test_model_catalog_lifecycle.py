import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import codex_config
import config_model
import model_catalog
import test_model_catalog as fixtures
import apply_journal
import pennix


class ModelCatalogLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        environment = patch.dict(os.environ, {
            "HOME": str(self.home), "XDG_STATE_HOME": str(self.home / "state"),
            "XDG_CONFIG_HOME": str(self.home / "settings")})
        environment.start()
        self.addCleanup(environment.stop)
        path = config_model.init()
        kconf, _ = config_model.load(str(path))
        kconf.syms["PENNIX_CODEX_MANAGED_CATALOG"].set_value("y")
        kconf.syms["PENNIX_CODEX_HOME"].set_value(str(self.home / "custom-codex"))
        config_model.write(kconf, path)
        self.config = path
        self.values = config_model.values(kconf)
        self.assertEqual(self.values["PENNIX_CODEX_MANAGED_CATALOG"], "y")
        self.path = self.home / "custom-codex/config.toml"
        self.path.parent.mkdir(mode=0o700)
        self.path.write_text('foreign = "keep"\n')
        self.path.chmod(0o600)
        fixture = fixtures.ModelCatalogTests()
        source = fixture.raw(fixture.source())
        self.candidate = model_catalog.candidate(source, fixture.proof(source))
        compatible = patch.object(model_catalog, "compatible")
        compatible.start()
        self.addCleanup(compatible.stop)

    def apply(self, candidate=None):
        return codex_config.apply(self.path, self.values, catalog_candidate=candidate)

    def provider(self):
        from tomlkit import dumps

        provider = {"model_provider": "fixture", "model_providers": {"fixture": {
            "name": "fixture", "base_url": "https://provider.invalid/v1", "wire_api": "responses",
            "requires_openai_auth": False, "auth": {"command": "/usr/bin/false", "args": []}}}}
        self.path.write_text(dumps({**provider, "foreign": "keep"}))
        source = self.candidate["contents"]["source.json"]
        proof = fixtures.ModelCatalogTests().proof(source)
        proof["provider_scope_sha256"] = model_catalog.provider_scope(provider)[1]
        return source, proof

    def test_source_required_then_first_baseline_preserved_across_rule_update_and_uninstall(self):
        before = self.path.read_bytes()
        self.assertEqual(codex_config.inspect(self.path, self.values)["state"], "blocked")
        with self.assertRaisesRegex(codex_config.CodexConfigError, "固定候选"):
            self.apply()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.apply(self.candidate), "changed")
        first = codex_config._load_record(self.path)
        self.assertEqual(first["schema"], 4)
        self.assertIsNone(first["before"]["model_catalog_json"])
        self.assertEqual(self.apply(), "no-op")
        with patch.object(model_catalog, "rule_bytes", return_value=b"new rule"):
            self.assertEqual(self.apply(), "changed")
            second = codex_config._load_record(self.path)
            self.assertEqual(second["before"], first["before"])
            self.assertEqual(second["retained_catalogs"], [first["catalog"]])
            model_catalog.read_generation(first["catalog"])
            self.assertEqual(codex_config.inspect(self.path, self.values)["state"], "current")
        self.assertEqual(codex_config.uninstall(self.path), "changed")
        self.assertIsNone(codex_config._load_record(self.path))
        self.assertNotIn("model_catalog_json", self.path.read_text())
        self.assertIn('foreign = "keep"', self.path.read_text())
        self.assertFalse(Path(model_catalog.reference(first["catalog"])).parent.exists())
        self.assertFalse(Path(model_catalog.reference(second["catalog"])).parent.exists())
        self.assertEqual(codex_config.uninstall(self.path), "no-op")

    def test_external_reference_needs_exact_adoption_and_never_takes_file_ownership(self):
        external = self.home / "external.json"
        raw = self.candidate["contents"]["source.json"]
        external.write_bytes(raw)
        self.path.write_text('foreign = "keep"\nmodel_catalog_json = ' + json.dumps(str(external)) + "\n")
        before = self.path.read_bytes()
        with self.assertRaisesRegex(codex_config.CodexConfigError, "精确摘要"):
            self.apply(self.candidate)
        self.assertEqual(self.path.read_bytes(), before)
        self.values["PENNIX_CODEX_CATALOG_ADOPT_SHA256"] = codex_config.catalog_adoption_digest(self.path)
        self.apply(self.candidate)
        self.assertEqual(external.read_bytes(), raw)
        external.unlink()
        active = self.path.read_bytes()
        with self.assertRaisesRegex(codex_config.CodexConfigError, "不可用"):
            codex_config.uninstall(self.path)
        self.assertEqual(self.path.read_bytes(), active)
        external.write_bytes(raw)
        codex_config.uninstall(self.path)
        self.assertIn(str(external), self.path.read_text())
        self.assertEqual(external.read_bytes(), raw)

    def test_deselection_restores_reference_but_keeps_provable_asset_ownership_until_uninstall(self):
        self.apply(self.candidate)
        original = codex_config._load_record(self.path)
        self.values["PENNIX_CODEX_MANAGED_CATALOG"] = "n"
        self.assertEqual(self.apply(), "changed")
        record = codex_config._load_record(self.path)
        self.assertIsNone(record["catalog"])
        self.assertEqual(record["retained_catalogs"], [original["catalog"]])
        self.assertNotIn("model_catalog_json", self.path.read_text())
        self.assertEqual(self.apply(), "no-op")
        codex_config.uninstall(self.path)
        self.assertFalse(Path(model_catalog.reference(original["catalog"])).exists())

    def test_asset_drift_future_schema_and_incompatible_source_fail_without_overwrite(self):
        self.apply(self.candidate)
        record = codex_config._load_record(self.path)
        asset = Path(model_catalog.reference(record["catalog"]))
        asset.chmod(0o600)
        asset.write_bytes(b"external")
        before = self.path.read_bytes()
        for action in (self.apply, lambda: codex_config.inspect(self.path, self.values),
                       lambda: codex_config.uninstall(self.path)):
            with self.assertRaises(codex_config.CodexConfigError):
                action()
            self.assertEqual(self.path.read_bytes(), before)
        state = codex_config._state_path()
        record["schema"] = 99
        state.write_bytes(model_catalog.files.json_bytes(record))
        with self.assertRaisesRegex(codex_config.CodexConfigError, "版本"):
            self.apply()

    def test_native_static_update_freezes_review_then_commits_same_journal_and_rejects_stale_review(self):
        source, proof = self.provider()
        identity = proof["binary"]
        def command(*options):
            args = pennix.parser().parse_args(["update", "codex-config", "--config", str(self.config),
                                               "--json", *options])
            with patch.object(pennix, "_emit") as emit:
                self.assertEqual(pennix.cmd_update(args), 0)
                return emit.call_args.args[0]
        with patch.object(model_catalog, "binary_identity", return_value=identity), \
                patch.object(model_catalog, "acquire", return_value=(source, proof)) as acquire:
            before = self.path.read_bytes()
            review = command("--plan")
            self.assertEqual(review["component"], "codex-config")
            self.assertEqual(self.path.read_bytes(), before)
            self.assertIsNone(codex_config._load_record(self.path))
            self.assertIsNone(apply_journal.read())
            self.assertFalse(Path(model_catalog.reference(review["catalog"])).exists())
            self.path.write_bytes(before + b'# concurrent change\n')
            with self.assertRaisesRegex(codex_config.CodexConfigError, "已改变"):
                command("--confirm", review["review_sha256"])
            self.assertIsNone(apply_journal.read())
            self.path.write_bytes(before)
            result = command("--confirm", review["review_sha256"])
            self.assertEqual(result["status"], "changed")
            self.assertIsNone(apply_journal.read())
            self.assertEqual(codex_config._load_record(self.path)["catalog"], review["catalog"])
            acquire.assert_called_once()

    def test_all_update_catalog_checkpoint_waits_for_dependencies_and_retains_same_transaction(self):
        source, proof = self.provider()
        args = pennix.parser().parse_args(["update", "--all", "--config", str(self.config), "--json"])
        kconf, _, catalog = pennix._load(args)
        args.kconf = kconf
        review = {"mode": "noninteractive-v1", "targets": {}, "available": {}, "system": {"upgrades": []},
                  "catalog_digest": apply_journal.digest(pennix.CATALOG), "secrets_digest": None,
                  "runtime_digest": pennix.release_owner._runtime_digest(pennix.ROOT)}
        args.update_review, args.update_targets = review, {}
        journal = pennix._start_transaction(args, "update-all", None, [
            {"id": "update-review", "component": "update-review", "action": "review", "target": review},
            {"id": "dependency", "component": "codex-cli", "action": "upgrade", "target": {"version": "fixture"}},
            {"id": "codex-config:catalog-review", "component": "codex-config", "action": "catalog-review",
             "target": {"source": "after-approved-dependencies"}},
            {"id": "after", "component": "codex-agents", "action": "upgrade", "target": {}}])
        step = journal["steps"][2]
        with patch.object(model_catalog, "acquire", return_value=(source, proof)) as acquire, \
                patch.object(model_catalog, "binary_identity", return_value=proof["binary"]), \
                patch.object(pennix, "_emit") as emit:
            before = self.path.read_bytes()
            with self.assertRaisesRegex(pennix.CliError, "依赖"):
                pennix._catalog_checkpoint(args, journal, step)
            acquire.assert_not_called()
            journal = apply_journal.verified(journal, "update-review")
            journal = apply_journal.verified(journal, "dependency")
            self.assertEqual(pennix._catalog_checkpoint(args, journal, journal["steps"][2]), 2)
            checkpoint = emit.call_args.args[0]
            self.assertEqual(checkpoint["status"], "catalog-review-required")
            self.assertEqual(self.path.read_bytes(), before)
            self.assertEqual(apply_journal.read()["steps"][2]["status"], "pending")
            child = pennix.parser().parse_args(["update", "codex-config", "--config", str(self.config),
                                                "--json", "--confirm", checkpoint["review_sha256"]])
            self.assertEqual(pennix.cmd_update(child), 0)
            committed = apply_journal.read()
            self.assertEqual(committed["transaction"], journal["transaction"])
            self.assertEqual([step["status"] for step in committed["steps"]],
                             ["verified", "verified", "verified", "pending"])
            self.assertEqual(emit.call_args.args[0]["next"], "pennix update --all --resume")
            acquire.assert_called_once()
            # The resumed plan preserves the exact admitted checkpoint instead of adding a second install.
            value = {"components": {
                "codex-config": {"selected": True, "state": "current", "action": "keep"},
                "paseo-autostart": {"selected": False, "action": "keep", "unit_state": "absent", "run": False,
                                   "runtime": {"running": False}}},
                "connections": {}, "side_effects": {"path": {"action": "keep"}}}
            exact = pennix._reviewed_update_steps(value, args, catalog)
            admitted = next(step for step in exact if step["component"] == "codex-config")
            self.assertEqual(admitted["target"], committed["steps"][2]["target"])
            self.assertEqual(admitted["action"], "catalog-review")
            apply_journal.started(committed, "after")
            resume = pennix.parser().parse_args(["update", "--all", "--resume", "--config", str(self.config)])
            with self.assertRaisesRegex(pennix.CliError, "未知副作用"):
                pennix.cmd_update_all(resume)

    def test_resume_is_not_a_new_review_or_unknown_effect_retry(self):
        for options in (["update", "--all", "--resume"], ["update", "codex-config", "--resume"]):
            with self.subTest(options=options), patch.object(pennix, "_updates") as updates:
                args = pennix.parser().parse_args([*options, "--config", str(self.config)])
                with self.assertRaisesRegex(pennix.CliError, "--resume"):
                    pennix.cmd_update(args)
                updates.assert_not_called()

    def test_updates_catalog_inventory_checks_native_freshness_without_writing_active_assets(self):
        source, proof = self.provider()
        lock = self.home / "components.lock.json"
        with patch.object(model_catalog, "binary_identity", return_value=proof["binary"]), \
                patch.object(model_catalog, "acquire", return_value=(source, proof)):
            before = self.path.read_bytes()
            self.assertEqual(codex_config.catalog_inventory(self.path, self.values, lock)["state"], "update")
            self.assertEqual(self.path.read_bytes(), before)
            self.assertIsNone(codex_config._load_record(self.path))
            value = model_catalog.candidate(source, proof)
            self.apply(value)
            self.assertEqual(codex_config.catalog_inventory(self.path, self.values, lock)["state"], "current")
            changed = {**proof, "endpoint_identity_sha256": "d" * 64}
            with patch.object(model_catalog, "acquire", return_value=(source, changed)):
                self.assertEqual(codex_config.catalog_inventory(self.path, self.values, lock)["state"], "update")

    def test_unchanged_native_source_is_noop_without_another_confirmation_or_generation(self):
        source, proof = self.provider()
        self.apply(model_catalog.candidate(source, proof))
        record = codex_config._load_record(self.path)
        configuration = self.path.read_bytes()
        args = pennix.parser().parse_args(["update", "codex-config", "--config", str(self.config), "--json"])
        fresh = {**proof, "verified_at": "2026-11-28T00:00:05Z"}
        with patch.object(model_catalog, "binary_identity", return_value=proof["binary"]), \
                patch.object(model_catalog, "acquire", return_value=(source, fresh)), \
                patch.object(pennix, "_emit") as emit, patch("builtins.input") as question:
            self.assertEqual(pennix.cmd_update(args), 0)
            self.assertEqual(emit.call_args.args[0]["status"], "no-op")
            question.assert_not_called()
        self.assertEqual(codex_config._load_record(self.path), record)
        self.assertEqual(self.path.read_bytes(), configuration)
        self.assertIsNone(apply_journal.read())

    def test_native_asset_repair_uses_exact_review_and_never_refreshes_fixed_source(self):
        source, proof = self.provider()
        self.apply(model_catalog.candidate(source, proof))
        record = codex_config._load_record(self.path)
        root = Path(model_catalog.reference(record["catalog"])).parent
        before_config = self.path.read_bytes()
        for name, broken in (("derived.json", None), ("derived.json", b"damaged fixture"),
                             ("manifest.json", None), ("manifest.json", b"damaged manifest")):
            with self.subTest(name=name, broken=broken):
                target = root / name
                target.chmod(0o600)
                if broken is None:
                    target.unlink()
                else:
                    target.write_bytes(broken)
                args = pennix.parser().parse_args(["repair", "codex-config", "--config", str(self.config), "--json"])
                kconf, _, catalog = pennix._load(args)
                args.kconf = kconf
                state = pennix._component_status(args, "codex-config", catalog["components"]["codex-config"])
                self.assertEqual(state["state"], "drifted")
                with self.assertRaisesRegex(codex_config.CodexConfigError, "精确摘要"):
                    codex_config.repair_catalog(self.path, "a" * 64)
                self.assertEqual(target.read_bytes() if target.exists() else None, broken)
                args.expected_sha256 = state["catalog_repair_sha256"]
                with patch.object(model_catalog, "acquire") as acquire, patch.object(pennix, "_emit"), \
                        patch.object(pennix, "_configure_updated_connections", side_effect=lambda a, c, j, **kw: j):
                    self.assertEqual(pennix.cmd_repair(args), 0)
                    acquire.assert_not_called()
                self.assertEqual(codex_config._load_record(self.path), record)
                self.assertEqual(self.path.read_bytes(), before_config)
                model_catalog.read_generation(record["catalog"])
                self.assertIsNone(apply_journal.read())
        source_file = root / "source.json"
        source_file.chmod(0o600)
        source_file.write_bytes(b"foreign source")
        with self.assertRaisesRegex(codex_config.CodexConfigError, "固定源"):
            codex_config.catalog_repair_review(self.path)
        self.assertEqual(source_file.read_bytes(), b"foreign source")

    def test_boolean_numeric_drift_is_not_equal_or_repairable(self):
        from tomlkit import parse

        self.apply(self.candidate)
        original = self.path.read_bytes()
        record = codex_config._load_record(self.path)
        for key in ("features.hooks", "features.goals"):
            self.path.write_bytes(original)
            doc = parse(original.decode())
            self.assertIs(type(record["after"][key]), bool)
            codex_config._set(doc, key, int(record["after"][key]))
            self.path.write_text(doc.as_string())
            before = self.path.read_bytes()
            for operation in (self.apply, lambda: codex_config.inspect(self.path, self.values),
                              lambda: codex_config.catalog_repair_review(self.path)):
                with self.subTest(key=key), self.assertRaises(codex_config.CodexConfigError):
                    operation()
                self.assertEqual(self.path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
