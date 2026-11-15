"""Optional real-owner contract test, always using a fresh home and local port."""
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import relay_owner


@unittest.skipUnless(os.environ.get("PENNIX_TEST_PASEO_CLI"), "pinned native Paseo CLI not selected")
class PaseoNativeTests(unittest.TestCase):
    def test_first_install_repeat_and_temporary_instance_cleanup(self):
        executable = str(Path(os.environ["PENNIX_TEST_PASEO_CLI"]).absolute())
        adapter = Path(__file__).resolve().parents[1] / "scripts/paseo-skills.mjs"
        with tempfile.TemporaryDirectory(prefix="pennix-native-contract-") as directory:
            root = Path(directory)
            home = root / "paseo"
            env = {"PATH": "/usr/bin:/bin", "HOME": directory, "CODEX_HOME": str(root / "nondefault-codex"),
                   **{f"XDG_{name}_HOME": str(root / name.lower()) for name in ("CONFIG", "STATE", "DATA", "CACHE")}}
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            result = subprocess.run([executable, "daemon", "config", "set", "daemon.listen", f"127.0.0.1:{port}",
                                     "--string", "--home", str(home), "--json"], env=env, capture_output=True, timeout=20)
            self.assertEqual(result.returncode, 0)
            for _ in range(2):
                result = subprocess.run(["node", str(adapter), "install", executable, str(home)],
                                        env=env, capture_output=True, text=True, timeout=150)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(json.loads(result.stdout)["temporary_daemon"])
                for target in (".agents", ".claude"):
                    for name in json.loads(result.stdout)["installed"]:
                        self.assertTrue((root / target / "skills" / name / "SKILL.md").is_file())
                status = subprocess.run([executable, "daemon", "status", "--home", str(home), "--json"],
                                        env=env, capture_output=True, text=True, timeout=20)
                self.assertEqual(status.returncode, 0)
                self.assertIsNone(json.loads(status.stdout)["pid"])
            self.assertFalse((root / "nondefault-codex").exists())
            with patch.dict(os.environ, env, clear=True):
                baseline = relay_owner._daemon_values(Path(executable), home)
                self.assertIs(baseline['daemon.relay.enabled'], False)
                self.assertTrue(all(value is relay_owner._MISSING for key,value in baseline.items()
                                    if key != 'daemon.relay.enabled'))
                self.assertEqual(relay_owner.configure_daemon(Path(executable), home,
                    'relay.test:443', 'public.test:443', True, True), 'changed')
                self.assertEqual(relay_owner.configure_daemon(Path(executable), home,
                    'relay.test:443', 'public.test:443', True, True), 'no-op')
                self.assertEqual(relay_owner.unconfigure_daemon(Path(executable), home), 'changed local relay configuration')
                self.assertEqual(relay_owner._daemon_values(Path(executable), home), baseline)
                self.assertFalse(relay_owner.daemon_owned(home))
