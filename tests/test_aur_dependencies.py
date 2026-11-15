"""Fixed AUR builds resolve dependencies only within an observed full upgrade."""
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import _engine as engine
import pennix


class AurDependencyTests(unittest.TestCase):
    def test_native_identity_selects_the_main_package_among_debug_outputs(self):
        self._select_artifact('debug',True)

    def test_duplicate_or_wrong_native_identity_never_reaches_install(self):
        for variant in ['duplicate','wrong-version']:
            with self.subTest(variant=variant):self._select_artifact(variant,False)

    def _select_artifact(self,variant,success):
        with tempfile.TemporaryDirectory() as directory:
            tree=Path(directory);main=tree/'example-1.0-1-x86_64.pkg.tar.zst';main.touch()
            (tree/'example-debug-1.0-1-x86_64.pkg.tar.zst').touch()
            (tree/(main.name+'.sig')).touch()
            if variant=='duplicate':(tree/'example-1.0-1-x86_64.pkg.tar.xz').touch()
            component={'delivery':'package','package':{'source':'aur','name':'example'},'_lock_target':{'version':'1.0-1'}}
            args=SimpleNamespace(codex_home=tree/'codex',candidate={'path':str(tree)})
            commands=[]
            def run(command,**kwargs):
                commands.append(command)
                name='example-debug' if 'example-debug-' in command[-1] else 'example'
                version='1.1-1' if variant=='wrong-version' else '1.0-1'
                return SimpleNamespace(returncode=0,stdout=f'{name} {version}\n')
            with patch.object(engine.host,'detect_host',return_value={'supported':True,'installers':{'aur':'yay'}}), \
                    patch.object(engine,'probe_component',side_effect=[('missing',None),('match','1.0'),('match','1.0')]), \
                    patch.object(engine,'package_owned',return_value=False), \
                    patch.object(engine,'_write_package_receipt') as receipt, \
                    patch.object(engine.subprocess,'run',side_effect=run):
                if success:
                    self.assertEqual(engine.component_operation(args,{},'example',component,'install'),'changed')
                    self.assertEqual(commands[-1],['sudo','pacman','-U','--needed','--noconfirm',str(main)])
                    self.assertFalse(any(command[-1].endswith('.sig') for command in commands))
                    receipt.assert_called_once()
                else:
                    with self.assertRaises(engine.BootstrapError):engine.component_operation(args,{},'example',component,'install')
                    self.assertFalse(any(command[0]=='sudo' for command in commands))
                    receipt.assert_not_called()

    def test_cli_passes_dependency_permission_only_from_same_target_verified_upgrade(self):
        args = SimpleNamespace(codex_home=Path('/tmp/aur-codex'))
        component = {'delivery':'package','package':{'source':'aur'}}
        identity = {'home':str(Path.home()),'codex_home':str(args.codex_home),'state_root':str(pennix.owned_files.state_root())}
        for state, same_target in [('pending',True),('unknown',True),('verified',False),('verified',True)]:
            active = {'identity':identity if same_target else {**identity,'codex_home':'/different'},
                      'steps':[{'id':'system-upgrade','status':state}]}
            with self.subTest(state=state,same_target=same_target), \
                    patch.object(pennix,'_verify_inputs'),patch.object(pennix,'_delegation_preflight'), \
                    patch.object(pennix.owned_files,'bind_target'), \
                    patch.object(pennix.apply_journal,'read',return_value=active), \
                    patch.object(engine,'component_operation',return_value='changed') as run:
                pennix._run_component(args,{'components':{'example':component}},'example','install',candidate={'path':'/fixed'})
                self.assertEqual(run.call_args.args[0].arch_dependencies_allowed,state=='verified' and same_target)

    def test_build_and_native_package_identity_keep_dependency_permission_and_failure_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            tree=Path(directory);artifact=tree/'example-1.0-1-x86_64.pkg.tar.zst';artifact.touch()
            component={'delivery':'package','package':{'source':'aur','name':'example'},
                       '_lock_target':{'version':'1.0-1'}}
            for allowed, failed in [(False,False),(True,False),(True,True)]:
                commands=[]
                def run(command,**kwargs):
                    commands.append(command)
                    return SimpleNamespace(returncode=8 if failed and command[0]=='makepkg' else 0,
                                           stdout='example 1.0-1\n')
                args=SimpleNamespace(codex_home=tree/'codex',candidate={'path':str(tree)},arch_dependencies_allowed=allowed)
                with self.subTest(allowed=allowed,failed=failed), \
                        patch.object(engine.host,'detect_host',return_value={'supported':True,'installers':{'aur':'yay'}}), \
                        patch.object(engine,'probe_component',side_effect=[('missing',None),('match','1.0'),('match','1.0')]), \
                        patch.object(engine,'package_owned',return_value=False), \
                        patch.object(engine,'_write_package_receipt') as receipt, \
                        patch.object(engine.subprocess,'run',side_effect=run):
                    if failed:
                        with self.assertRaises(engine.BootstrapError):engine.component_operation(args,{},'example',component,'install')
                        receipt.assert_not_called();self.assertEqual(len(commands),1)
                    else:
                        self.assertEqual(engine.component_operation(args,{},'example',component,'install'),'changed')
                        receipt.assert_called_once()
                    self.assertEqual('--syncdeps' in commands[0],allowed)


if __name__=='__main__':unittest.main()
