"""Exercise the native SDK bridge through real module resolution and subprocess IO."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest


BRIDGE = Path(__file__).resolve().parents[1] / "scripts/paseo-skills.mjs"


class PaseoSdkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cli = self.root / "cli"
        self.cli.mkdir()
        (self.cli / "package.json").write_text(json.dumps({"name": "@getpaseo/cli", "version": "1.2.3"}))
        self.executable = self.cli / "paseo"
        self.executable.write_text("#!/bin/sh\nexit 1\n")
        self.executable.chmod(0o700)
        self.home = self.root / "home"
        self.client = self.module("client", {".": "./index.js", "./node": "./node.js"})
        self.server = self.module("server", {"./daemon-control": "./index.js"})
        for name in ("paseo", "new-native-skill"):
            skill = self.server / "dist/server/skills" / name
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("native payload\n")
        self.instance = {"pid": 123, "serverId": "instance", "listen": "127.0.0.1:1"}
        (self.server / "index.js").write_text(f"""
const instance = {json.dumps(self.instance)};
export async function readDaemonInstance() {{ return instance; }}
export async function waitForDaemonReady() {{ return instance; }}
export function readLocalCredentialForTarget() {{ return 'local'; }}
export function isSameDaemonInstance(a,b) {{ return a?.pid === b?.pid && a?.serverId === b?.serverId; }}
export function stopDaemonInstance() {{ throw new Error('must not stop an existing instance'); }}
""")
        self.executable.write_text(f"#!/bin/sh\nprintf '%s' '{json.dumps({**self.instance, 'home':str(self.home), 'daemonVersion':'9.0.0'})}'\n")
        (self.client / "node.js").write_text("""
export function createHostClient() { return {
  connect: async () => {}, close: async () => {},
  getAgentSkillsStatus: async () => {}, saveAgentSkillsSelection: async () => {},
  getLastServerInfoMessage: () => ({serverId:'instance',version:'9.0.0'})
}; }
""")

    def module(self, name, exports):
        root = self.cli / "node_modules" / "@getpaseo" / name
        root.mkdir(parents=True)
        (root / "package.json").write_text(json.dumps({"name": f"@getpaseo/{name}",
            "version": "2.0.0", "type": "module", "exports": exports}))
        return root

    def run_bridge(self, action):
        return subprocess.run(["node", str(BRIDGE), action, str(self.executable), str(self.home)],
                              capture_output=True, text=True, timeout=10)

    def test_unfamiliar_versions_use_callable_public_exports(self):
        result = self.run_bridge("probe")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["version"], "1.2.3")
        self.assertEqual(json.loads(result.stdout)["available"], ["new-native-skill", "paseo"])
        (self.client / "node.js").write_text("export const createHostClient = null;\n")
        result = self.run_bridge("probe")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("compatibility", result.stderr)

    def test_inspection_streams_all_pages_and_detects_late_delegation(self):
        for opened in (False, True):
            (self.client / "index.js").write_text(f"""
export function createPaseoApi() {{ return {{ dispose: async () => {{}}, agents: {{
  list: async (options) => {{
    if (!options.includeArchived || !options.includeBackground || options.limit !== 100) throw new Error();
    const offset = Number(options.cursor ?? 0), end = Math.min(offset + 100, 1301);
    return {{ entries: Array.from({{length:end-offset}}, (_,i) => ({{agent:{{
      id:String(offset+i), status:offset+i===1300 && {str(opened).lower()} ? 'idle' : 'closed',
      labels:offset+i===1300 ? {{'trellis.attempt':'last'}} : {{}}, activeTurn:null
    }}}})), pageInfo:{{nextCursor:end===1301 ? null : String(end)}} }};
  }}
}} }}; }}
""")
            result = self.run_bridge("inspect")
            self.assertEqual(result.returncode, 0, result.stderr)
            reply = json.loads(result.stdout)
            self.assertEqual(reply["agents_scanned"], 1301)
            self.assertIs(reply["delegations_open"], opened)
            self.assertLess(len(result.stdout), 1024)

    def test_incomplete_or_repeating_pages_never_report_success(self):
        for page in ({"entries": [], "pageInfo": {}},
                     {"entries": [], "pageInfo": {"nextCursor": "repeat"}},
                     {"entries": [{"id": "unknown", "status": "idle"}], "pageInfo": {"nextCursor": None}}):
            (self.client / "index.js").write_text("export function createPaseoApi() { return {"
                "dispose:async()=>{}, agents:{list:async()=> (" + json.dumps(page) + ")}}; }\n")
            result = self.run_bridge("inspect")
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
