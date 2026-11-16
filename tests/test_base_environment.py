"""Isolated base lifecycle: no host package, account, daemon or shell mutation."""
import hashlib
import contextlib
import io
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

install_base_packages = base._install_packages

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

    def test_native_base_failure_uses_cli_error_handler(self):
        errors = io.StringIO()
        with patch.object(sys, 'argv', ['pennix', 'check', '--scope', 'base']), \
             patch.object(pennix, 'cmd_check', side_effect=base.BaseError('native package download failed')), \
             contextlib.redirect_stderr(errors):
            self.assertEqual(pennix.main(), 2)
        self.assertIn('pennix: native package download failed', errors.getvalue())
        self.assertNotIn('Traceback', errors.getvalue())

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

    def test_malformed_aur_receipt_is_rejected_without_type_error(self):
        base.apply(self.kconf, self.catalog)
        record = base.receipt()
        valid = {'ref': 'a' * 40, 'recipe_sha256': 'b' * 64, 'sources': {},
                 'version': '1', 'sha256': 'c' * 64}
        for key in ('ref', 'recipe_sha256', 'sha256'):
            with self.subTest(key=key):
                record['packages'] = {'synthetic': {**valid, key: 12}}
                base._save(record)
                with self.assertRaisesRegex(base.BaseError, 'AUR'):
                    base.receipt()

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

    def test_partial_native_uninstall_resumes_after_prefix_restoration(self):
        base.apply(self.kconf, self.catalog)
        record = base.receipt()
        record['native'].update(npm_prefix='/usr', shell='/bin/bash')
        base._save(record)
        current = {**self.native, 'npm_prefix': str(self.home / '.local')}
        calls = []
        interrupted = True
        def run(command):
            nonlocal interrupted
            calls.append(command)
            if command[0] == 'npm':
                current['npm_prefix'] = '/usr'
            elif command[:2] == ['sudo', 'chsh']:
                if interrupted:
                    interrupted = False
                    raise base.BaseError('native shell restore interrupted')
                current['shell'] = '/bin/bash'
            return SimpleNamespace(returncode=0, stdout='')
        with patch.object(base, 'native_state', side_effect=lambda: dict(current)), \
                patch.object(base, '_run', side_effect=run), \
                patch.object(base.subprocess, 'run', return_value=SimpleNamespace(returncode=1, stdout='')):
            with self.assertRaisesRegex(base.BaseError, 'interrupted'):
                base.uninstall()
            self.assertEqual(base.receipt()['uninstall']['native'], {'npm-prefix': 'verified', 'shell': 'started'})
            self.assertTrue(base.uninstall().startswith('changed;'))
        self.assertEqual(sum(command[0] == 'npm' for command in calls), 1)
        self.assertEqual(current['shell'], '/bin/bash')
        self.assertIsNone(base.receipt())

    def test_symlink_never_adopted_even_with_hash(self):
        target = self.home/'external';target.write_text('keep')
        (self.home/'.p10k.zsh').symlink_to(target)
        self.assertEqual(base.inspect(self.kconf)['state'],'blocked')
        self.assertEqual(target.read_text(),'keep')

    def test_partial_uninstall_resumes_without_treating_restored_files_as_drift(self):
        (self.home / '.bashrc').write_bytes(b'# original user data\n')
        base.apply(self.kconf, self.catalog)
        replace = files.replace
        count = 0
        def fail(path, *args, **kwargs):
            nonlocal count
            if str(path) in base.receipt()['files']:
                count += 1
                if count == 2:
                    raise OSError('second restore interrupted')
            return replace(path, *args, **kwargs)
        with patch.object(files, 'replace', side_effect=fail), \
                patch.object(base.subprocess, 'run', return_value=SimpleNamespace(returncode=1, stdout='')):
            with self.assertRaisesRegex(OSError, 'interrupted'):
                base.uninstall()
        record = base.receipt()
        self.assertEqual(set(record['uninstall']['files'].values()), {'started', 'verified'})
        with self.assertRaisesRegex(base.BaseError, '撤回尚未完成'):
            base.apply(self.kconf, self.catalog)
        restored = next(path for path, state in record['uninstall']['files'].items() if state == 'verified')
        original = files.read(Path(restored))
        Path(restored).parent.mkdir(parents=True, exist_ok=True)
        Path(restored).write_bytes(b'new external edit')
        with self.assertRaisesRegex(base.BaseError, '用户修改'):
            base.uninstall()
        files.replace(Path(restored), original, b'new external edit')
        with patch.object(base.subprocess, 'run', return_value=SimpleNamespace(returncode=1, stdout='')):
            base.uninstall()
        self.assertIsNone(base.receipt())
        self.assertEqual((self.home / '.bashrc').read_bytes(), b'# original user data\n')

    def test_fixed_base_vcs_build_binds_recipe_source_and_native_artifact(self):
        base.apply(self.kconf, self.catalog)
        tree = self.home / 'aur-tree';tree.mkdir()
        recipe = 'pkgname=oh-my-zsh-git\nsource=(git+https://example.test/zsh.git)\n'
        (tree / 'PKGBUILD').write_text(recipe)
        (tree / '.SRCINFO').write_text('pkgver = 1\npkgrel = 1\npkgname = oh-my-zsh-git\n')
        artifact = tree / 'oh-my-zsh-git-2-1-any.pkg.tar.zst';artifact.write_bytes(b'fixed synthetic package')
        target = {'source': 'aur:oh-my-zsh-git', 'version': '1-1', 'ref': 'a'*40,
                  'recipe_sha256': base.candidates._digest(tree / 'PKGBUILD'),
                  'srcinfo_sha256': base.candidates._digest(tree / '.SRCINFO'),
                  'sources': {'source': ['git+https://example.test/zsh.git#commit=' + 'b'*40]}, 'recipe': recipe}
        plan = {'official': [], 'aur': ['oh-my-zsh-git'], 'versions': {'oh-my-zsh-git': '1-1'}, 'aur_candidates': {'oh-my-zsh-git': target}}
        commands = []
        def run(command, **kwargs):
            commands.append(command)
            if command[0] == 'makepkg' and '--printsrcinfo' not in command:
                self.assertIn('--holdver', command)
                self.assertIn('#commit=' + 'b'*40, (tree / 'PKGBUILD').read_text())
                self.assertEqual(kwargs['environment']['PKGDEST'], str(tree))
                return ''
            if command == ['makepkg', '--printsrcinfo']:
                return 'pkgver = 2\npkgrel = 1\n'
            return 'oh-my-zsh-git 2-1'
        with patch.object(base.candidates, 'resolve_aur', return_value={'path': str(tree)}) as resolve, patch.object(base, '_run', side_effect=run):
            install_base_packages(plan, self.catalog, base.receipt())
            self.assertEqual(resolve.call_args.args[2], target)
        record = base.receipt()['packages']['oh-my-zsh-git']
        self.assertEqual(record['version'], '2-1')
        self.assertEqual(record['sha256'], hashlib.sha256(artifact.read_bytes()).hexdigest())
        self.assertFalse(any(cmd[0] == 'yay' for cmd in commands))

    def test_no_tty_shell_is_quiet_and_path_preserves_project_and_deduplicates(self):
        base.apply(self.kconf,self.catalog)
        project = self.home/'project/.venv';(project/'bin').mkdir(parents=True)
        env = {**os.environ,'VIRTUAL_ENV':str(project),'PATH':f'/usr/bin::{self.home}/.local/bin:/mnt/c/Windows:{self.home}/.npm-global/bin:{self.home}/go/bin:/usr/bin'}
        for shell in ['bash','zsh']:
            p = subprocess.run([shell,'-c','. "$XDG_CONFIG_HOME/pennix/base/env.sh"; . "$XDG_CONFIG_HOME/pennix/base/env.sh"; printf "%s" "$PATH"'],env=env,capture_output=True,text=True)
            self.assertEqual(p.returncode,0,p.stderr)
            parts=p.stdout.split(':');self.assertEqual(parts[0],str(project/'bin'));self.assertEqual(len(parts),len(set(parts)))
            self.assertNotIn('',parts);self.assertNotIn('/mnt/c/Windows',parts)
            self.assertEqual(parts[1:4],[str(self.home/x) for x in ['.local/bin','.cargo/bin','.go/bin']])
            self.assertNotIn(str(self.home/'go/bin'),parts)
            go=subprocess.run([shell,'-c','. "$XDG_CONFIG_HOME/pennix/base/env.sh"; printf "%s\\n" "$GOPATH" "$GOBIN"'],env=env,capture_output=True,text=True,check=True)
            self.assertEqual(go.stdout.splitlines(),[str(self.home/'.go'),str(self.home/'.go/bin')])
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
        self.assertEqual(value['side_effects']['path']['action'],'keep')
        with contextlib.redirect_stdout(io.StringIO()) as output:
            pennix._emit(value,False)
        self.assertIn('PATH: keep',output.getvalue())
        for key,item in value['components'].items():
            if key not in base.SCOPE:self.assertEqual(item['action'],'keep')
        steps=pennix._apply_steps(value,args,self.catalog)
        self.assertNotIn('path-env:apply',[x['id'] for x in steps])

    def test_clean_host_dependencies_and_ocr_provider_follow_native_resolution(self):
        for key in ['tmux','poppler','tesseract','tesseract-eng','tesseract-chi-sim']:
            self.assertTrue(self.catalog['components'][key]['package']['resolve_dependencies'])
        self.assertLess(pennix.APPLY_ORDER.index('tesseract-eng'),pennix.APPLY_ORDER.index('tesseract'))
        self.assertLess(pennix.APPLY_ORDER.index('tesseract-chi-sim'),pennix.APPLY_ORDER.index('tesseract'))

    def test_bootstrap_uv_stays_with_native_owner_without_fabricating_a_lock(self):
        args=pennix.parser().parse_args(['plan','--scope','base'])
        def status(args,key,component):
            return {'state':'match','ownership':'external'} if key=='uv' else {'state':'missing'}
        with patch.object(pennix,'_component_status',side_effect=status),\
             patch.object(engine,'package_lock_migration',return_value=None),\
             patch.object(pennix.pnpm_migration,'needed',return_value=False):
            value=pennix._plan(args)
        self.assertEqual(value['components']['uv']['action'],'keep')
        self.assertEqual(value['components']['uv']['ownership'],'external')
        self.assertNotIn('uv',args.component_lock['components'])

    def test_uninstall_removes_the_prefix_added_when_npm_was_initially_absent(self):
        base.apply(self.kconf,self.catalog)
        record=base.receipt();record['native']['npm_prefix']=None;base._save(record)
        with patch.object(base,'_run',return_value='') as native,\
             patch.object(base.subprocess,'run',return_value=SimpleNamespace(returncode=1,stdout='')):
            base.uninstall()
        native.assert_called_once_with(['npm','config','delete','prefix','--location=user'])

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
