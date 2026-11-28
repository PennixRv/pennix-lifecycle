import json
import os
import subprocess
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
import pennix


class PaseoPluginsTests(unittest.TestCase):
    def test_native_bridge_checks_blobs_for_exact_compatible_refs(self):
        bridge = Path(__file__).resolve().parents[1] / "scripts/paseo-plugins.mjs"
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            checkout = home / "plugins/fixture-plugin/source"
            checkout.mkdir(parents=True)
            def git(*args):
                return subprocess.check_output(["git", "-C", str(checkout), *args], text=True).strip()
            git("init", "-q")
            (checkout / "settings.ts").write_text("reviewed settings\n")
            git("add", "settings.ts")
            git("-c", "user.name=fixture", "-c", "user.email=fixture@example.test",
                "commit", "-qm", "fixture")
            revision = git("rev-parse", "HEAD")
            blob = git("rev-parse", "HEAD:settings.ts")
            program = (
                "import {managePlugins} from " + json.dumps(bridge.as_uri()) + ";"
                "const plugin=" + json.dumps({"id": "fixture-plugin", "path": str(checkout),
                    "enabled": True, "status": "running", "installation": {"currentRevision": revision,
                    "identity": {"kind": "git", "remote": "https://example.test/plugin.git", "pluginPath": "."}}}) + ";"
                "const client={getDaemonConfig:async()=>({config:{pluginsEnabled:true}}),"
                "listPlugins:async()=>[plugin]};"
                "for(const name of ['invokePluginRpc','installPluginSource','previewPluginUpdates',"
                "'applyPluginUpdates','removePlugin','enablePlugin','disablePlugin'])"
                "client[name]=()=>{throw Error('inspect must not mutate')};"
                "console.log(JSON.stringify(await managePlugins(client," + json.dumps(str(home)) +
                ",{serverId:'fixture'})));")
            request = {"id": "fixture-plugin", "action": "inspect",
                       "url": "https://example.test/plugin.git", "blocks": [],
                       "settingsContract": {"settings.ts": "0" * 40},
                       "settingsUpgrade": {"ref": "a" * 40, "refs": [revision],
                                           "contract": {"settings.ts": blob}}}
            def invoke():
                return subprocess.run(["node", "--input-type=module", "-e", program],
                    input=json.dumps(request), capture_output=True, text=True, timeout=15)
            result = invoke()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["plugin"]["ref"], revision)
            request["settingsUpgrade"]["refs"] = ["b" * 40]
            result = invoke()
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("plugin settings contract changed", result.stderr)
            request["settingsUpgrade"] = {"ref": revision, "contract": {"settings.ts": blob}}
            self.assertEqual(invoke().returncode, 0)

    def test_exact_compatible_polish_ref_preserves_settings_and_rollback(self):
        catalog = json.loads((Path(__file__).resolve().parents[1] / "references/catalog.json").read_bytes())
        spec = catalog["paseo_plugins"]["paseo-chat-polish"]
        migration = spec["user_markdown_migration"]
        previous, current = migration["to"], migration["compatible_to"][0]
        self.assertEqual(paseo_plugins.settings_shape(spec, previous),
                         paseo_plugins.settings_shape(spec, current))
        for source, target in ((previous, current), (current, previous)):
            self.assertIsNone(paseo_plugins.migration_direction(spec, source, target))
        self.assertEqual(paseo_plugins.migration_direction(spec, migration["from"], current),
                         "introduce-user-markdown")
        self.assertEqual(paseo_plugins.migration_direction(spec, current, migration["from"]),
                         "remove-user-markdown")
        with self.assertRaises(paseo_plugins.PluginError):
            paseo_plugins.migration_direction(spec, "f" * 40, current)
        self.assertNotIn("userMarkdown", paseo_plugins.settings_shape(spec, "f" * 40)["timeline"])
        with patch.object(paseo_plugins.subprocess, "run") as native:
            native.return_value = SimpleNamespace(stdout='{"plugin":null}')
            with patch.object(paseo_plugins.shutil, "which", side_effect=lambda name: "/usr/bin/" + name):
                paseo_plugins.Native(Path("/fixture"), catalog["paseo_plugins"])("paseo-chat-polish")
            request = json.loads(native.call_args.kwargs["input"])
            self.assertEqual(set(request["settingsUpgrade"]["refs"]), {previous, current})
            self.assertEqual(request["settingsUpgrade"]["contract"], migration["settings_contract"])

    def test_chat_polish_transition_accepts_only_reviewed_ref_pair_and_preserved_settings(self):
        catalog = json.loads((Path(__file__).resolve().parents[1] / "references/catalog.json").read_bytes())
        spec = catalog["paseo_plugins"]["paseo-chat-polish"]
        old, new = (spec["user_markdown_migration"][key] for key in ("from", "to"))
        forward = paseo_plugins.migration_direction(spec, old, new)
        rollback = paseo_plugins.migration_direction(spec, new, old)
        old_settings = {"timeline": {"userMessages": "full"}, "theme": {"name": "fixture"}}
        new_settings = paseo_plugins.migrated_settings(old_settings, forward)
        self.assertEqual(new_settings["timeline"], {"userMessages": "full", "userMarkdown": "on"})
        self.assertEqual(paseo_plugins.migrated_settings(new_settings, rollback), old_settings)
        self.assertNotIn("userMarkdown", old_settings["timeline"])
        with self.assertRaisesRegex(paseo_plugins.PluginError, "不支持这一对提交"):
            paseo_plugins.migration_direction(spec, "f" * 40, new)

        def observed(ref, settings):
            return {"plugin": {"ref": ref, "registry": None, "enabled": True, "status": "running",
                               "settings": {key: {"values": value} for key, value in settings.items()}}}

        self.assertTrue(paseo_plugins._satisfies("update", observed(new, new_settings),
                        {"ref": new, "migration": forward}, observed(old, old_settings)))
        new_settings["theme"]["name"] = "altered"
        self.assertFalse(paseo_plugins._satisfies("update", observed(new, new_settings),
                         {"ref": new, "migration": forward}, observed(old, old_settings)))
    def test_catalog_plugin_can_be_updated_and_is_optional_when_capturing(self):
        catalog = json.loads((Path(__file__).resolve().parents[1] / "references/catalog.json").read_bytes())
        specs = catalog["paseo_plugins"]
        key = "paseo-session-insights"
        args = pennix.parser().parse_args(["paseo-plugins", "update", key])
        self.assertEqual(args.component, key)
        self.assertFalse(specs[key]["capture_required"])
        states = {name: {"plugin": {"ref": "a" * 40, "enabled": True, "status": "running",
                                    "settings": {}}} for name in specs if name != key}
        states[key] = {"plugin": None}
        outputs = []
        with patch.object(paseo_plugins, "Native", return_value=lambda name: states[name]), \
                patch.object(paseo_plugins.config_model, "config_value", return_value="/fixture"):
            paseo_plugins.command(SimpleNamespace(plugin_action="capture"),
                                  lambda _: (None, None, catalog), None, None, None,
                                  lambda data, _: outputs.append(data))
        self.assertEqual(set(outputs[0]["plugins"]), set(specs) - {key})

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
            profile = {"schema": 1, "plugins": {key: {"ref": "a" * 40, "enabled": True,
                       "settings": {"notify": {name: True for name in spec["settings"]["notify"]}}}}}
            path.write_bytes(owned_files.json_bytes(profile))
            self.assertEqual(paseo_plugins.load_profile(path, catalog)[1], profile["plugins"])
            profile["plugins"][key]["settings"]["notify"]["token"] = "unexpected"
            path.write_bytes(owned_files.json_bytes(profile))
            with self.assertRaises(paseo_plugins.PluginError):
                paseo_plugins.load_profile(path, catalog)

    def test_published_annotated_tag_resolves_to_commit(self):
        spec = {"url": "https://github.com/organisation/plugin.git"}
        tag, commit = "f" * 40, "c" * 40
        release = SimpleNamespace(stdout="https://github.com/organisation/plugin/releases/tag/v1.2.3")
        result = SimpleNamespace(stdout=f"{tag}\trefs/tags/v1.2.3\n{commit}\trefs/tags/v1.2.3^{{}}\n")
        with patch.object(paseo_plugins.subprocess, "run", side_effect=[release, result]):
            self.assertEqual(paseo_plugins.latest_release(spec), {"tag": "v1.2.3", "ref": commit})
