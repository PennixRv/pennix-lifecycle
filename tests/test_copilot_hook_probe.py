"""Native hook availability is independent of the installed Skill revision."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import copilot_integrations as copilot


EVENTS = ['sessionStart', 'userPromptSubmitted', 'preToolUse', 'postToolUse']


class CopilotHookProbeTests(unittest.TestCase):
    def test_framed_native_reply_preserves_policy_and_ignores_foreign_hooks(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            # This stale file is deliberately not a template from the current package.
            skill = home / 'skills/ctx-doctor/SKILL.md'
            skill.parent.mkdir(parents=True)
            skill.write_text('previous receipted Skill\n')
            executable = home / 'copilot-probe'
            executable.write_text('''#!/usr/bin/python3
import json, os, sys
assert sys.argv[1:] == ['--server', '--stdio', '--no-auto-update']
header = b''
while not header.endswith(b'\\r\\n\\r\\n'):
    header += sys.stdin.buffer.read(1)
size = int(header.split(b':', 1)[1].strip())
request = json.loads(sys.stdin.buffer.read(size))
assert request == {'jsonrpc': '2.0', 'id': 1, 'method': 'hooks.discover', 'params': {}}
reply = open(os.path.join(os.environ['COPILOT_HOME'], 'reply'), 'rb').read()
for chunk in [b'Content-Len', b'gth: ' + str(len(reply)).encode() + b'\\r\\n\\r\\n', reply[:8], reply[8:]]:
    sys.stdout.buffer.write(chunk)
    sys.stdout.buffer.flush()
''')
            executable.chmod(0o700)
            good = [{'hookType': event, 'source': 'hooks/context-mode.json', 'origin': 'user', 'enabled': True}
                    for event in EVENTS]
            foreign = {'hookType': 'sessionStart', 'source': '/other/hooks.json', 'origin': 'user', 'enabled': False}
            for name, hooks, identity, accepted in [
                ('stale-owned-skill', good + [foreign], 1, True),
                ('disabled', [{**h, 'enabled': False} if h['hookType'] == 'preToolUse' else h for h in good], 1, False),
                ('missing', good[:-1], 1, False),
                ('wrong-source', [{**h, 'source': 'hooks/foreign.json'} for h in good], 1, False),
                ('wrong-response', good, 2, False),
            ]:
                with self.subTest(name=name), patch.object(copilot.shutil, 'which', return_value=str(executable)):
                    (home / 'reply').write_text(json.dumps({'jsonrpc': '2.0', 'id': identity, 'result': {'hooks': hooks}}))
                    if accepted:
                        copilot.verify_hooks(home)
                    else:
                        with self.assertRaises(copilot.Error):
                            copilot.verify_hooks(home)
                    self.assertEqual(skill.read_text(), 'previous receipted Skill\n')


@unittest.skipUnless(os.environ.get('PENNIX_TEST_COPILOT_CLI'), 'native Copilot metadata probe not selected')
class NativeCopilotHookProbeTests(unittest.TestCase):
    def test_enabled_and_globally_disabled_hooks_without_model_or_auth(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            hook = home / 'hooks/context-mode.json'
            hook.parent.mkdir()
            hook.write_text(json.dumps({'version': 1, 'hooks': {event: [{
                'type': 'command', 'command': 'true', 'powershell': 'exit 0', 'timeoutSec': 5}]
                for event in EVENTS}}))
            with patch.dict(os.environ, {'HOME': str(home), 'COPILOT_HOME': str(home)}), \
                    patch.object(copilot.shutil, 'which', return_value=os.environ['PENNIX_TEST_COPILOT_CLI']):
                copilot.verify_hooks(home)
                settings = home / 'settings.json'
                settings.write_text('{"disableAllHooks":true}\n')
                before = settings.read_bytes(), hook.read_bytes()
                with self.assertRaises(copilot.Error):
                    copilot.verify_hooks(home)
                self.assertEqual((settings.read_bytes(), hook.read_bytes()), before)
