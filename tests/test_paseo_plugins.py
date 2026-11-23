import json
import os
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import apply_journal
import owned_files
import paseo_plugins


class PaseoPluginsTests(unittest.TestCase):
    def test_cutover_recovers_after_native_remove_without_replaying_remove(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            with patch.dict(os.environ, {"HOME": directory, "XDG_STATE_HOME": directory,
                                          "CODEX_HOME": str(home / "codex")}):
                ref = "a" * 40
                plugin = {
                    "ref": ref, "path": "registry", "registry": {"id": "reviewed"},
                    "enabled": True, "status": "running",
                    "settings": {"timeline": {"values": {"images": "large"}, "revision": "old"}},
                }
                before = {"server": "one", "plugin": plugin}
                state = deepcopy(before)
                actions = []

                def rpc(key, action="inspect", previous=None, **data):
                    nonlocal state
                    if action == "inspect":
                        return deepcopy(state)
                    self.assertEqual(previous, state)
                    actions.append(action)
                    if action == "remove":
                        state = {"server": "one", "plugin": None, "residue": False}
                        raise paseo_plugins.PluginError("connection lost after remove")
                    if action == "install":
                        state = {"server": "one", "plugin": {**plugin, "registry": None, "path": "git"}}
                    if action == "settings":
                        state["plugin"]["settings"][data["block"]] = {"values": data["values"], "revision": "new"}
                    return deepcopy(state)

                rpc.home = home
                key = "paseo-chat-polish"
                source = "paseo-git:https://example.com/plugin.git"
                args = SimpleNamespace(plugin_profile_digest="approved", component_lock_sources={key: source},
                                       component_lock={"schema": 1, "components": {}})
                target = {"fingerprint": "approved", "previous_lock": None, "previous_receipt": None,
                          "before": before, "desired": {"ref": ref, "enabled": True,
                                                       "settings": {"timeline": {"images": "small"}}}}

                def start(_, command, component, steps):
                    return apply_journal.start(command, component, "b" * 64, None, steps)

                with self.assertRaisesRegex(paseo_plugins.PluginError, "connection lost"):
                    paseo_plugins.execute(args, key, "adopt", target, rpc, lambda _: None, lambda _: None, start)
                pending = apply_journal.read()
                self.assertEqual(pending["steps"][0]["target"]["pending"]["operation"], "remove")

                state["residue"] = True
                with self.assertRaisesRegex(paseo_plugins.PluginError, "中断现场"):
                    paseo_plugins.execute(args, key, "adopt", target, rpc, lambda _: None, lambda _: None, start)
                self.assertEqual(actions, ["remove"])
                state["residue"] = False
                paseo_plugins.execute(args, key, "adopt", target, rpc, lambda _: None, lambda _: None, start)
                self.assertEqual(actions, ["remove", "install", "settings"])
                self.assertIsNone(apply_journal.read())
                self.assertTrue(paseo_plugins.matches(state["plugin"], target["desired"]))
                self.assertEqual(args.component_lock["components"][key]["ref"], ref)
                receipt = owned_files.read_json(home / "pennix-lifecycle/paseo-plugins" / f"{key}.json")
                self.assertEqual(receipt["accepted"], target["desired"])

    def test_unknown_profile_fields_rejected(self):
        catalog_path = Path(__file__).resolve().parents[1] / "references/catalog.json"
        catalog = json.loads(catalog_path.read_bytes())["paseo_plugins"]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "paseo.json"
            key = "phone-notify"
            spec = catalog[key]
            profile = {"schema": 1, "plugins": {key: {"ref": spec["schema_1_refs"][0], "enabled": True,
                       "settings": {"notify": {name: True for name in spec["settings"]["notify"]}}}}}
            path.write_bytes(owned_files.json_bytes(profile))
            self.assertEqual(paseo_plugins.load_profile(path, catalog)[1], profile["plugins"])
            profile["plugins"][key]["settings"]["notify"]["token"] = "unexpected"
            path.write_bytes(owned_files.json_bytes(profile))
            with self.assertRaises(paseo_plugins.PluginError):
                paseo_plugins.load_profile(path, catalog)
