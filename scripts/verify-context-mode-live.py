#!/usr/bin/env python3
"""Verify one local tarball through the real package/config owners and Codex."""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import queue
import subprocess
import tempfile
import threading
from types import SimpleNamespace

import _engine as engine
import native_integrations as native


def hooks_list(home, cwd):
    process = subprocess.Popen([native._command("context-mode"), "app-server"],
                               env={**os.environ, "CODEX_HOME": str(home)},
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               text=True)
    responses = queue.Queue()
    def read():
        try:
            for line in process.stdout:
                responses.put(json.loads(line))
        finally:
            responses.put(None)
    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    try:
        for number, method, params in [(1, "initialize", {"clientInfo": {"name": "pennix-cm-live", "version": "1"},
                                                         "capabilities": {"experimentalApi": True}}),
                                       (2, "hooks/list", {"cwds": [str(cwd)]})]:
            process.stdin.write(json.dumps({"id": number, "method": method, "params": params}) + "\n")
            process.stdin.flush()
            while True:
                response = responses.get(timeout=30)
                if response is None:
                    raise RuntimeError("Codex app-server exited before its response")
                if response.get("id") == number:
                    if "error" in response:
                        raise RuntimeError("Codex app-server rejected the verification request")
                    break
            if number == 1:
                process.stdin.write('{"method":"initialized"}\n')
                process.stdin.flush()
        return response["result"]["data"]
    finally:
        process.stdin.close()
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        reader.join(timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tarball", required=True)
    parser.add_argument("--version", required=True)
    args = parser.parse_args()
    tarball = Path(args.tarball).resolve(strict=True)
    candidate = {"path": str(tarball), "target": {"source": "npm:@pennixrv/context-mode:beta",
                 "version": args.version, "sha256": hashlib.sha256(tarball.read_bytes()).hexdigest()}}
    catalog = json.loads((Path(__file__).parents[1] / "references/catalog.json").read_text())
    component = copy.deepcopy(catalog["components"]["context-mode"])
    component["_candidate_target"] = candidate["target"]
    original = dict(os.environ)
    with tempfile.TemporaryDirectory(prefix="pennix-cm-live-") as directory:
        home = Path(directory) / "home with 空格"
        codex = home / ".codex"
        codex.mkdir(parents=True)
        os.environ.update(HOME=str(home), USERPROFILE=str(home), CODEX_HOME=str(codex),
                          XDG_STATE_HOME=str(home / ".local/state"), XDG_CONFIG_HOME=str(home / ".config"),
                          PATH=f"{home / '.local/bin'}:{original['PATH']}")
        try:
            (codex / "config.toml").write_text('[features]\nhooks = true\n[mcp_servers.foreign]\ncommand = "preserve"\n')
            foreign = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "true"}]}]}}
            (codex / "hooks.json").write_text(json.dumps(foreign))
            operation = SimpleNamespace(codex_home=codex, candidate=candidate, kconf=None, explicit_update=False)
            assert engine.component_operation(operation, catalog, "context-mode", component, "install") == "changed"
            assert native.apply("context-mode", codex) == "changed"
            assert native.inspect("context-mode", codex, enabled=True)["state"] == "current"
            hooks = [h for group in hooks_list(codex, home) for h in group["hooks"] if "context-mode" in h.get("command", "")]
            assert len(hooks) == 4, "real Codex must expose the four installed hooks"
            assert all(h["trustStatus"] == "trusted" for h in hooks), "plan hashes must match real Codex hashes"
            receipt = native._read_receipt("context-mode")
            assert set(receipt["trust"]) == {h["key"] for h in hooks}
            assert all(receipt["trust"][h["key"]]["trusted_hash"] == h["currentHash"] for h in hooks)
            assert native.apply("context-mode", codex) == "no-op"
            assert "preserved" in native.unconfigure("context-mode", codex)
            assert json.loads((codex / "hooks.json").read_text()) == foreign
            assert native._toml(codex / "config.toml")["mcp_servers"] == {"foreign": {"command": "preserve"}}
            assert engine.component_operation(operation, catalog, "context-mode", component, "uninstall") == "changed"
            print(json.dumps({"status": "passed", "version": args.version, "tarball_sha256": candidate["target"]["sha256"],
                              "trusted_hooks": len(hooks), "owner_install_uninstall": True}))
        finally:
            os.environ.clear()
            os.environ.update(original)


if __name__ == "__main__":
    main()
