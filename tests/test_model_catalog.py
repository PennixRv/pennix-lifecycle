from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import model_catalog


class ModelCatalogTests(unittest.TestCase):
    def proof(self, raw):
        return {"schema": 1, "binary": {"path": "/usr/bin/codex", "sha256": "a" * 64,
                                      "version": "0.172.0"},
                "source_sha256": model_catalog._sha(raw), "provider_scope_sha256": "b" * 64,
                "endpoint_identity_sha256": "c" * 64, "endpoint_models": 1,
                "effective_models": len(model_catalog.parse_catalog(raw)["models"]),
                "fetched_at": "2026-11-28T00:00:00Z", "verified_at": "2026-11-28T00:00:01Z"}

    def test_generations_are_private_immutable_and_lossless_and_planning_is_read_only(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
                "HOME": directory, "XDG_STATE_HOME": directory + "/state"}):
            source = self.raw(self.source())
            value = model_catalog.candidate(source, self.proof(source))
            self.assertFalse((Path(directory) / "state").exists())
            descriptor = model_catalog.publish(value)
            self.assertEqual(model_catalog.read_generation(descriptor), value)
            self.assertEqual(model_catalog.publish(value), descriptor)
            path = Path(model_catalog.reference(descriptor))
            self.assertEqual(path.stat().st_mode & 0o777, 0o400)
            self.assertEqual(model_catalog.with_current_rule(descriptor), value)
            newer = model_catalog.candidate(source, self.proof(source), b"new default")
            model_catalog.publish(newer)
            self.assertNotEqual(newer["descriptor"], descriptor)
            self.assertEqual(model_catalog.read_generation(descriptor), value)
            path.chmod(0o600)
            path.write_bytes(b'{"models":[{"slug":"external"}]}')
            before = path.read_bytes()
            for action in (lambda: model_catalog.read_generation(descriptor),
                           lambda: model_catalog.publish(value)):
                with self.assertRaises(model_catalog.ModelCatalogError):
                    action()
                self.assertEqual(path.read_bytes(), before)

    def test_generations_reject_unknown_files_links_bad_proof_and_changed_candidate(self):
        for change in ("unknown", "link", "manifest", "candidate"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
                    "HOME": directory, "XDG_STATE_HOME": directory + "/state"}):
                source = self.raw(self.source())
                value = model_catalog.candidate(source, self.proof(source))
                descriptor = model_catalog.publish(value)
                path = Path(model_catalog.reference(descriptor)).parent
                if change == "unknown":
                    (path / "external").write_bytes(b"keep")
                elif change == "link":
                    (path / "source.json").unlink()
                    (path / "source.json").symlink_to(path / "derived.json")
                elif change == "manifest":
                    (path / "manifest.json").unlink()
                else:
                    value["contents"]["source.json"] = b"{}"
                with self.assertRaises(model_catalog.files.OwnershipError):
                    model_catalog.publish(value) if change == "candidate" else model_catalog.read_generation(descriptor)
        source = self.raw(self.source())
        for field in ("source_sha256", "binary", "effective_models", "fetched_at"):
            proof = self.proof(source)
            proof[field] = None
            with self.subTest(field=field), self.assertRaises(model_catalog.ModelCatalogError):
                model_catalog.candidate(source, proof)

    def source(self) -> dict:
        return {"future_metadata": {"revision": 17, "flags": [True, None, "中文"]}, "models": [
            {"slug": "z-model", "future_capability": {"enabled": True},
             "model_messages": {"permissions": {"workspace_write": "keep permissions"},
                                "collaboration_modes": {"default": "old Default", "plan": "keep Plan",
                                                        "future_mode": {"keep": [1, 2]}}}},
            {"slug": "a-model", "model_messages": None},
            {"slug": "no-messages"},
            {"slug": "null-modes", "model_messages": {"collaboration_modes": None, "apps": "keep apps"}},
        ]}

    def raw(self, value: object) -> bytes:
        return json.dumps(value, ensure_ascii=False).encode()

    def test_derivation_preserves_every_non_target_field_and_model_order(self):
        source = self.source()
        before = copy.deepcopy(source)
        rule = model_catalog.rule_bytes()
        first = model_catalog.derive(self.raw(source), rule)
        self.assertEqual(first, model_catalog.derive(self.raw(source), rule))
        self.assertEqual(source, before)
        derived = model_catalog.parse_catalog(first)
        self.assertEqual([m["slug"] for m in derived["models"]], [m["slug"] for m in source["models"]])
        self.assertEqual(derived["future_metadata"], source["future_metadata"])
        for model in derived["models"]:
            self.assertEqual(model["model_messages"]["collaboration_modes"]["default"], rule.decode())
        expected = copy.deepcopy(source)
        for model in expected["models"]:
            if model.get("model_messages") is None:
                model["model_messages"] = {}
            if model["model_messages"].get("collaboration_modes") is None:
                model["model_messages"]["collaboration_modes"] = {}
            model["model_messages"]["collaboration_modes"]["default"] = rule.decode()
        self.assertEqual(derived, expected)
        model_catalog.verify(self.raw(source), first, rule)

    def test_verification_rejects_non_target_metadata_model_loss_and_reordering(self):
        raw = self.raw(self.source())
        rule = model_catalog.rule_bytes()
        correct = model_catalog.parse_catalog(model_catalog.derive(raw, rule))
        mutations = []
        for path, value in (
            (("future_metadata", "revision"), 18),
            (("models", 0, "future_capability", "enabled"), False),
            (("models", 0, "future_capability", "enabled"), 1),
            (("models", 0, "model_messages", "collaboration_modes", "plan"), "changed Plan"),
            (("models", 0, "model_messages", "permissions", "workspace_write"), "changed permissions"),
            (("models", 0, "model_messages", "collaboration_modes", "default"), "wrong Default"),
        ):
            candidate = copy.deepcopy(correct)
            parent = candidate
            for part in path[:-1]:
                parent = parent[part]
            parent[path[-1]] = value
            mutations.append(candidate)
        mutations.extend([{**correct, "models": correct["models"][:-1]},
                          {**correct, "models": list(reversed(correct["models"]))}])
        for candidate in mutations:
            with self.subTest(candidate=candidate), self.assertRaises(model_catalog.ModelCatalogError):
                model_catalog.verify(raw, self.raw(candidate), rule)

    def test_bad_sources_and_rules_fail_without_reclassifying_them_as_empty_catalogs(self):
        invalid = [
            b"", b"\xff", b"[]", b'{"models":[]}', b'{"models":[],"models":[]}',
            b'{"models":[{"slug":"m","unknown":NaN}]}',
            b'{"models":[{"slug":"m","unknown":1e999}]}',
            self.raw({"models": [{"slug": "m"}, {"slug": "m"}]}),
            self.raw({"models": [{"slug": True}]}),
            self.raw({"models": [{"slug": "m", "model_messages": []}]}),
            self.raw({"models": [{"slug": "m", "model_messages": {"collaboration_modes": []}}]}),
            self.raw({"models": [{"slug": "m", "model_messages": {"collaboration_modes": {"default": 1}}}]}),
            b"x" * (model_catalog.MAX_CATALOG_BYTES + 1),
        ]
        for raw in invalid:
            with self.subTest(raw_length=len(raw)), self.assertRaises(model_catalog.ModelCatalogError):
                model_catalog.parse_catalog(raw)
        for rule in (b"", b"  ", b"\xff", b"x" * (32 * 1024 + 1)):
            with self.subTest(rule_length=len(rule)), self.assertRaises(model_catalog.ModelCatalogError):
                model_catalog.derive(self.raw(self.source()), rule)

    def native_fixture(self, root, mode="fresh"):
        binary = root / "codex"
        binary.write_text(
            "#!/usr/bin/python3\n"
            "import datetime,json,os,pathlib,sys\n"
            "if sys.argv[1:]==['--version']:\n print('codex-cli 0.172.0');sys.exit(0)\n"
            "assert sys.argv[1:]==['debug','models']\n"
            "home=pathlib.Path(os.environ['CODEX_HOME'])\n"
            "text=(home/'config.toml').read_text()\n"
            "assert 'model_catalog_json' not in text and 'must-not-copy' not in text\n"
            "assert 'CODEX_THREAD_ID' not in os.environ\n"
            "source={'models':[{'slug':'endpoint-model'},{'slug':'native-bundled-extra','keep':True}]}\n"
            f"mode={mode!r}\n"
            "cache={'fetched_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),"
            "'client_version':'0.172.0','identity':'synthetic-scope','models':[{'slug':'endpoint-model'}]}\n"
            "if mode=='stale':cache['fetched_at']='2000-01-01T00:00:00Z'\n"
            "if mode=='version':cache['client_version']='0.171.0'\n"
            "if mode=='scope':cache['identity']=None\n"
            "if mode!='fallback':(home/'models_cache.json').write_text(json.dumps(cache))\n"
            "print(json.dumps(source))\n"
            "if mode=='exit':sys.exit(3)\n"
        )
        binary.chmod(0o700)
        config = {"model_provider": "fixture", "model_catalog_json": "must-not-copy",
                  "mcp_servers": {"must-not-copy": {"command": "private"}},
                  "model_providers": {"fixture": {"name": "Fixture", "base_url": "https://example.invalid/v1",
                      "wire_api": "responses", "requires_openai_auth": False,
                      "auth": {"command": "/usr/bin/true", "args": []}}}}
        return binary, config

    def test_native_acquisition_preserves_effective_catalog_not_the_smaller_endpoint_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            binary, config = self.native_fixture(Path(directory))
            before = copy.deepcopy(config)
            raw, proof = model_catalog.acquire(binary, config)
            self.assertEqual(len(model_catalog.parse_catalog(raw)["models"]), 2)
            self.assertEqual((proof["endpoint_models"], proof["effective_models"]), (1, 2))
            self.assertEqual(proof["source_sha256"], __import__("hashlib").sha256(raw).hexdigest())
            self.assertEqual(config, before)
            self.assertNotIn("synthetic-scope", json.dumps(proof))
            self.assertNotIn("must-not-copy", json.dumps(proof))

    def test_fallback_old_cache_wrong_version_and_unscoped_or_failed_acquisition_are_rejected(self):
        for mode in ("fallback", "stale", "version", "scope", "exit"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                binary, config = self.native_fixture(Path(directory), mode)
                with self.assertRaises(model_catalog.ModelCatalogError):
                    model_catalog.acquire(binary, config)

    def test_unknown_provider_or_inline_auth_is_not_copied_to_metadata_probe(self):
        with tempfile.TemporaryDirectory() as directory:
            _, config = self.native_fixture(Path(directory))
            for bad in ({**config, "model_provider": None},
                        {**config, "model_providers": {"fixture": {**config["model_providers"]["fixture"],
                                                                 "experimental_bearer_token": "not-real"}}},
                        {**config, "model_providers": {"fixture": {**config["model_providers"]["fixture"],
                                                                 "auth": {"token": "not-real"}}}}):
                with self.assertRaises(model_catalog.ModelCatalogError):
                    model_catalog.provider_scope(bad)


if __name__ == "__main__":
    unittest.main()
