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

    def test_cli_owned_sdk_allows_multiple_ordinary_agents_without_optional_adapter(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(pennix.shutil, "which", side_effect=lambda name: "/native/paseo" if name == "paseo" else None) as which, \
             patch.object(pennix.config_model, "config_value", return_value=directory), \
             patch.object(pennix.subprocess, "run", return_value=subprocess.CompletedProcess(
                 [], 0, json.dumps({"pid": 123, "serverId": "daemon"}), "")):
            observed = {"identity": {"pid": 123, "server_id": "daemon", "home": directory},
                        "agents_scanned": 1201, "delegations_open": False}
            args = argparse.Namespace(kconf=None)
            with patch.object(pennix.paseo_service, "inspect_delegations", return_value=observed) as inspect:
                pennix._delegation_preflight(args, "codegraph-cli", "upgrade")
                inspect.assert_called_once_with(Path("/native/paseo"), Path(directory))
                self.assertNotIn(unittest.mock.call("trellis-paseo-adapter"), which.call_args_list)
                observed["delegations_open"] = True
                with self.assertRaisesRegex(pennix.CliError, "未关闭"):
                    pennix._delegation_preflight(args, "codegraph-cli", "upgrade")
                observed["delegations_open"] = False
                for key, invalid in [("pid", 456), ("server_id", "other"), ("home", "/other")]:
                    original = observed["identity"][key]
                    observed["identity"][key] = invalid
                    with self.subTest(key=key), self.assertRaisesRegex(pennix.CliError, "不可核验"):
                        pennix._delegation_preflight(args, "codegraph-cli", "upgrade")
                    observed["identity"][key] = original
                observed.pop("delegations_open")
                with self.assertRaisesRegex(pennix.CliError, "不可核验"):
                    pennix._delegation_preflight(args, "codegraph-cli", "upgrade")
            with patch.object(pennix.paseo_service, "inspect_delegations",
                              side_effect=pennix.paseo_service.PaseoServiceError("SDK unavailable")), \
                 self.assertRaisesRegex(pennix.CliError, "不可核验"):
                pennix._delegation_preflight(args, "codegraph-cli", "upgrade")


if __name__ == "__main__":
    unittest.main()
