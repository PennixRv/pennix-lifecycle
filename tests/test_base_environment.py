"""Isolated base lifecycle: no host package, account, daemon or shell mutation."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import base_environment as base
import config_model
import _engine as engine
import owned_files as files
import path_env
import pennix


class BaseEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='pennix-base-test-')
        self.home = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"HOME": str(self.home), "CODEX_HOME": str(self.home/'codex'),
            "XDG_CONFIG_HOME": str(self.home/'config'), "XDG_STATE_HOME": str(self.home/'state'),
            "XDG_DATA_HOME": str(self.home/'data'), "XDG_CACHE_HOME": str(self.home/'cache')})
        self.env.start()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.env.stop)
        config_model.init(profile='development', git_name='Base Test', git_email='base@example.invalid')
        self.kconf, self.config = config_model.load()
        self.catalog = engine.load_catalog(base.ROOT/'references/catalog.json')
        self.native = {"user": "test", "shell": "/usr/bin/zsh", "docker_group": True,
            "docker_enabled": True, "docker_active": True, "locales": ["zh_cn.utf8", "en_us.utf8"],
            "npm_prefix": str(self.home/'.local'), "node24": True}
        for name, value in [('package_plan', {"official": [], "aur": [], "versions": {}, "node_replacement": False}),
                            ('native_state', self.native), ('_native_apply', None), ('_install_packages', None)]:
            p = patch.object(base, name, return_value=value)
            p.start();self.addCleanup(p.stop)

    def test_profile_is_base_only_on_new_machine_and_preserves_existing_selections(self):
        selected = config_model.selected_components(self.kconf)
        self.assertEqual(selected, base.SCOPE)
        self.kconf.syms['PENNIX_COMPONENT_TRELLIS_CLI'].set_value('y')
        config_model.write(self.kconf,self.config)
        config_model.init(profile='development')
        self.assertIn('trellis-cli',config_model.selected_components(config_model.load()[0]))
        with self.assertRaises(config_model.ConfigError):
            config_model.init(profile='development',force=True)

    def test_old_configuration_without_new_optional_fields_loads(self):
        self.kconf.syms['PENNIX_COMPONENT_BASE_ENVIRONMENT'].set_value('n')
        config_model.write(self.kconf,self.config)
        before = self.config.read_text()
        self.config.write_text('\n'.join(l for l in before.splitlines()
            if 'PENNIX_BASE_' not in l and 'PENNIX_COMPONENT_BASE_ENVIRONMENT' not in l)+'\n')
        self.assertNotIn('base-environment',config_model.selected_components(config_model.load()[0]))

    def test_apply_repair_uninstall_roundtrip_preserves_external_data(self):
        original = b'# user preference\nalias custom=true\n'
        (self.home/'.bashrc').write_bytes(original)
        self.assertEqual(base.inspect(self.kconf)['state'],'missing')
        base.apply(self.kconf,self.catalog)
        self.assertEqual(base.inspect(self.kconf)['state'],'current')
        self.assertIn(original,(self.home/'.bashrc').read_bytes())
        env = self.home/'config/pennix/base/env.sh'
        env.unlink()
        self.assertEqual(base.inspect(self.kconf)['state'],'missing')
        base.apply(self.kconf,self.catalog)
        self.assertEqual(base.inspect(self.kconf)['state'],'current')
        with patch.object(base.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout='workload\n')):
            base.uninstall()
        self.assertEqual((self.home/'.bashrc').read_bytes(),original)
        self.assertFalse((self.home/'.p10k.zsh').exists())
        self.assertIsNone(base.receipt())

    def test_external_template_needs_exact_adoption_and_later_drift_is_protected(self):
        target = self.home/'.p10k.zsh';target.write_text('# personal\n')
        self.assertEqual(base.inspect(self.kconf)['state'],'blocked')
        self.kconf.syms['PENNIX_BASE_ADOPT_FILES'].set_value(json.dumps({str(target):hashlib.sha256(target.read_bytes()).hexdigest()}))
        base.apply(self.kconf,self.catalog)
        target.write_text('user changed\n')
        self.assertEqual(base.inspect(self.kconf)['state'],'blocked')
        with self.assertRaises(base.BaseError):base.uninstall()
        self.assertEqual(target.read_text(),'user changed\n')

    def test_failed_file_write_keeps_receipt_and_converges_without_reowning(self):
        original = files.replace
        def fail(path,*args,**kwargs):
            if path == self.home/'.p10k.zsh':raise OSError('injected')
            return original(path,*args,**kwargs)
        with patch.object(files,'replace',side_effect=fail):
            with self.assertRaisesRegex(OSError,'injected'):base.apply(self.kconf,self.catalog)
        self.assertIsNotNone(base.receipt())
        base.apply(self.kconf,self.catalog)
        self.assertEqual(base.inspect(self.kconf)['state'],'current')

    def test_symlink_never_adopted_even_with_hash(self):
        target = self.home/'external';target.write_text('keep')
        (self.home/'.p10k.zsh').symlink_to(target)
        self.assertEqual(base.inspect(self.kconf)['state'],'blocked')
        self.assertEqual(target.read_text(),'keep')

    def test_no_tty_shell_is_quiet_and_path_preserves_project_and_deduplicates(self):
        base.apply(self.kconf,self.catalog)
        project = self.home/'project/.venv';(project/'bin').mkdir(parents=True)
        env = {**os.environ,'VIRTUAL_ENV':str(project),'PATH':f'/usr/bin::{self.home}/.local/bin:/mnt/c/Windows:{self.home}/.npm-global/bin:/usr/bin'}
        for shell in ['bash','zsh']:
            p = subprocess.run([shell,'-c','. "$XDG_CONFIG_HOME/pennix/base/env.sh"; . "$XDG_CONFIG_HOME/pennix/base/env.sh"; printf "%s" "$PATH"'],env=env,capture_output=True,text=True)
            self.assertEqual(p.returncode,0,p.stderr)
            parts=p.stdout.split(':');self.assertEqual(parts[0],str(project/'bin'));self.assertEqual(len(parts),len(set(parts)))
            self.assertNotIn('',parts);self.assertNotIn('/mnt/c/Windows',parts)
        p=subprocess.run(['zsh','-ic','true'],env=env,capture_output=True,text=True)
        self.assertEqual(p.returncode,0,p.stderr);self.assertEqual(p.stdout,'')

    def test_scope_excludes_unrelated_components_without_probing_them(self):
        args=pennix.parser().parse_args(['plan','--scope','base'])
        def status(args,key,component):
            self.assertIn(key,base.SCOPE)
            return {'state':'missing'}
        with patch.object(pennix,'_component_status',side_effect=status),patch.object(pennix.pnpm_migration,'needed',return_value=False):
            value=pennix._plan(args)
        self.assertEqual(value['connections'],{})
        for key,item in value['components'].items():
            if key not in base.SCOPE:self.assertEqual(item['action'],'keep')

    def test_known_tmux_upgrade_keeps_cch_and_unknown_changes_block(self):
        path=self.home/'.tmux.conf'
        old=f'{engine.BEGIN_TMUX}\n{engine.LEGACY_TMUX}\n{engine.END_TMUX}\n'
        cch='# CCH external hook\nsource-file /tmp/cch.conf\n'
        path.write_text(cch+old)
        component=self.catalog['components']['tmux-config']
        self.assertEqual(engine._static_state('tmux-config',self.home/'codex',component),'outdated')
        engine._operate_static('tmux-config',self.home/'codex',component,'install')
        self.assertTrue(path.read_text().startswith(cch))
        path.write_text(path.read_text().replace('history-limit 50000','history-limit 99'))
        self.assertEqual(engine._static_state('tmux-config',self.home/'codex',component),'drifted')

    def test_pnpm_catalog_and_candidate_use_official_owner_with_only_legacy_alias(self):
        self.assertEqual(self.catalog['components']['pnpm']['lock_source'],'arch:extra/pnpm')
        self.assertEqual(self.catalog['components']['pnpm']['lock_source_aliases'],['npm:pnpm:latest'])
        self.assertIs(pennix.candidates.RESOLVERS['pnpm'],pennix.candidates.resolve_arch)


if __name__=='__main__':unittest.main()
