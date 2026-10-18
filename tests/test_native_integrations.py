from pathlib import Path
import os
import stat
import tempfile
import unittest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import native_integrations


FAKE_NATIVE = r'''#!/usr/bin/env python3
import os
from pathlib import Path
import sys

name = Path(sys.argv[0]).name
args = sys.argv[1:]
if name == "fastctx":
    home = Path(args[args.index("--codex-home") + 1])
    if args[0] == "apply":
        tier = args[args.index("--tier") + 1]
        limits = {"compact": (20000, 18000), "standard": (60000, 54000), "high": (100000, 90000)}
        host, total = limits[tier]
        env = {
            "FASTCTX_TOKEN_BUDGET": str(total),
            "FASTCTX_GREP_TOKEN_BUDGET": str(9000 if tier == "compact" else 10800),
            "FASTCTX_GLOB_TOKEN_BUDGET": str(4500 if tier == "compact" else 5400),
            "FASTCTX_RUN_TOKEN_BUDGET": str(9000 if tier == "compact" else 10800),
            "FASTCTX_JOB_OUTPUT_TOKEN_BUDGET": str(4500 if tier == "compact" else 5400),
        }
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.toml").write_text(
            f'tool_output_token_limit = {host}\n[mcp_servers.fastctx]\ncommand = "fastctx"\nargs = ["serve"]\n'
            + "[mcp_servers.fastctx.env]\n"
            + "\n".join(f'{key} = "{value}"' for key, value in env.items()) + "\n", encoding="utf-8")
        settings = Path.home() / ".fastctx"
        settings.mkdir(parents=True, exist_ok=True)
        (settings / "config.toml").write_text(f'tier = "{tier}"\n', encoding="utf-8")
    else:
        (home / "config.toml").unlink(missing_ok=True)
        (Path.home() / ".fastctx/config.toml").unlink(missing_ok=True)
else:
    home = Path(os.environ["CODEX_HOME"])
    config = home / "config.toml"
    if args[0] == "install":
        home.mkdir(parents=True, exist_ok=True)
        config.write_text('[mcp_servers]\n[mcp_servers.codegraph]\ncommand = "codegraph"\nargs = ["serve", "--mcp"]\n', encoding="utf-8")
    else:
        config.write_text('[mcp_servers]\n[mcp_servers.other]\ncommand = "other"\nargs = ["serve"]\n', encoding="utf-8")
'''


class NativeIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.codex = self.root / "codex"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for name in ("fastctx", "codegraph"):
            path = self.bin / name
            path.write_text(FAKE_NATIVE, encoding="utf-8")
            path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        self.old = {key: os.environ.get(key) for key in ("HOME", "PATH", "XDG_STATE_HOME", "CODEX_HOME")}
        os.environ.update({
            "HOME": str(self.home),
            "PATH": f"{self.bin}:{self.old['PATH'] or ''}",
            "XDG_STATE_HOME": str(self.root / "state"),
            "CODEX_HOME": str(self.codex),
        })
        self.original_home = Path.home
        Path.home = lambda: self.home

    def tearDown(self):
        Path.home = self.original_home
        for key, value in self.old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temp.cleanup()

    def test_fastctx_native_owner_configures_and_unconfigures(self):
        self.assertEqual(native_integrations.inspect("fastctx", self.codex, enabled=True, tier="standard")["state"], "absent")
        self.assertEqual(native_integrations.apply("fastctx", self.codex, tier="standard"), "changed")
        self.assertEqual(native_integrations.inspect("fastctx", self.codex, enabled=True, tier="standard")["state"], "current")
        self.assertEqual(native_integrations.inspect("fastctx", self.codex, enabled=True, tier="high")["state"], "drifted")
        self.assertEqual(native_integrations.unconfigure("fastctx", self.codex, tier="standard"), "changed")
        self.assertEqual(native_integrations.inspect("fastctx", self.codex, enabled=True, tier="standard")["state"], "absent")

    def test_external_native_config_is_not_taken_over(self):
        native_integrations.apply("fastctx", self.codex, tier="standard")
        native_integrations._receipt_path("fastctx").unlink()
        with self.assertRaises(native_integrations.NativeIntegrationError):
            native_integrations.apply("fastctx", self.codex, tier="standard")

    def test_partial_external_native_config_is_not_taken_over(self):
        self.codex.mkdir(parents=True)
        (self.codex / "config.toml").write_text(
            '[mcp_servers.fastctx]\ncommand = "fastctx"\n', encoding="utf-8"
        )
        self.assertEqual(
            native_integrations.inspect("fastctx", self.codex, enabled=True, tier="standard")["state"],
            "external",
        )
        with self.assertRaises(native_integrations.NativeIntegrationError):
            native_integrations.apply("fastctx", self.codex, tier="standard")

    def test_drifted_native_config_is_not_removed(self):
        native_integrations.apply("fastctx", self.codex, tier="standard")
        config = self.codex / "config.toml"
        config.write_text(config.read_text(encoding="utf-8").replace('command = "fastctx"', 'command = "other"'), encoding="utf-8")
        with self.assertRaises(native_integrations.NativeIntegrationError):
            native_integrations.unconfigure("fastctx", self.codex, tier="standard")
        self.assertTrue(config.exists())
        self.assertTrue(native_integrations._receipt_path("fastctx").exists())

    def test_codegraph_owner_preserves_the_cli_config_boundary(self):
        self.assertEqual(native_integrations.apply("codegraph", self.codex, tier="standard"), "changed")
        self.assertEqual(native_integrations.inspect("codegraph", self.codex, enabled=True, tier="standard")["state"], "current")
        self.assertEqual(native_integrations.unconfigure("codegraph", self.codex, tier="standard"), "changed")
        self.assertEqual(native_integrations.inspect("codegraph", self.codex, enabled=True, tier="standard")["state"], "absent")
        self.assertIn('command = "other"', (self.codex / "config.toml").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
