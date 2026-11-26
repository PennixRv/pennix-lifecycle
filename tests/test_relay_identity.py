"""Exact remote ownership, lock ordering and native relay activation contracts."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import owned_files
import paseo_service
import relay_owner as relay
import pennix


class RelayIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        env = patch.dict(os.environ, {'HOME': str(self.root), 'XDG_STATE_HOME': str(self.root / 'state')})
        env.start();self.addCleanup(env.stop)
        self.home = self.root / 'paseo';self.home.mkdir()
        (self.home / 'server-id').write_text('fixture-server')
        (self.home / 'server-id').chmod(0o600)
        self.public = 'ssh-ed25519 AAAAsynthetic'
        self.target = {'nas_id': 'a'*64, 'router_id': hashlib.sha256(self.public.encode()).hexdigest(),
                       'uid': '0', 'resource': relay.MAP_PATH}

    def test_cli_dispatches_selected_relay_without_a_catalog_package(self):
        digest = 'b' * 64
        args = pennix.parser().parse_args(['repair', 'paseo-relay', '--expected-sha256', digest, '--json'])
        args.config_path = self.root / 'workflow.config'
        args.component_lock_path = self.root / 'components.lock.json'
        args.config_path.write_text('fixture config')
        args.component_lock_path.write_text('{}')
        values = {'PENNIX_CODEX_HOME': str(self.root / 'codex'), 'PENNIX_PASEO_HOME': str(self.home),
                  'PENNIX_PASEO_RELAY': 'y', 'PENNIX_PASEO_RELAY_REGISTER': 'y',
                  'PENNIX_RELAY_NAS_SSH': 'fixture-nas', 'PENNIX_RELAY_ROUTER_SSH': 'fixture-router'}
        with patch.object(pennix, '_load', return_value=(SimpleNamespace(), args.config_path, {'components': {}})), \
             patch.object(pennix.config_model, 'config_value', side_effect=lambda k, name: values[name]), \
             patch.object(relay, 'adopt_current', return_value='adopted-current') as adopt, \
             patch.object(pennix, '_emit'), owned_files.locked():
            self.assertEqual(pennix.cmd_repair(args), 0)
            adopt.assert_called_once_with(self.home, digest, nas_ssh='fixture-nas', router_ssh='fixture-router')
            self.assertIsNone(pennix.apply_journal.read())
            for key in ['PENNIX_PASEO_RELAY', 'PENNIX_PASEO_RELAY_REGISTER']:
                adopt.reset_mock()
                values[key] = 'n'
                with self.assertRaisesRegex(pennix.CliError, 'relay repair'):
                    pennix.cmd_repair(args)
                adopt.assert_not_called()
                self.assertIsNone(pennix.apply_journal.read())
                values[key] = 'y'

    def script(self):
        out = f"nas:{self.target['nas_id']}\nrouter:{self.target['router_id']}:0\nidentity\n"
        with patch.object(relay.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, out, '')) as run:
            self.assertEqual(relay._ssh('identity', 'fixture-server'), self.target)
        script = run.call_args.kwargs['input']
        public = self.root / 'host.pub';public.write_text(self.public + '\n')
        self.map = self.root / 'map';self.map.touch()
        self.bin = self.root / 'bin';self.bin.mkdir()
        for name, body in {
            'id': 'echo 0\n',
            'nginx': '[ "${FAIL_NGINX:-0}" != 1 ]\n',
            'mkdir': 'if [ -n "${WAIT_FOR_FIRST:-}" ]; then\n i=0; while [ ! -f "$WAIT_FOR_FIRST" ]; do i=$((i+1)); [ "$i" -lt 1000 ] || exit 1; sleep 0.01; done\nfi\nexec /usr/bin/mkdir "$@"\n',
        }.items():
            p = self.bin / name;p.write_text('#!/bin/sh\n' + body);p.chmod(0o700)
        return script.replace(relay.MAP_PATH, str(self.map)).replace('/etc/ssh/ssh_host_ed25519_key.pub', str(public))

    def test_management_errors_report_only_observed_hops_and_do_not_echo_stderr(self):
        nas = f"nas:{self.target['nas_id']}\n"
        router = f"router:{self.target['router_id']}:0\n"
        for status, output, detail in [(255, "", "失败跳点未知"),
                                        (255, nas, "未确认 router"),
                                        (47, nas + router + "busy\n", "登记表正被其他操作占用"),
                                        (44, nas + router + "duplicate\n", "重复身份")]:
            with self.subTest(status=status, output=bool(output)), patch.object(
                relay.subprocess, 'run', return_value=subprocess.CompletedProcess([], status, output, 'private error')
            ):
                with self.assertRaisesRegex(relay.RelayError, detail) as raised:
                    relay.inspect(self.home)
                self.assertNotIn('private error', str(raised.exception))
        with patch.object(relay.subprocess, 'run', side_effect=subprocess.TimeoutExpired('ssh', 60)):
            with self.assertRaisesRegex(relay.RelayError, '管理查询超时'):
                relay.inspect(self.home)

    def run_script(self, script, action, **environment):
        return subprocess.run(['sh', '-s', '--', action, 'fixture-server', self.target['router_id']],
            input=script, capture_output=True, text=True, timeout=20,
            env={**os.environ, 'PATH': str(self.bin) + ':/usr/bin:/bin', **environment})

    def test_lock_is_acquired_before_count_and_concurrent_registration_is_unique(self):
        script = self.script()
        done = self.root / 'first-done'
        def first():
            try:
                return self.run_script(script, 'add')
            finally:
                done.write_text('done')
        with ThreadPoolExecutor(max_workers=2) as pool:
            delayed = pool.submit(self.run_script, script, 'add', WAIT_FOR_FIRST=str(done))
            initial = pool.submit(first)
            a, b = initial.result(), delayed.result()
        self.assertEqual(a.returncode, 0, a.stderr)
        self.assertEqual(b.returncode, 0, b.stderr)
        self.assertTrue(a.stdout.endswith('added\n'))
        self.assertTrue(b.stdout.endswith('present\n'))
        self.assertEqual(len(self.map.read_text().splitlines()), 1)
        self.assertFalse(Path(str(self.map) + '.pennix-lock').exists())

    def test_identity_mismatch_and_nginx_failure_preserve_the_map(self):
        script = self.script()
        self.map.write_text('# external owner\n')
        before = self.map.read_bytes()
        wrong = script.replace(self.public.split()[1], 'BBBAsynthetic')
        # The fixture host key changes, while the expected fingerprint remains fixed.
        (self.root / 'host.pub').write_text('ssh-ed25519 BBBAsynthetic\n')
        result = self.run_script(wrong, 'add')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.map.read_bytes(), before)
        (self.root / 'host.pub').write_text(self.public + '\n')
        result = self.run_script(script, 'add', FAIL_NGINX='1')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.map.read_bytes(), before)
        self.assertFalse(Path(str(self.map) + '.pennix-lock').exists())

    def test_alias_target_change_cannot_authorize_removal(self):
        relay._write_record('fixture-server', self.target)
        raw = relay._state_path().read_bytes()
        out = f"nas:{'b'*64}\nrouter:{self.target['router_id']}:0\nremoved\n"
        with patch.object(relay.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, out, '')) as run:
            with self.assertRaisesRegex(relay.RelayError, '实际远端发生变化'):
                relay.remove(self.home, nas_ssh='alias-now-points-elsewhere')
            self.assertIn(self.target['nas_id'], run.call_args.args[0][-1])
            self.assertIn(self.target['router_id'], run.call_args.args[0][-1])
        self.assertEqual(relay._state_path().read_bytes(), raw)

    def test_legacy_receipt_requires_exact_review_of_current_registration(self):
        owned_files.replace(relay._state_path(), owned_files.json_bytes({'schema': 1, 'server_id': 'fixture-server'}), None)
        raw = relay._state_path().read_bytes()
        with patch.object(relay, '_ssh') as remote:
            self.assertEqual(relay.inspect(self.home), 'adoption-required')
            with self.assertRaises(relay.RelayError):relay.remove(self.home)
            with self.assertRaises(relay.RelayError):relay.ensure(self.home)
            remote.assert_not_called()
        with patch.object(relay, '_ssh', side_effect=lambda action, *args, **kwargs: self.target if action == 'identity' else 'present'):
            review = relay.review(self.home)
            with self.assertRaises(relay.RelayError):relay.adopt_current(self.home, '0'*64)
            self.assertEqual(relay._state_path().read_bytes(), raw)
            relay.adopt_current(self.home, review['review_sha256'])
        record = relay._record()
        self.assertEqual(record['target'], self.target)
        self.assertTrue(record['adopted_current'])

    def test_native_endpoint_restart_precedes_enable_and_proves_runtime(self):
        self.activation(False)

    def test_failed_restart_restores_configuration_and_actual_runtime(self):
        self.activation(True)

    def activation(self, fail_restart):
        paseo = self.root / 'paseo-bin';paseo.write_text('#!/bin/sh\nexit 99\n');paseo.chmod(0o700)
        keys = ['enabled', 'endpoint', 'publicEndpoint', 'useTls', 'publicUseTls']
        before = dict(zip(('daemon.relay.' + key for key in keys), [True, 'old:443', 'old:443', True, True]))
        persisted, live, operations = dict(before), dict(before), []
        receipt_path = self.root / 'daemon-receipt'
        record = {'schema': 1, 'home': str(self.home), 'desired': before,
                  'before': {key: {'present': True, 'value': value} for key, value in before.items()}}
        owned_files.replace(receipt_path, owned_files.json_bytes(record), None)
        raw = receipt_path.read_bytes()
        def run(command, **kwargs):
            if 'get' in command:
                key = command[command.index('get') + 1]
                return subprocess.CompletedProcess(command, 0, json.dumps({'set': key in persisted, 'value': persisted.get(key)}), '')
            key = command[command.index('set') + 1]
            value = command[command.index('set') + 2]
            value = value if '--string' in command else json.loads(value)
            persisted[key] = value;operations.append((key, value))
            if key == 'daemon.relay.enabled':live[key] = value
            return subprocess.CompletedProcess(command, 0, '{}', '')
        attempts = 0
        def restart(*args):
            nonlocal attempts
            attempts += 1;operations.append(('restart', live['daemon.relay.enabled']))
            self.assertFalse(persisted['daemon.relay.enabled'])
            if fail_restart and attempts == 1:
                raise relay.RelayError('injected restart failure')
            live.update(persisted)
        with patch.object(relay, '_daemon_receipt', return_value=(receipt_path, record)), \
                patch.object(relay, '_live_relay', side_effect=lambda *args: dict(live)), \
                patch.object(relay.subprocess, 'run', side_effect=run), \
                patch.object(relay, 'restart_daemon', side_effect=restart), \
                patch.object(paseo_service, 'assert_no_active_work') as guard:
            if fail_restart:
                with self.assertRaisesRegex(relay.RelayError, '已恢复原配置'):
                    relay.configure_daemon(paseo, self.home, 'new:443', 'new-public:443', True, True)
                self.assertEqual(persisted, before)
                self.assertEqual(live, before)
                self.assertEqual(receipt_path.read_bytes(), raw)
            else:
                relay.configure_daemon(paseo, self.home, 'new:443', 'new-public:443', True, True)
                self.assertEqual(live, persisted)
                self.assertEqual(live['daemon.relay.endpoint'], 'new:443')
                restart_at = operations.index(('restart', False))
                enable_at = operations.index(('daemon.relay.enabled', True))
                self.assertLess(restart_at, enable_at)
            guard.assert_called_once()


if __name__ == '__main__':unittest.main()
