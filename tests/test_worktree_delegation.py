"""Shared-component replacement must preserve active delegated writers."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import pennix


class WorktreeDelegationTests(unittest.TestCase):
    def test_optional_catalog_owner_dependencies_and_scripts(self):
        catalog = json.loads((pennix.ROOT / "references/catalog.json").read_text())
        item = catalog["components"]["trellis-paseo-adapter"]
        self.assertEqual(item["package"]["name"], "@pennixrv/trellis-paseo-adapter")
        self.assertEqual(pennix.DEPENDENCIES["trellis-paseo-adapter"], {"trellis-cli", "paseo-cli"})
        self.assertEqual(item["project_init"], "not-applicable")
        self.assertEqual(set(item["package"]["allow_scripts"]), {"esbuild", "node-pty", "msgpackr-extract"})

    def test_node_requirement_is_local_to_optional_adapter(self):
        args = argparse.Namespace(kconf=None)
        with patch.object(pennix.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "v20.19.0\n", "")):
            with self.assertRaisesRegex(pennix.CliError, "Node >=22"):
                pennix._delegation_preflight(args, "trellis-paseo-adapter", "install")
        pennix._delegation_preflight(args, "pnpm", "upgrade")

    def test_idle_or_unknown_delegation_blocks_shared_replacement(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(pennix.shutil, "which", return_value="/native/paseo"), patch.object(pennix.config_model, "config_value", return_value=directory):
            args = argparse.Namespace(kconf=None)
            status = subprocess.CompletedProcess([], 0, json.dumps({"pid": 123}), "")
            for raw in [json.dumps([{"status": "idle", "labels": {"trellis.attempt": "attempt"}}]), "invalid"]:
                with patch.object(pennix.subprocess, "run", side_effect=[status, subprocess.CompletedProcess([], 0, raw, "")]), self.assertRaises(pennix.CliError):
                    pennix._delegation_preflight(args, "codegraph-cli", "upgrade")
            closed = subprocess.CompletedProcess([], 0, json.dumps([{"status": "closed", "labels": {"trellis.attempt": "attempt"}}]), "")
            with patch.object(pennix.subprocess, "run", side_effect=[status, closed]):
                pennix._delegation_preflight(args, "codegraph-cli", "upgrade")


if __name__ == "__main__":
    unittest.main()
