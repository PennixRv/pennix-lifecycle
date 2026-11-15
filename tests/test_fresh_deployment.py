"""Fresh dependencies and unified updates keep the native ownership boundaries."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import apply_journal
import candidates
import component_lock
import config_model
import connection_owners
import owned_files
import pennix
import release_owner
import update_inventory as inventory


class FreshDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        env = patch.dict(os.environ, {'HOME': str(self.root), 'CODEX_HOME': str(self.root/'codex'),
            'XDG_CONFIG_HOME': str(self.root/'config'), 'XDG_STATE_HOME': str(self.root/'state')})
        env.start()
        self.addCleanup(env.stop)

    def test_new_optional_cli_keeps_existing_external_copilot_choices(self):
        config = config_model.init()
        kconf, _ = config_model.load()
        kconf.syms['PENNIX_COMPONENT_CONTEXT_MODE'].set_value('y')
        kconf.syms['PENNIX_TARGET_COPILOT_CONTEXT_MODE'].set_value('y')
        kconf.syms['PENNIX_TARGET_COPILOT_CODEGRAPH'].set_value('y')
        config_model.write(kconf, config)
        config.write_text('\n'.join(line for line in config.read_text().splitlines()
                                   if 'PENNIX_COMPONENT_COPILOT_CLI' not in line) + '\n')
        kconf, _ = config_model.load()
        self.assertEqual(kconf.syms['PENNIX_COMPONENT_COPILOT_CLI'].str_value, 'n')
        self.assertEqual(kconf.syms['PENNIX_TARGET_COPILOT_CONTEXT_MODE'].str_value, 'y')
        self.assertEqual(kconf.syms['PENNIX_TARGET_COPILOT_CODEGRAPH'].str_value, 'y')

    def test_fresh_copilot_defers_only_absent_target_and_respects_disabled_hooks(self):
        config = config_model.init()
        kconf, _ = config_model.load()
        kconf.syms['PENNIX_COMPONENT_CONTEXT_MODE'].set_value('y')
        kconf.syms['PENNIX_TARGET_COPILOT_CONTEXT_MODE'].set_value('y')
        catalog = pennix.owner_engine.load_catalog(pennix.CATALOG)
        spec = catalog['configuration_targets']['copilot-context-mode']
        catalog = {'configuration_targets': {'copilot-context-mode': spec}}
        # 原生模板确实不可用，而不是把所有检查都 mock 为成功。
        with patch.object(pennix.native_integrations.copilot, 'desired', side_effect=pennix.native_integrations.copilot.Error('missing owner')):
            value = connection_owners.plan(kconf, catalog, self.root/'codex', self.root/'secrets', installing={'context-mode', 'copilot-cli'})
            self.assertEqual(value['copilot-context-mode']['state'], 'pending-owner')
            home = self.root/'.copilot'
            home.mkdir()
            (home/'settings.json').write_text('{"disableAllHooks":true}')
            value = connection_owners.plan(kconf, catalog, self.root/'codex', self.root/'secrets', installing={'context-mode'})
            self.assertEqual(value['copilot-context-mode']['action'], 'blocked')
            (home/'settings.json').unlink()
            (home/'mcp-config.json').write_text('{"mcpServers":{"context-mode":{"command":"foreign"}}}')
            value = connection_owners.plan(kconf, catalog, self.root/'codex', self.root/'secrets', installing={'context-mode'})
            self.assertEqual(value['copilot-context-mode']['action'], 'blocked')

    def test_missing_relay_identity_is_generated_before_registration_not_copied(self):
        config_model.init()
        kconf, _ = config_model.load()
        for name, value in {'PENNIX_PASEO_RELAY':'y', 'PENNIX_PASEO_RELAY_REGISTER':'y',
                'PENNIX_PASEO_RELAY_ENDPOINT':'relay.example:443', 'PENNIX_PASEO_RELAY_PUBLIC_ENDPOINT':'relay.example:443'}.items():
            kconf.syms[name].set_value(value)
        with patch.object(pennix.relay_owner, 'local_owned', return_value=False), patch.object(pennix.relay_owner, 'inspect') as remote:
            relay = pennix._relay_plan(SimpleNamespace(), kconf)
            self.assertEqual(relay['state'], 'pending-identity')
            remote.assert_not_called()
            value = {'components': {'paseo-cli': {'selected': True, 'action': 'install'},
                'paseo-relay': relay, 'paseo-autostart': {'selected': False, 'action': 'keep', 'listen':'127.0.0.1:6767',
                    'runtime':{}, 'unit_state':'absent'}}, 'connections':{}}
            steps = pennix._apply_steps(value, SimpleNamespace(component_lock={'components':{}}), {'components':{}})
            ids = [s['id'] for s in steps]
            self.assertLess(ids.index('paseo-cli:install'), ids.index('paseo-config:configure'))
            self.assertLess(ids.index('paseo-config:configure'), ids.index('paseo-relay:install'))
            self.assertFalse((self.root/'.paseo/server-id').exists())
            (self.root/'.paseo').mkdir()
            (self.root/'.paseo/old-identity-data').write_text('preserve')
            remote.side_effect = pennix.relay_owner.RelayError('missing identity in existing home')
            self.assertEqual(pennix._relay_plan(SimpleNamespace(), kconf)['state'], 'blocked')
            (self.root/'.paseo/server-id').symlink_to(self.root/'nonexistent')
            remote.side_effect = pennix.relay_owner.RelayError('unsafe identity')
            self.assertEqual(pennix._relay_plan(SimpleNamespace(), kconf)['state'], 'blocked')

    def test_fresh_relay_install_reaches_native_owner_then_service(self):
        config_model.init()
        kconf, config = config_model.load()
        args = SimpleNamespace(json=True, kconf=kconf, codex_home=self.root/'codex',
            config_path=config, component_lock_path=self.root/'components.lock.json',
            component_lock={'schema':1, 'components':{}})
        service = {'selected':True, 'action':'install', 'unit_state':'absent',
            'runtime':{'state':'current', 'running':False}, 'mode':'login',
            'run':False, 'listen':'127.0.0.1:6767'}
        value = {'config':str(config), 'connections':{}, 'components':{
            'paseo-relay':{'selected':True, 'state':'absent', 'action':'install', 'register':True},
            'paseo-autostart':service}}
        calls = []
        with ExitStack() as stack:
            stack.enter_context(patch.object(pennix, '_plan', return_value=value))
            stack.enter_context(patch.object(pennix, '_load', return_value=(kconf,config,{'components':{}})))
            stack.enter_context(patch.object(pennix, '_emit'))
            stack.enter_context(patch.object(pennix.paseo_service, 'preflight'))
            stack.enter_context(patch.object(pennix.paseo_service, 'runtime', return_value={'state':'current'}))
            stack.enter_context(patch.object(pennix.paseo_service, 'observe', return_value={'state':'current'}))
            stack.enter_context(patch.object(pennix.paseo_service, 'apply',
                side_effect=lambda *a,**kw: calls.append('service') or 'changed'))
            stack.enter_context(patch.object(pennix.relay_owner, 'ensure',
                side_effect=lambda *a,**kw: calls.append('register') or 'added'))
            stack.enter_context(patch.object(pennix.relay_owner, 'configure_daemon',
                side_effect=lambda *a,**kw: calls.append('relay-config') or 'changed'))
            stack.enter_context(patch.object(pennix.path_env, 'apply', return_value='no-op'))
            package = stack.enter_context(patch.object(pennix, '_run_component'))
            self.assertEqual(pennix.cmd_apply(args), 0)
            self.assertIsNone(apply_journal.read())
            unknown = {'id':'unknown:install', 'component':'unknown', 'action':'install', 'target':{}}
            with patch.object(pennix, '_apply_steps', return_value=[unknown]):
                with self.assertRaisesRegex(pennix.CliError, 'journal 步骤没有有效执行路由'):
                    pennix.cmd_apply(args)
            self.assertEqual(apply_journal.read()['steps'][0]['status'], 'pending')
        self.assertEqual(calls, ['register','relay-config','service'])
        package.assert_not_called()

    def test_relay_partial_install_recovery_requires_same_owned_registration_and_local_proof(self):
        config_model.init()
        kconf, config = config_model.load()
        for key,value in {'PENNIX_COMPONENT_PASEO_CLI':'y','PENNIX_PASEO_RELAY':'y',
                'PENNIX_PASEO_RELAY_REGISTER':'y','PENNIX_PASEO_RELAY_ENDPOINT':'relay.test:443',
                'PENNIX_PASEO_RELAY_PUBLIC_ENDPOINT':'public.test:443'}.items():
            kconf.syms[key].set_value(value)
        config_model.write(kconf, config)
        home = Path(config_model.config_value(kconf, 'PENNIX_CODEX_HOME'))
        owned_files.bind_target(home, register=True)
        os.environ['CODEX_HOME'] = str(home)
        lock = self.root/'components.lock.json'
        step = {'id':'paseo-relay:install','component':'paseo-relay','action':'install','target':{}}
        active = apply_journal.start('apply',None,apply_journal.digest(config),None,[step])
        active = apply_journal.unknown(active,step['id'])
        args = SimpleNamespace(resolve='completed',transaction=active['transaction'],step=step['id'],
            component_lock_path=lock,json=True)
        with ExitStack() as stack:
            stack.enter_context(patch.object(pennix,'_load',return_value=(kconf,config,{'components':{}})))
            stack.enter_context(patch.object(pennix,'_emit'))
            stack.enter_context(patch.object(pennix.relay_owner,'_server_id',return_value='new-device'))
            record = stack.enter_context(patch.object(pennix.relay_owner,'_record'))
            remote = stack.enter_context(patch.object(pennix.relay_owner,'inspect',return_value='present'))
            configure = stack.enter_context(patch.object(pennix.relay_owner,'configure_daemon'))
            owned = stack.enter_context(patch.object(pennix.relay_owner,'daemon_owned',return_value=False))
            stack.enter_context(patch.object(pennix.relay_owner,'inspect_daemon',return_value='current'))
            for candidate,remote_state in [(None,'present'),({'server_id':'other-device'},'present'),
                    ({'server_id':'new-device'},'absent')]:
                record.return_value=candidate;remote.return_value=remote_state
                with self.assertRaisesRegex(pennix.CliError,'同身份归属未证明'):
                    pennix.cmd_recover(args)
                self.assertEqual(apply_journal.read()['steps'][0]['status'],'unknown')
                configure.assert_not_called()
            record.return_value={'server_id':'new-device'};remote.return_value='present'
            with self.assertRaisesRegex(pennix.CliError,'本地配置归属或后置条件未证明'):
                pennix.cmd_recover(args)
            self.assertEqual(apply_journal.read()['steps'][0]['status'],'unknown')
            owned.return_value=True
            self.assertEqual(pennix.cmd_recover(args),0)
            self.assertIsNone(apply_journal.read())

    def test_arch_discovery_uses_only_temporary_db_and_detects_real_changes(self):
        real = self.root/'db'
        (real/'sync').mkdir(parents=True)
        (real/'local').mkdir()
        db = real/'sync/core.db'
        db.write_bytes(b'original')
        calls = []
        def run(command, **_):
            calls.append(command)
            text = ''
            if command[0] == 'pacman-conf': text = str(real)+'\n'
            elif command[1] == '-Q': text = 'package 1\n'
            elif command[0] == 'fakeroot':
                temporary = Path(command[command.index('--dbpath')+1])
                self.assertNotEqual(temporary, real)
                self.assertEqual((temporary/'local').resolve(), real/'local')
            elif command[1] == '-Sl': text = 'core package 2 [installed: 1]\n'
            elif command[1] == '-Qu': text = 'package 1 -> 2\n'
            return subprocess.CompletedProcess(command, 0, text, '')
        with patch.object(inventory, 'run', side_effect=run):
            result = inventory.arch()
        self.assertEqual(result['state'], 'current')
        self.assertEqual(result['upgrades'], [{'name':'package','installed':'1','available':'2'}])
        self.assertEqual(db.read_bytes(), b'original')
        self.assertTrue(all('--dbpath' in c for c in calls if '-Sy' in c))
        with patch.object(inventory, 'installed_arch', return_value={'package':'2', 'extra':'new'}), self.assertRaises(inventory.UpdateError):
            inventory.verify_system({'package':'1'}, result['upgrades'])
        with patch.object(inventory, 'installed_arch', return_value={'package':'2', 'extra':'new'}):
            inventory.verify_system({'package':'1'}, result['upgrades'], accepted={'package':'2', 'extra':'new'})

    def test_inventory_fixed_source_unknown_and_actual_lock_are_separate(self):
        component = {'id':'collection', 'delivery':'collection', 'lock_source':'git:owner/collection',
            'collection_contract':{'source':{'repo':'owner/collection','ref':'a'*40}}}
        with patch.object(candidates, '_git_head') as head:
            self.assertEqual(inventory.available(component, {}, {}), {'source':'git:owner/collection','ref':'a'*40})
            head.assert_not_called()
        plan = {'components':{'collection':{'selected':True,'state':'current'}}, 'connections':{}}
        with patch.object(inventory, 'arch', return_value={'state':'current','upgrades':[]}), \
                patch.object(inventory, 'available', side_effect=OSError('private-fixture')):
            result = inventory.inspect({'components':{'collection':component}}, {'collection'}, {}, plan, system_upgrade=False)
        self.assertEqual(result['components']['collection']['state'], 'unknown')
        self.assertNotIn('private-fixture', json.dumps(result))
        private = {'id':'cch-status', 'delivery':'package', 'lock_source':'github-release:owner/repo',
            'package':{'source':'npm','name':'private-package', 'asset':{'repo':'owner/repo','name_prefix':'private-', 'suffix':'.tgz'}}}
        with patch.object(candidates,'_gh_release',return_value={'tagName':'v1.0.0','assets':[{'name':'private-1.0.0.tgz'}]}), \
                patch.object(candidates,'_json') as public_registry:
            self.assertEqual(inventory.available(private,{},{}), {'source':'github-release:owner/repo','version':'1.0.0','ref':'v1.0.0'})
            public_registry.assert_not_called()

    def test_unified_transaction_resumes_partial_success_and_keeps_frozen_targets(self):
        config, lock = self.root/'workflow.config', self.root/'components.lock.json'
        config.write_text('fixture config')
        names = ['trellis-cli', 'codegraph-cli']
        components = {key:{'id':key,'delivery':'package','lock_source':'npm:'+key,
            'package':{'source':'npm','name':key}} for key in names}
        sources = {key:spec['lock_source'] for key,spec in components.items()}
        old = {key:{'source':sources[key],'version':'1','sha256':'a'*64} for key in names}
        targets = {key:{**old[key],'version':'2','sha256':'b'*64} for key in names}
        component_lock.write(lock, {'schema':1,'components':old}, sources)
        review = {'targets':targets,'available':targets, 'system':{'state':'current','installed':{},'upgrades':[]},
                  'catalog_digest':apply_journal.digest(pennix.CATALOG), 'secrets_digest':None}
        args = pennix.parser().parse_args(['update','--all','--json'])
        args.update_review, args.update_targets, args.update_inventory = review, targets, {}
        args.kconf = SimpleNamespace()
        args.kconf.syms = {}
        args.codex_home = self.root/'codex'
        args.config_path, args.component_lock_path = config, lock
        args.config_snapshot = config.read_bytes()
        args.component_lock_sources = sources
        catalog = {'components':components, 'configuration_targets':{}}
        def load(_):
            args.component_lock = component_lock.read(lock, sources)
            args.component_lock_snapshot = lock.read_bytes()
            return args.kconf, config, catalog
        def plan(_):
            load(args)
            return {'config':str(config),'connections':{}, 'side_effects':{}, 'components':{
                **{key:{'selected':True,'state':'current','action':'upgrade','candidate_target':targets[key]} for key in names},
                'paseo-autostart':{'selected':False,'action':'keep','unit_state':'absent','mode':'off','run':False,
                    'listen':'127.0.0.1:6767','runtime':{'state':'current','running':False}}}}
        calls = []
        def install(_args, _catalog, key, _operation, **_):
            calls.append(key)
            if calls == names:
                raise connection_owners.ConnectionError('fixture before effect', effect='none')
            return 'changed'
        def resolve(component, workspace, target):
            self.assertEqual(target, targets[component['id']])
            return {**target, 'path':str(workspace/'fixture')}
        with ExitStack() as stack:
            for obj,name,value in [(pennix,'_plan',plan),(pennix,'_load',load),
                (pennix,'_run_component',install),(candidates,'resolve',resolve)]:
                stack.enter_context(patch.object(obj,name,side_effect=value))
            stack.enter_context(patch.object(config_model,'config_value',side_effect=lambda _,key:
                str(self.root/'secrets') if key == 'PENNIX_SECRETS_FILE' else str(self.root/'paseo') if key == 'PENNIX_PASEO_HOME' else 'n'))
            stack.enter_context(patch.object(pennix,'_verify_update_candidates'))
            stack.enter_context(patch.object(pennix,'_emit'))
            stack.enter_context(patch.object(pennix.path_env,'apply',return_value='no-op'))
            stack.enter_context(patch.object(sys.stdin,'isatty',return_value=True))
            stack.enter_context(patch.object(sys.stdout,'isatty',return_value=True))
            stack.enter_context(patch('builtins.input',return_value='yes'))
            with self.assertRaises(connection_owners.ConnectionError): pennix.cmd_apply(args)
            transaction = apply_journal.read()
            self.assertEqual(transaction['command'], 'update-all')
            self.assertEqual(transaction['steps'][1]['status'], 'verified')
            self.assertEqual(transaction['steps'][2]['status'], 'pending')
            self.assertEqual(pennix.cmd_update_all(args), 0)
        self.assertEqual(calls, ['trellis-cli','codegraph-cli','codegraph-cli'])
        self.assertEqual(component_lock.read(lock, sources)['components'], targets)
        self.assertIsNone(apply_journal.read())

    def test_unified_no_tty_and_candidate_changes_do_not_start_transaction(self):
        args = pennix.parser().parse_args(['update','--all','--json'])
        report = {'system':{'state':'current','installed':{},'upgrades':[]}, 'components':{}}
        with patch.object(pennix,'_updates',return_value=(report,{},{})), patch.object(pennix,'_emit'), \
                patch.object(sys.stdin,'isatty',return_value=False), self.assertRaises(pennix.CliError):
            pennix.cmd_update_all(args)
        self.assertIsNone(apply_journal.read())
        args.update_review = {'system':report['system'], 'available':{'trellis-cli':{'version':'2'}}}
        args.component_lock = {'components':{}}
        with patch.object(inventory,'arch',return_value=report['system']), \
                patch.object(inventory,'available',return_value={'version':'3'}), self.assertRaises(pennix.CliError):
            pennix._verify_update_candidates(args, {'components':{'trellis-cli':{}}})
        self.assertIsNone(apply_journal.read())

    def test_self_update_rejects_checksum_and_changed_release_before_install(self):
        target = {'version':'9.0.0','tag':'v9.0.0','name':'pennix-lifecycle-9.0.0.tar.gz',
                  'url':'https://github.com/PennixRv/pennix-lifecycle/releases/download/v9.0.0/pennix-lifecycle-9.0.0.tar.gz',
                  'digest':'sha256:'+'a'*64}
        def download(url, path, **_):
            path.write_bytes(b'wrong bytes' if not url.endswith('.sha256') else b'a'*64 + b'  '+target['name'].encode())
        with patch.object(owned_files,'download',side_effect=download), patch.object(release_owner,'_install') as install:
            with self.assertRaises(release_owner.ReleaseError): release_owner.update(target)
            install.assert_not_called()
        args = pennix.parser().parse_args(['tool','update','--json'])
        with patch.object(release_owner,'latest',side_effect=[target,{**target,'version':'9.0.1'}]), \
                patch.object(sys.stdin,'isatty',return_value=True), patch.object(sys.stdout,'isatty',return_value=True), \
                patch('builtins.input',return_value='yes'), patch.object(pennix,'_emit'), \
                patch.object(release_owner,'update') as update:
            with self.assertRaises(pennix.CliError): pennix.cmd_tool(args)
            update.assert_not_called()

    def test_system_rereview_cannot_release_unknown_effects(self):
        review = {'targets':{}, 'system':{'installed':{},'upgrades':[]}}
        active = apply_journal.start('update-all', None, 'a'*64, None, [
            {'id':'update-review','component':'update-review','action':'review','target':review},
            {'id':'system-upgrade','component':'system','action':'upgrade','target':{}}])
        apply_journal.unknown(active, 'system-upgrade')
        before = apply_journal.path().read_bytes()
        args = pennix.parser().parse_args(['update','--all','--review-system-changes'])
        with self.assertRaisesRegex(pennix.CliError,'不释放 unknown'):
            pennix.cmd_update_all(args)
        self.assertEqual(apply_journal.path().read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
