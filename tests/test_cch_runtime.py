"""CCH native runtime and Lifecycle keep independent configuration and frozen effects."""
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
import component_lock
import config_model
import connection_owners
import owned_files
import pennix


class CchRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        env = patch.dict(os.environ, {'HOME':str(self.root), 'CODEX_HOME':str(self.root/'.codex'),
            **{'XDG_'+name+'_HOME':str(self.root/name.lower()) for name in ['CONFIG','DATA','STATE','CACHE']}})
        env.start()
        self.addCleanup(env.stop)
        config_model.init()
        self.kconf, self.config = config_model.load()
        self.catalog = pennix.owner_engine.load_catalog(pennix.CATALOG)
        self.spec = self.catalog['configuration_targets']['cch-tmux']
        for name in ['PENNIX_COMPONENT_CCH_STATUS','PENNIX_TARGET_CCH','PENNIX_TARGET_CCH_TMUX']:
            self.kconf.syms[name].set_value('y')
        config_model.write(self.kconf, self.config)
        self.kconf, _ = config_model.load()

    def test_choice_is_explicit_and_cch_connection_precedes_runtime_with_frozen_fingerprint(self):
        config_model.init(str(self.root/'default.config'))
        default, _ = config_model.load(str(self.root/'default.config'))
        self.assertEqual(default.syms['PENNIX_TARGET_CCH_TMUX'].str_value, 'n')
        value = {'components':{'cch-status':{'selected':True,'action':'install'}, 'tmux':{'selected':True,'action':'install'},
            'paseo-autostart':{'selected':False,'action':'keep','unit_state':'absent','runtime':{}}},
            'connections':{'cch':{'selected':True,'action':'configure'},
                'cch-tmux':{'selected':True,'action':'configure','fingerprint':'a'*64}}}
        args = SimpleNamespace(component_lock={'components':{}})
        steps = pennix._apply_steps(value,args,self.catalog)
        ids = [s['id'] for s in steps]
        self.assertLess(ids.index('tmux:install'), ids.index('connection:cch-tmux:configure'))
        self.assertLess(ids.index('connection:cch:configure'), ids.index('connection:cch-tmux:configure'))
        self.assertEqual(next(s['target'] for s in steps if s['id']=='connection:cch-tmux:configure'), {'expected_sha256':'a'*64})
        with self.assertRaises(pennix.CliError):
            pennix._ensure_connection_dependencies({'cch-status'},value['connections'],self.catalog)

    def test_runtime_payload_requires_frozen_evidence_and_keeps_owner_subcommand(self):
        seen = []
        def run(command, action, payload, **_):
            seen.append((command, action, payload))
            return {'state':'current','owned':True,'effect':'none' if action=='validate' else 'changed'}
        with patch.object(connection_owners,'_command',return_value=['native-cch','runtime']), \
                patch.object(connection_owners,'_run',side_effect=run):
            with self.assertRaises(connection_owners.ConnectionError) as error:
                connection_owners.apply_target('cch-tmux',self.spec,self.kconf,self.root/'.codex',self.root/'secrets','configure')
            self.assertEqual(error.exception.effect,'none')
            self.assertFalse(seen)
            connection_owners.apply_target('cch-tmux',self.spec,self.kconf,self.root/'.codex',self.root/'secrets','configure',
                operation_id='operation:step',expected_sha256='b'*64)
        self.assertEqual([action for _,action,_ in seen], ['validate','configure'])
        for command,_,payload in seen:
            self.assertEqual(command, ['native-cch','runtime'])
            self.assertEqual(payload['expected_sha256'],'b'*64)
            self.assertEqual(payload['operation_id'],'operation:step')
            self.assertEqual(payload['secrets'],{})

    def test_pending_native_package_preflight_freezes_runtime_and_preserves_runtime_argument(self):
        steps = [{'id':'cch-status:install','component':'cch-status','action':'install','target':{}},
            {'id':'connection:cch-tmux:configure','component':'connection:cch-tmux','action':'configure','target':{}}]
        args = SimpleNamespace(kconf=self.kconf,codex_home=self.root/'.codex')
        candidate = {'source':self.catalog['components']['cch-status']['lock_source'],'version':'0.1.52','sha256':'b'*64,'path':'/fixture.tgz'}
        with patch.object(pennix.candidates,'resolve',return_value=candidate), \
                patch.object(pennix.subprocess,'run',return_value=SimpleNamespace(returncode=0)), \
                patch.object(connection_owners,'_run',return_value={'state':'missing','owned':False,'effect':'none','fingerprint':'c'*64}) as native:
            pennix._preflight_pending_owners(args,{'connections':{'cch-tmux':{'state':'pending-owner'}}},self.catalog,steps)
        self.assertEqual(native.call_args.args[0][-1], 'runtime')
        self.assertEqual(native.call_args.args[2]['target'],'cch-tmux')
        self.assertEqual(steps[1]['target'],{'expected_sha256':'c'*64})

    def test_package_refresh_uses_journal_fingerprint_after_upgrade(self):
        args = SimpleNamespace(kconf=self.kconf,component='cch-status',codex_home=self.root/'.codex')
        active = apply_journal.start('update','cch-status','a'*64,None,[
            {'id':'cch-status:upgrade','component':'cch-status','action':'upgrade','target':{}},
            {'id':'connection:cch-tmux:configure','component':'connection:cch-tmux','action':'configure','target':{'expected_sha256':'a'*64}}])
        active = apply_journal.verified(active,'cch-status:upgrade')
        with patch.object(connection_owners,'inspect_target',side_effect=AssertionError('do not rebuild frozen steps')), \
                patch.object(connection_owners,'apply_target',return_value='changed') as apply:
            result = pennix._configure_updated_connections(args,self.catalog,active)
        self.assertEqual(apply.call_args.kwargs['expected_sha256'],'a'*64)
        self.assertEqual(apply.call_args.kwargs['operation_id'],f"{active['transaction']}:connection:cch-tmux:configure")
        self.assertTrue(apply_journal.complete(result))

    def test_native_fingerprint_is_required_and_redacted_at_read_boundary(self):
        for fingerprint in [None,'synthetic-private-value']:
            output = {'state':'current','owned':True}
            if fingerprint is not None:output['fingerprint']=fingerprint
            with patch.object(connection_owners.subprocess,'run',return_value=subprocess.CompletedProcess([],0,json.dumps(output),'')), \
                    self.assertRaises(connection_owners.ConnectionError) as error:
                connection_owners._run(['cch','runtime'],'inspect',{'target':'cch-tmux'})
            self.assertNotIn('synthetic-private-value',str(error.exception))

    def test_recover_requires_native_same_operation_completion_before_releasing_unknown(self):
        lock = component_lock.path_for(self.config)
        sources = {'cch-status':self.catalog['components']['cch-status']['lock_source']}
        component_lock.write(lock,{'schema':1,'components':{}},sources)
        active = apply_journal.start('apply',None,apply_journal.digest(self.config),apply_journal.digest(lock),[
            {'id':'connection:cch-tmux:configure','component':'connection:cch-tmux','action':'configure','target':{'expected_sha256':'a'*64}}])
        apply_journal.unknown(active,'connection:cch-tmux:configure')
        args = pennix.parser().parse_args(['recover','--transaction',active['transaction'],'--step','connection:cch-tmux:configure','--resolve','completed','--json'])
        before = apply_journal.path().read_bytes()
        with patch.object(connection_owners,'_command',return_value=['cch','runtime']), \
                patch.object(connection_owners,'_run',side_effect=connection_owners.ConnectionError('wrong operation',effect='none')):
            with self.assertRaises(connection_owners.ConnectionError):pennix.cmd_recover(args)
        self.assertEqual(apply_journal.path().read_bytes(),before)
        with patch.object(connection_owners,'_command',return_value=['cch','runtime']), \
                patch.object(connection_owners,'_run',return_value={'state':'current','owned':True,'effect':'changed'}) as native, \
                patch.object(connection_owners,'inspect_target',return_value={'state':'current','owned':True}),patch.object(pennix,'_emit'):
            self.assertEqual(pennix.cmd_recover(args),0)
        self.assertEqual(native.call_args.args[1],'complete')
        self.assertEqual(native.call_args.args[2]['operation_id'],f"{active['transaction']}:connection:cch-tmux:configure")
        self.assertIsNone(apply_journal.read())


if __name__ == '__main__':
    unittest.main()
