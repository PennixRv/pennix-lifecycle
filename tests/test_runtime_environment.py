import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import owned_files as files
import release_owner
import runtime_environment as runtime


class RuntimeEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='pennix-runtime-test-');self.home=Path(self.temp.name)
        self.env=patch.dict(os.environ,{'HOME':str(self.home),'XDG_STATE_HOME':str(self.home/'state'),
            'XDG_DATA_HOME':str(self.home/'data'),'UV_PYTHON':'untrusted','UV_INDEX_URL':'https://invalid.test'})
        self.env.start();self.addCleanup(self.temp.cleanup);self.addCleanup(self.env.stop)
        self.source=self.home/'source';self.source.mkdir();(self.source/'VERSION').write_text('1.0.0\n')
        (self.source/'pennix').write_text('#!/bin/sh\nexit 0\n');(self.source/'pennix').chmod(0o700)
        for name in ['scripts','references','config','templates']:(self.source/name).mkdir()
        (self.source/'references/runtime-requirements.txt').write_text('fixed fixture lock\n')
        self.calls=[]
        def execute(command,**kwargs):
            self.calls.append((command,kwargs))
            if '-m' in command and 'venv' in command:
                target=Path(command[-1]);(target/'bin').mkdir(parents=True)
                (target/'bin/python').write_text('fixture interpreter '+str(len(self.calls)))
            return SimpleNamespace(returncode=0)
        self.run=patch.object(runtime.subprocess,'run',side_effect=execute);self.run.start();self.addCleanup(self.run.stop)

    def test_private_runtime_pins_python_hashes_and_ignores_external_uv_configuration(self):
        runtime.prepare(self.source)
        self.assertTrue(runtime.verify(self.source))
        commands=[c for c,k in self.calls]
        self.assertIn('--copies',commands[0]);self.assertIn('--without-pip',commands[0])
        self.assertIn('--require-hashes',commands[1]);self.assertIn('--no-python-downloads',commands[1])
        self.assertIn('https://pypi.org/simple',commands[1])
        for _,kwargs in self.calls:
            self.assertNotIn('UV_INDEX_URL',kwargs['env']);self.assertNotIn('UV_PYTHON',kwargs['env'])
        count=len(self.calls);runtime.prepare(self.source);self.assertEqual(len(self.calls),count)
        (self.source/'.runtime/bin/python').write_text('changed')
        with self.assertRaises(runtime.RuntimeError):runtime.verify(self.source)

    def test_reentry_receipt_failure_restores_previous_runtime_and_active_identity(self):
        release_owner.install(self.source)
        current=(self.home/'.local/bin/pennix').resolve().parent
        manifest=current/runtime.MANIFEST
        record=files.read_json(manifest);record['python']='0'*64
        files.replace(manifest,files.json_bytes(record),files.read(manifest,private=True))
        receipt=release_owner._receipt_path()
        value=files.read_json(receipt);value['digest']=release_owner._installed_digest(current)
        files.replace(receipt,files.json_bytes(value),files.read(receipt,private=True))
        old=release_owner._installed_digest(current)
        original=files.replace
        def fail(path,*args,**kwargs):
            if path==receipt:raise OSError('injected receipt failure')
            return original(path,*args,**kwargs)
        with patch.object(files,'replace',side_effect=fail):
            with self.assertRaisesRegex(OSError,'injected'):release_owner.install(self.source)
        self.assertEqual(release_owner._installed_digest(current),old)
        self.assertEqual(release_owner._active_target(self.home/'.local/bin/pennix',current.parent),current)


if __name__=='__main__':unittest.main()
