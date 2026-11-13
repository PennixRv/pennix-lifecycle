import hashlib
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import _engine as engine
import owned_files as files
import pnpm_migration as migration


class PnpmMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='pennix-pnpm-test-');self.home=Path(self.temp.name)
        self.env=patch.dict(os.environ,{'HOME':str(self.home),'CODEX_HOME':str(self.home/'codex'),
            'XDG_STATE_HOME':str(self.home/'state')});self.env.start()
        self.addCleanup(self.temp.cleanup);self.addCleanup(self.env.stop)
        native=patch.object(migration.subprocess,'run',return_value=SimpleNamespace(returncode=1,stdout=''))
        native.start();self.addCleanup(native.stop)
        self.old={'source':migration.LEGACY_SOURCE,'version':'10.32.1'}
        self.new={'source':'arch:extra/pnpm','version':'12.19.0-1'}
        self.receipt={'schema':1,'component':'pnpm','source':'npm','name':'pnpm','version':'10.32.1','target':files.target_identity()}
        files.replace(engine._package_receipt_path('pnpm'),files.json_bytes(self.receipt),None)
        self.archive=self.home/'candidate.tgz';self.archive.write_bytes(b'verified npm fixture')

    def test_unknown_or_mismatched_old_identity_stops_before_exit(self):
        with patch.object(engine,'package_identity',return_value={'version':'10.99'}):
            with self.assertRaisesRegex(engine.BootstrapError,'不一致'):migration.inspect(self.old)
        with self.assertRaisesRegex(engine.BootstrapError,'旧锁'):migration.inspect(self.new)

    def test_failure_after_old_owner_exit_retains_verified_backup_and_recovers(self):
        component={'package':{'source':'official','name':'pnpm'}}
        args=SimpleNamespace(component_lock={'components':{'pnpm':self.old}})
        absent=[False];failed=[False];events=[]
        def operation(args,catalog,key,spec,action):
            events.append((spec['package']['source'],action))
            path=engine._package_receipt_path('pnpm')
            if action=='uninstall':
                absent[0]=True;files.replace(path,None,files.read(path,private=True));return 'changed'
            if not failed[0]:failed[0]=True;raise RuntimeError('injected install failure')
            new={**self.receipt,'source':'official','version':self.new['version']}
            files.replace(path,files.json_bytes(new),None);return 'changed'
        with patch.object(engine,'package_identity',return_value={'version':'10.32.1'}),\
             patch.object(engine,'package_absent',side_effect=lambda c:absent[0]),\
             patch.object(engine,'component_operation',side_effect=operation),\
             patch.object(engine,'recover_npm_receipt'),\
             patch.object(migration.candidates,'resolve_npm',return_value={'path':str(self.archive)}),\
             patch.object(migration.candidates,'lock_entry',return_value=self.new):
            with self.assertRaisesRegex(RuntimeError,'injected'):migration.apply(args,{},component,{})
            self.assertTrue(migration.needed())
            self.assertEqual(migration.pending()['archive_sha256'],hashlib.sha256(self.archive.read_bytes()).hexdigest())
            migration.apply(args,{},component,{})
            with patch.object(engine,'package_owned',return_value=True):migration.finish(component,self.new)
        self.assertEqual(events,[('npm','uninstall'),('official','install'),('official','install')])
        self.assertIsNone(migration.pending());self.assertFalse(migration.directory().exists())

    def test_changed_backup_cannot_release_unknown(self):
        content=b'backup';files.replace(migration.directory()/'package.tgz',content,None)
        record={'schema':1,'target':files.target_identity(),'old_lock':self.old,'old_receipt':self.receipt,
            'archive_sha256':hashlib.sha256(b'other').hexdigest(),'new_target':self.new}
        files.replace(migration.directory()/'record.json',files.json_bytes(record),None)
        with self.assertRaisesRegex(engine.BootstrapError,'恢复材料'):migration.inspect(self.old)

    def test_restore_uses_only_verified_archive_and_preserves_unknown_system_owner(self):
        content=b'verified old archive';files.replace(migration.directory()/'package.tgz',content,None)
        record={'schema':1,'target':files.target_identity(),'old_lock':self.old,'old_receipt':self.receipt,
            'archive_sha256':hashlib.sha256(content).hexdigest(),'new_target':self.new}
        files.replace(migration.directory()/'record.json',files.json_bytes(record),None)
        args=SimpleNamespace(component_lock={'components':{'pnpm':self.new}})
        component={'package':{'source':'official','name':'pnpm'}}
        with patch.object(engine,'package_absent',return_value=False), patch.object(engine,'package_owned',return_value=False):
            with self.assertRaisesRegex(engine.BootstrapError,'不属于本次'):migration.restore(args,{},component)
        with patch.object(engine,'package_absent',return_value=True), patch.object(engine,'package_owned',return_value=True),\
             patch.object(engine,'package_identity',return_value={'version':'10.32.1'}),\
             patch.object(engine,'component_operation',return_value='changed') as operation:
            self.assertEqual(migration.restore(args,{},component),self.old)
            self.assertEqual(operation.call_args.args[0].candidate['path'],str(migration.directory()/'package.tgz'))
            self.assertEqual(operation.call_args.args[3]['_lock_target'],self.old)
        migration.finish_restore(self.old)
        self.assertFalse(migration.directory().exists())


if __name__=='__main__':unittest.main()
