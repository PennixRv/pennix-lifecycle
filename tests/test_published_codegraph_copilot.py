"""Exercise the public bundle's native writer, not a second mock of that writer."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import copilot_integrations as copilot
import owned_files


@unittest.skipUnless(os.environ.get('PENNIX_TEST_CODEGRAPH_MAIN_ARCHIVE') and
                     os.environ.get('PENNIX_TEST_CODEGRAPH_PLATFORM_ARCHIVE'), 'public CodeGraph payloads not selected')
class PublishedCodeGraphCopilotTests(unittest.TestCase):
    def test_native_writer_guard_and_owner_roundtrip_without_indexing(self):
        with tempfile.TemporaryDirectory(prefix='pennix-codegraph-copilot-') as directory:
            home = Path(directory)
            prefix = home / '.local'
            target = home / 'copilot'
            target.mkdir()
            env = {**os.environ, 'HOME': str(home), 'COPILOT_HOME': str(target), 'CODEX_HOME': str(home / 'codex'),
                   'XDG_STATE_HOME': str(home / 'state'), 'XDG_CONFIG_HOME': str(home / 'config'),
                   'XDG_DATA_HOME': str(home / 'data'), 'XDG_CACHE_HOME': str(home / 'cache'),
                   'PATH': str(prefix / 'bin') + os.pathsep + os.environ['PATH'],
                   'NPM_CONFIG_OFFLINE': 'true', 'NPM_CONFIG_USERCONFIG': '/dev/null', 'NPM_CONFIG_PROGRESS': 'false'}
            for command in [
                ['npm', 'install', '--global', '--prefix', str(prefix), '--ignore-scripts', '--omit=optional',
                 os.environ['PENNIX_TEST_CODEGRAPH_MAIN_ARCHIVE']],
                ['npm', 'install', '--prefix', str(prefix / 'lib/node_modules/@pennixrv/codegraph'),
                 '--ignore-scripts', '--no-save', '--package-lock=false', '--include=optional',
                 os.environ['PENNIX_TEST_CODEGRAPH_PLATFORM_ARCHIVE']],
            ]:
                result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=120)
                self.assertEqual(result.returncode, 0, result.stderr)
            with patch.dict(os.environ, env, clear=True), owned_files.locked():
                self.assertEqual(copilot.apply('codegraph', target, operation_id='public:configure'), 'changed')
                self.assertEqual(copilot.server('codegraph', target)['env'], {'CODEGRAPH_MANAGED': '1'})
                self.assertEqual(copilot.inspect('codegraph', target)['state'], 'current')
                self.assertEqual(copilot.apply('codegraph', target), 'no-op')
                self.assertEqual(copilot.unconfigure('codegraph', target), 'changed')
                self.assertIsNone(copilot.server('codegraph', target))
            self.assertFalse(any((home / '.codegraph').rglob('*.db')))
