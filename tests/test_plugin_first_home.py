"""A missing Codex home is absent plugin state and is prepared only for writing."""
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import _engine as engine


class PluginFirstHomeTests(unittest.TestCase):
    def test_missing_codex_home_does_not_turn_native_cli_failure_into_existing_install(self):
        with tempfile.TemporaryDirectory() as directory:
            home=Path(directory)/'new-codex'
            with patch.object(engine,'_plugin_snapshot',return_value=None), \
                    patch.object(engine.shutil,'which',return_value='/native/codex'), \
                    patch.object(engine.subprocess,'run',return_value=SimpleNamespace(returncode=1)) as cli:
                self.assertEqual(engine._plugin_status({'plugin':{'id':'fixture@market'}},home),('missing',None))
                cli.assert_not_called()
                self.assertFalse(home.exists())

    def test_first_plugin_write_prepares_only_the_safe_codex_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            home=Path(directory)/'new-codex'
            target={'source':'git:fixture','version':'1.0.0','ref':'a'*40}
            component={'plugin':{'id':'fixture@market','marketplace':{'name':'market','source':'https://fixture.invalid/repo'}},'_lock_target':target}
            def prepare(*_):
                self.assertTrue(home.is_dir())
                self.assertEqual(home.stat().st_mode&0o777,0o700)
                self.assertFalse((home/'config.toml').exists())
            with patch.object(engine.shutil,'which',return_value='/native/codex'), \
                    patch.object(engine,'_plugin_status',side_effect=[('missing',None),('match','1.0.0')]), \
                    patch.object(engine,'_prepare_plugin_marketplace',side_effect=prepare), \
                    patch.object(engine,'_plugin_snapshot',return_value=target), \
                    patch.object(engine,'_write_plugin_receipt') as receipt, \
                    patch.object(engine.subprocess,'run',return_value=SimpleNamespace(returncode=0)):
                self.assertEqual(engine._plugin_operation('fixture',component,home,'install'),'changed')
                receipt.assert_called_once()


if __name__=='__main__':unittest.main()
