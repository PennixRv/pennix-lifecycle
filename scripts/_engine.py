"""Owner operations for the versioned lifecycle tool."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import tomllib
import urllib.request
from functools import lru_cache
from pathlib import Path
from typing import Any

import host
import skill_collections
import owned_files
import local_clients


SCRIPT_ROOT = Path(__file__).resolve().parent
ROOT = SCRIPT_ROOT.parent
TEMPLATE_ROOT = ROOT / "templates"
VERSION_RE = re.compile(r"(?<![A-Za-z0-9])v?(\d+(?:\.\d+)+(?:[-+][A-Za-z0-9.-]+|[A-Za-z][A-Za-z0-9.-]*)?)")
PACKAGE_NAME = re.compile(r"^[A-Za-z0-9@._+:/-]+$")
BEGIN_AGENTS = "# pennix-lifecycle:managed-agents:begin"
END_AGENTS = "# pennix-lifecycle:managed-agents:end"
BEGIN_TMUX = "# pennix-lifecycle:managed-tmux:begin"
END_TMUX = "# pennix-lifecycle:managed-tmux:end"


class BootstrapError(RuntimeError):
    """An owner operation cannot safely continue."""


def load_catalog(path: Path) -> dict[str, Any]:
    def pairs(items):
        value: dict[str, Any] = {}
        for key, item in items:
            if key in value:
                raise BootstrapError(f"catalog contains duplicate key: {key}")
            value[key] = item
        return value

    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BootstrapError(f"cannot read catalog: {path}") from error
    if (not isinstance(value, dict) or value.get("schema") != 3
            or not isinstance(value.get("components"), dict)
            or not isinstance(value.get("configuration_targets"), dict)):
        raise BootstrapError("catalog schema 3 is required")
    for target, spec in value["configuration_targets"].items():
        if (not isinstance(target, str) or not isinstance(spec, dict)
                or not isinstance(spec.get("select"), str)
                or not isinstance(spec.get("owner"), str)
                or not isinstance(spec.get("component"), str)
                or not isinstance(spec.get("command"), list)
                or not isinstance(spec.get("values"), dict)
                or not isinstance(spec.get("secrets"), dict)):
            raise BootstrapError(f"configuration target contract is invalid: {target}")
        if any(not isinstance(item, str) or not item for item in spec["command"]):
            raise BootstrapError(f"configuration target command is invalid: {target}")
        if any(not isinstance(item, str) or not item for item in [*spec["values"].values(), *spec["secrets"].values()]):
            raise BootstrapError(f"configuration target field mapping is invalid: {target}")
    for key, component in value["components"].items():
        if not isinstance(key, str) or not isinstance(component, dict):
            raise BootstrapError("catalog component is invalid")
        component["id"] = key
        if component.get("delivery") not in {"package", "plugin", "native", "client", "static", "collection", "state"}:
            raise BootstrapError(f"catalog delivery is invalid: {key}")
        if component.get("delivery") != "state" and not isinstance(component.get("actions"), dict):
            raise BootstrapError(f"catalog actions are missing: {key}")
        if (component.get("delivery") in {"package", "plugin", "client", "collection"}
                and (not isinstance(component.get("lock_source"), str) or not component["lock_source"])):
            raise BootstrapError(f"catalog lock source is missing: {key}")
        if component.get("delivery") == "collection":
            contract = component.get("collection_contract")
            if not isinstance(contract, dict) or set(contract) != {"directory", "source"}:
                raise BootstrapError(f"collection manifest contract is invalid: {key}")
            source = contract["source"]
            if not isinstance(source, dict) or set(source) not in ({"repo", "manifest"}, {"repo", "ref", "manifest"}):
                raise BootstrapError(f"collection source contract is invalid: {key}")
    return value


def normalize_version(raw: str) -> str | None:
    matches = VERSION_RE.findall(raw.replace("_", ""))
    return matches[-1] if matches else None


def _prefix(component: dict[str, Any]) -> Path | None:
    package = component.get("package")
    if not isinstance(package, dict) or package.get("source") != "npm":
        return None
    value = package.get("prefix")
    if not isinstance(value, str):
        raise BootstrapError("npm prefix is missing")
    expanded = Path(os.path.expandvars(os.path.expanduser(value)))
    if not expanded.is_absolute():
        raise BootstrapError("npm prefix must be absolute")
    return expanded


def _command(component: dict[str, Any]) -> str | None:
    probe = component.get("probe")
    if not isinstance(probe, str) or not PACKAGE_NAME.fullmatch(probe):
        return None
    prefix = _prefix(component)
    if prefix is not None:
        candidate = prefix / "bin" / probe
        return str(candidate) if candidate.is_file() else None
    return shutil.which(probe)


def _version(component: dict[str, Any], command: str) -> str | None:
    try:
        result = subprocess.run(
            [command, *[str(item) for item in component.get("version_args", [])]],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return normalize_version((result.stdout or result.stderr).strip())


def _target_version(component: dict[str, Any]) -> str | None:
    target = component.get("_candidate_target") or component.get("_lock_target")
    value = target.get("version") if isinstance(target, dict) else None
    return value if isinstance(value, str) else None


def _static_target(component_id: str, codex_home: Path) -> Path:
    if component_id == "codex-agents":
        return codex_home / "AGENTS.md"
    if component_id == "tmux-config":
        return Path.home() / ".tmux.conf"
    raise BootstrapError(f"unsupported static component: {component_id}")


def _template(component: dict[str, Any]) -> str:
    name = component.get("template", {}).get("name") if isinstance(component.get("template"), dict) else None
    if not isinstance(name, str) or Path(name).name != name:
        raise BootstrapError("static template is invalid")
    path = TEMPLATE_ROOT / name
    try:
        value = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise BootstrapError(f"static template is unavailable: {name}") from error
    if not value.strip():
        raise BootstrapError(f"static template is empty: {name}")
    return value


def _read(path: Path) -> str:
    if not path.exists():
        return ""
    if path.is_symlink() or not path.is_file():
        raise BootstrapError(f"unsafe static target: {path}")
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise BootstrapError(f"cannot read static target: {path}") from error


def _write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise BootstrapError(f"unsafe static target: {path}")
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _agents_block(template: str) -> str:
    return f"{BEGIN_AGENTS}\n{template.rstrip()}\n{END_AGENTS}"


def _agents_receipt(path: Path) -> Path:
    return owned_files.state_root() / "static" / (hashlib.sha256(str(path).encode()).hexdigest() + ".json")


def _agents_record(path: Path, content: bytes) -> dict:
    return {"schema": 1, "path": str(path), "digest": hashlib.sha256(content).hexdigest()}


def _static_state(component_id: str, codex_home: Path, component: dict[str, Any]) -> str:
    path = _static_target(component_id, codex_home)
    current = _read(path)
    if component_id == "codex-agents":
        if not current:
            return "missing"
        if current == _template(component):
            return "current"
        receipt = owned_files.read_json(_agents_receipt(path))
        raw = owned_files.read(path)
        if raw is not None and receipt == _agents_record(path, raw):
            return "outdated"
        return "drifted"
    block = f"{BEGIN_TMUX}\n{_template(component).rstrip()}\n{END_TMUX}"
    if not current:
        return "missing"
    if BEGIN_TMUX not in current and END_TMUX not in current:
        return "missing"
    if current.count(BEGIN_TMUX) != 1 or current.count(END_TMUX) != 1:
        return "drifted"
    start = current.index(BEGIN_TMUX)
    finish = current.index(END_TMUX, start) + len(END_TMUX)
    return "current" if current[start:finish] == block else "drifted"


def _operate_static(component_id: str, codex_home: Path, component: dict[str, Any], operation: str, expected_sha256: str | None = None) -> str:
    path = _static_target(component_id, codex_home)
    state = _static_state(component_id, codex_home, component)
    if component_id == "codex-agents":
        before = owned_files.read(path)
        receipt_path = _agents_receipt(path)
        before_receipt = owned_files.read(receipt_path, private=True)
        if expected_sha256 is not None:
            if (operation != "upgrade" or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)
                    or before is None or hashlib.sha256(before).hexdigest() != expected_sha256):
                raise BootstrapError("AGENTS.md 与已审阅的 SHA-256 不符；未执行修改")
            state = "outdated"
        if operation == "uninstall":
            if state == "missing":
                return "no-op"
            if state not in {"current", "outdated"}:
                raise BootstrapError(f"refusing {state} AGENTS.md")
            owned_files.replace(path, None, before)
            owned_files.replace(receipt_path, None, before_receipt)
            return "changed"
        if state == "current":
            owned_files.replace(receipt_path, owned_files.json_bytes(_agents_record(path, before)), before_receipt)
            return "no-op"
        if state != "missing" and not (state == "outdated" and operation == "upgrade"):
            raise BootstrapError(f"refusing {state} AGENTS.md")
        output = _template(component).encode()
        owned_files.replace(path, output, before)
        try:
            owned_files.replace(receipt_path, owned_files.json_bytes(_agents_record(path, output)), before_receipt)
        except BaseException:
            owned_files.replace(path, before, output)
            raise
        return "changed"

    current = _read(path)
    block = f"{BEGIN_TMUX}\n{_template(component).rstrip()}\n{END_TMUX}"
    if operation == "uninstall":
        if state == "missing":
            return "no-op"
        if state != "current":
            raise BootstrapError(f"refusing {state} tmux config")
        start = current.index(BEGIN_TMUX)
        finish = current.index(END_TMUX, start) + len(END_TMUX)
        _write(path, current[:start].rstrip("\n") + current[finish:])
        return "changed"
    if state == "current":
        return "no-op"
    if state != "missing":
        raise BootstrapError(f"refusing {state} tmux config")
    _write(path, current.rstrip("\n") + ("\n\n" if current else "") + block + "\n")
    return "changed"


def _collection_destination(component: dict[str, Any], codex_home: Path) -> Path:
    return skill_collections.destination(component, codex_home)


def collection_owned(component: dict[str, Any], codex_home: Path) -> bool:
    return skill_collections.owned(component, codex_home)


def _collection_state(component: dict[str, Any], codex_home: Path) -> str:
    return skill_collections.inspect(component, codex_home)[0]


def _operate_collection(component: dict[str, Any], codex_home: Path, operation: str, *, candidate: dict | None = None) -> str:
    try:
        return skill_collections.operate(component, codex_home, operation, candidate=candidate)
    except (skill_collections.CollectionError, OSError, subprocess.TimeoutExpired) as error:
        raise BootstrapError(str(error)) from error


def _plugin_snapshot(component: dict[str, Any], codex_home: Path) -> dict[str, str] | None:
    plugin = component.get("plugin")
    if not isinstance(plugin, dict):
        return None
    codex = shutil.which("codex")
    if not codex:
        return None
    result = subprocess.run([codex, "plugin", "list", "--json"], capture_output=True, text=True, check=False,
                            env={**os.environ, "CODEX_HOME": str(codex_home)}, timeout=30)
    if result.returncode:
        return None
    try:
        installed = [item for item in json.loads(result.stdout).get("installed", [])
                     if isinstance(item, dict) and item.get("pluginId") == plugin.get("id")]
        if len(installed) != 1:
            return None
        item = installed[0]
        market_result = subprocess.run([codex, "plugin", "marketplace", "list", "--json"], capture_output=True,
                                       text=True, check=False,
                                       env={**os.environ, "CODEX_HOME": str(codex_home)}, timeout=30)
        if market_result.returncode:
            return None
        marketplaces = [market for market in json.loads(market_result.stdout).get("marketplaces", [])
                        if isinstance(market, dict) and market.get("name") == plugin["marketplace"]["name"]]
        if len(marketplaces) != 1 or item.get("enabled") is not True:
            return None
        market = marketplaces[0]
        if (market.get("marketplaceSource", {}).get("source") != plugin["marketplace"]["source"]
                or item.get("source", {}).get("url") != plugin["marketplace"]["source"]):
            return None
        root = Path(market.get("root", ""))
        head = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True,
                              timeout=15, check=False)
        clean = _plugin_tree_clean(root)
        try:
            manifest = json.loads((root / ".codex-plugin/plugin.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        version = item.get("version")
        name = plugin["id"].split("@", 1)[0]
        if (head.returncode or not clean
                or not re.fullmatch(r"[0-9a-f]{40}", head.stdout.strip())
                or not isinstance(version, str) or manifest.get("name") != name
                or manifest.get("version") != version):
            return None
        snapshot = {"source": component["lock_source"], "version": version, "ref": head.stdout.strip()}
        selected_plugin = {**plugin, "marketplace": {**plugin["marketplace"], "ref": snapshot["ref"]}}
        if not _plugin_refs(codex, codex_home, selected_plugin, version):
            return None
        return snapshot
    except (json.JSONDecodeError, TypeError, KeyError, OSError, subprocess.TimeoutExpired):
        return None


def _plugin_receipt_path(component_id: str) -> Path:
    if not PACKAGE_NAME.fullmatch(component_id):
        raise BootstrapError("plugin identity is invalid")
    return owned_files.state_root() / "plugins" / f"{component_id}.json"


def _plugin_receipt(component_id: str, component: dict[str, Any]) -> dict[str, str] | None:
    value = owned_files.read_json(_plugin_receipt_path(component_id))
    if value is None:
        return None
    plugin = component["plugin"]
    if (not isinstance(value, dict) or set(value) != {"schema", "component", "id", "source", "version", "ref"}
            or type(value["schema"]) is not int or value["schema"] != 1 or value["component"] != component_id
            or value["id"] != plugin["id"] or value["source"] != component["lock_source"]
            or not isinstance(value["version"], str) or not re.fullmatch(r"[0-9a-f]{40}", value["ref"])):
        raise BootstrapError(f"plugin ownership receipt is invalid: {component_id}")
    return {key: value[key] for key in ("source", "version", "ref")}


def plugin_lock_migration(component_id: str, component: dict[str, Any], codex_home: Path) -> dict[str, str] | None:
    snapshot = _plugin_snapshot(component, codex_home)
    if snapshot is None:
        return None
    receipt = _plugin_receipt(component_id, component)
    if receipt is not None and receipt != snapshot:
        return None
    return snapshot


def _plugin_owned(component_id: str, component: dict[str, Any], codex_home: Path) -> bool:
    receipt = _plugin_receipt(component_id, component)
    return receipt is not None and receipt == _plugin_snapshot(component, codex_home)


def _write_plugin_receipt(component_id: str, component: dict[str, Any], snapshot: dict[str, str]) -> None:
    path = _plugin_receipt_path(component_id)
    before = owned_files.read(path, private=True)
    record = {"schema": 1, "component": component_id, "id": component["plugin"]["id"],
              **snapshot}
    owned_files.replace(path, owned_files.json_bytes(record), before)


def _remove_plugin_receipt(component_id: str) -> None:
    path = _plugin_receipt_path(component_id)
    before = owned_files.read(path, private=True)
    if before is not None:
        owned_files.replace(path, None, before)


def _plugin_status(component: dict[str, Any], codex_home: Path) -> tuple[str, str | None]:
    plugin = component.get("plugin")
    if not isinstance(plugin, dict):
        return "blocked", None
    snapshot = _plugin_snapshot(component, codex_home)
    if snapshot is None:
        codex = shutil.which("codex")
        if not codex:
            return "missing", None
        result = subprocess.run([codex, "plugin", "list", "--json"], capture_output=True, text=True, check=False,
                                env={**os.environ, "CODEX_HOME": str(codex_home)}, timeout=30)
        if result.returncode:
            return "blocked", None
        try:
            installed = [item for item in json.loads(result.stdout).get("installed", [])
                         if isinstance(item, dict) and item.get("pluginId") == plugin.get("id")]
        except (json.JSONDecodeError, TypeError):
            return "blocked", None
        return ("blocked", str(installed[0].get("version", ""))) if installed else ("missing", None)
    target = component.get("_candidate_target") or component.get("_lock_target")
    if not isinstance(target, dict):
        return "outdated", snapshot["version"]
    matches = all(snapshot.get(key) == target.get(key) for key in ("source", "version", "ref") if key in target)
    return ("match" if matches else "outdated"), snapshot["version"]


def _plugin_refs(codex: str, home: Path, plugin: dict, version: str) -> bool:
    result = subprocess.run([codex, "plugin", "marketplace", "list", "--json"], capture_output=True, text=True,
                            env={**os.environ, "CODEX_HOME": str(home)}, timeout=30, check=False)
    try:
        market = next(item for item in json.loads(result.stdout)["marketplaces"] if item["name"] == plugin["marketplace"]["name"])
        name, marketplace = plugin["id"].split("@")
        if any(not re.fullmatch(r"[A-Za-z0-9_.-]+", value) for value in (name, marketplace, version)):
            return False
        roots = [Path(market["root"]), home / "plugins/cache" / marketplace / name / version]
        for root in roots:
            head = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=15, check=False)
            if head.returncode or not _plugin_tree_clean(root) or head.stdout.strip() != plugin["marketplace"]["ref"]:
                return False
        return result.returncode == 0
    except (ValueError, KeyError, TypeError, StopIteration):
        return False


def _plugin_tree_clean(root: Path) -> bool:
    result = subprocess.run(["git", "-C", str(root), "-c", "core.fsmonitor=false", "status", "--porcelain",
                             "--untracked-files=all"], capture_output=True, text=True, timeout=15, check=False)
    # Codex writes this marketplace ownership marker beside otherwise immutable source files.
    return result.returncode == 0 and all(line == "?? .codex-marketplace-install.json"
                                          for line in result.stdout.splitlines())


def _prepare_plugin_marketplace(codex: str, home: Path, marketplace: dict[str, Any]) -> None:
    """Refresh a same-source marketplace so its pinned ref can change safely."""
    env = {**os.environ, "CODEX_HOME": str(home)}
    result = subprocess.run([codex, "plugin", "marketplace", "list", "--json"],
                            capture_output=True, text=True, check=False, env=env)
    if result.returncode:
        raise BootstrapError("Codex marketplace listing failed")
    try:
        configured = next(
            item for item in json.loads(result.stdout).get("marketplaces", [])
            if item.get("name") == marketplace["name"]
        )
    except (TypeError, ValueError, StopIteration):
        configured = None
    if configured is not None:
        source = configured.get("marketplaceSource", {}).get("source")
        if source != marketplace["source"]:
            raise BootstrapError(f"Codex marketplace {marketplace['name']} has a different source")
        result = subprocess.run([codex, "plugin", "marketplace", "remove", marketplace["name"], "--json"],
                                check=False, env=env)
        if result.returncode:
            raise BootstrapError("Codex marketplace removal failed")


def probe_component(component: dict[str, Any], codex_home: Path | None = None, destination: str | None = None, home_directory: Path | None = None) -> tuple[str, str | None]:
    codex_home = codex_home or Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    delivery = component.get("delivery")
    adapter = component.get("adapter")
    if delivery == "client":
        return local_clients.inspect(component["id"], component)
    if adapter in {"codex-agents", "tmux-config"}:
        state = _static_state(adapter, codex_home, component)
        return {"missing": "missing", "current": "match"}.get(state, "blocked" if state == "blocked" else "drifted"), state
    if delivery == "collection":
        return skill_collections.inspect(component, codex_home)
    if isinstance(component.get("plugin"), dict):
        return _plugin_status(component, codex_home)
    if delivery == "state":
        return "unknown", None
    command = _command(component)
    if command is None:
        return "missing", None
    observed = _version(component, command)
    if observed is None:
        return "blocked", command
    package = component.get("package")
    if isinstance(package, dict) and package.get("source") in {"official", "aur"}:
        try:
            observed = package_identity(component)["version"]
        except BootstrapError:
            return "blocked", command
    expected = _target_version(component)
    state = "match" if expected is None or observed == expected else "outdated"
    return state, observed


def package_identity(component: dict[str, Any]) -> dict[str, str]:
    """Verify the package manager and the executable before claiming ownership."""
    package = component["package"]
    command = _command(component)
    if not command:
        raise BootstrapError("package executable is missing")
    if package["source"] == "uv":
        directory = subprocess.run(["uv", "tool", "dir"], capture_output=True, text=True, timeout=15, check=False)
        tool_root = Path(directory.stdout.strip()) / package["name"]
        try:
            record = tomllib.loads((tool_root / "uv-receipt.toml").read_text())
            entries = record["tool"]["entrypoints"]
            if (directory.returncode or not tool_root.is_absolute()
                    or Path(command).resolve(strict=True) != (tool_root / "bin" / component["probe"]).resolve(strict=True)
                    or not any(entry["name"] == component["probe"] and entry["from"] == package["name"]
                               and entry["install-path"] == command for entry in entries)):
                raise ValueError("uv executable owner mismatch")
            version = _version(component, command)
            if version is None:
                raise ValueError("uv version unavailable")
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise BootstrapError("cannot verify uv tool owner") from error
        return {"manager": "uv", "name": package["name"], "version": version, "command": str(Path(command).resolve())}
    if package["source"] == "npm":
        prefix = _prefix(component)
        root = prefix / "lib/node_modules" / package["name"]
        try:
            metadata = json.loads((root / "package.json").read_text())
            bins = metadata.get("bin", {})
            relative = bins if isinstance(bins, str) else bins.get(component["probe"])
            if (metadata["name"] != package["name"] or not relative
                    or Path(command).resolve(strict=True) != (root / relative).resolve(strict=True)):
                raise ValueError("wrong executable owner")
            result = subprocess.run(["npm", "list", "--global", "--prefix", str(prefix), "--depth=0", "--json", package["name"]],
                                    capture_output=True, text=True, timeout=30, check=False)
            actual = json.loads(result.stdout)["dependencies"][package["name"]]["version"]
            if result.returncode or actual != metadata["version"]:
                raise ValueError("npm package identity differs")
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise BootstrapError(f"cannot verify npm package owner: {package['name']}") from error
        return {"manager": "npm", "name": metadata["name"], "version": actual, "command": str(Path(command).resolve())}
    result = subprocess.run(["pacman", "-Q", package["name"]], capture_output=True, text=True, timeout=30, check=False)
    owner = subprocess.run(["pacman", "-Qoq", str(Path(command).resolve())], capture_output=True, text=True, timeout=30, check=False)
    parts = result.stdout.strip().split()
    if result.returncode or owner.returncode or len(parts) != 2 or parts[0] != package["name"] or owner.stdout.strip() != package["name"]:
        raise BootstrapError(f"cannot verify pacman executable owner: {package['name']}")
    return {"manager": "pacman", "name": parts[0], "version": parts[1], "command": str(Path(command).resolve())}


@lru_cache(maxsize=32)
def _candidate(source: str, name: str) -> str:
    installer = host.detect_host()["installers"].get(source)
    if not installer:
        if source != "aur":
            raise BootstrapError(f"no package candidate owner for {name}")
        try:
            with urllib.request.urlopen(f"https://aur.archlinux.org/rpc/v5/info/{name}", timeout=30) as response:
                value = json.load(response)
            result = value["results"]
            if len(result) != 1 or result[0]["Name"] != name or not isinstance(result[0]["Version"], str):
                raise ValueError()
            return result[0]["Version"]
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise BootstrapError(f"cannot query AUR candidate: {name}") from error
    result = subprocess.run(host.package_info_command(installer, name), capture_output=True, text=True,
                            env={**os.environ, "LC_ALL": "C"}, timeout=60, check=False)
    version = re.search(r"^Version\s*:\s*(\S+)", result.stdout, re.MULTILINE)
    if result.returncode or not version:
        raise BootstrapError(f"cannot query package candidate: {name}")
    return version[1]


def package_candidate(component: dict[str, Any]) -> str:
    package = component["package"]
    return _candidate(package["source"], package["name"])


def _package_receipt_path(component_id: str) -> Path:
    if not PACKAGE_NAME.fullmatch(component_id):
        raise BootstrapError("package identity is invalid")
    return owned_files.state_root() / "packages" / f"{component_id}.json"


def _package_receipt(component_id: str, component: dict[str, Any]) -> dict[str, Any] | None:
    raw = owned_files.read_json(_package_receipt_path(component_id))
    if raw is None:
        return None
    package = component.get("package")
    if (not isinstance(raw, dict) or set(raw) != {"schema", "component", "source", "name", "version"}
            or raw.get("schema") != 1 or raw.get("component") != component_id
            or not isinstance(package, dict) or raw.get("source") != package.get("source")
            or raw.get("name") != package.get("name") or not isinstance(raw.get("version"), str)):
        raise BootstrapError(f"package ownership receipt is invalid: {component_id}")
    return raw


def package_owned(
    component_id: str,
    component: dict[str, Any],
    observed: str | None = None,
    codex_home: Path | None = None,
) -> bool:
    if component.get("delivery") == "client":
        return local_clients.receipt(component_id, component) is not None and local_clients.inspect(component_id, component)[0] in {"match", "outdated"}
    receipt = _package_receipt(component_id, component)
    if receipt is None:
        return False
    identity = package_identity(component)
    if observed is None:
        observed = probe_component(component, codex_home)[1]
    if observed is not None and receipt["version"] == observed:
        return True
    package = component["package"]
    return (package.get("source") in {"official", "aur"}
            and _version(component, identity["command"]) == receipt["version"])


def package_lock_migration(component_id: str, component: dict[str, Any]) -> dict[str, str] | None:
    """Map only receipts whose owner source also identifies the catalog delivery."""
    package = component.get("package")
    receipt = _package_receipt(component_id, component)
    if not isinstance(package, dict) or receipt is None:
        return None
    source = package.get("source")
    lock_source = component.get("lock_source")
    manager = {"npm": "npm", "uv": "uv", "official": "pacman", "aur": "pacman"}.get(source)
    private_release = package.get("asset", {}).get("private") is True
    release_lock_source = (f"github-release:{package['asset'].get('repo')}:{package['asset'].get('name_prefix')}*"
                           f"{package['asset'].get('suffix')}" if private_release else None)
    npm_lock_source_valid = (isinstance(lock_source, str)
                             and (lock_source.startswith(f"npm:{package['name']}:")
                                  or private_release and lock_source == release_lock_source))
    if (manager is None or receipt["source"] != source or receipt["name"] != package.get("name")
            or not isinstance(lock_source, str)
            or source == "npm" and not npm_lock_source_valid
            or source == "uv" and lock_source != f"pypi:{package['name']}"
            or source == "official" and not lock_source.startswith("arch:")
            or source == "aur" and lock_source != f"aur:{package['name']}"):
        return None
    try:
        identity = package_identity(component)
    except BootstrapError:
        return None
    command_version = _version(component, identity["command"])
    if (identity["manager"] != manager or identity["name"] != package["name"]
            or receipt["version"] not in {identity["version"], command_version}):
        return None
    return {"source": lock_source, "version": identity["version"]}


def _write_package_receipt(component_id: str, component: dict[str, Any], observed: str) -> None:
    package_identity(component)
    package = component["package"]
    path = _package_receipt_path(component_id)
    before = owned_files.read(path, private=True)
    content = owned_files.json_bytes({
        "schema": 1,
        "component": component_id,
        "source": package["source"],
        "name": package["name"],
        "version": observed,
    })
    owned_files.replace(path, content, before)


def _remove_package_receipt(component_id: str) -> None:
    path = _package_receipt_path(component_id)
    before = owned_files.read(path, private=True)
    if before is not None:
        owned_files.replace(path, None, before)


def _ensure_npm_prefix(metadata: dict[str, Any]) -> None:
    if metadata.get("source") != "npm":
        return
    npm = shutil.which("npm")
    if not npm:
        raise BootstrapError("npm is unavailable")
    prefix = str(_prefix({"package": metadata}) or "")
    result = subprocess.run([npm, "config", "get", "prefix", "--location=user"], capture_output=True, text=True, check=False)
    if result.returncode:
        raise BootstrapError("cannot read npm user prefix")
    if result.stdout.strip() != prefix:
        result = subprocess.run([npm, "config", "set", "prefix", prefix, "--location=user"], check=False)
        if result.returncode:
            raise BootstrapError("cannot set npm user prefix")
        result = subprocess.run([npm, "config", "get", "prefix", "--location=user"], capture_output=True, text=True, check=False)
        if result.returncode or result.stdout.strip() != prefix:
            raise BootstrapError("npm user prefix verification failed")


def _plugin_operation(component_id: str, component: dict[str, Any], codex_home: Path, operation: str) -> str:
    plugin = component["plugin"]
    codex = shutil.which("codex")
    if not codex:
        raise BootstrapError("Codex CLI is unavailable")
    env = {**os.environ, "CODEX_HOME": str(codex_home)}
    state, _ = _plugin_status(component, codex_home)
    if operation == "uninstall":
        if state == "missing":
            return "no-op"
        if not _plugin_owned(component_id, component, codex_home):
            raise BootstrapError("refusing external or drifted Codex plugin uninstall")
        result = subprocess.run([codex, "plugin", "remove", plugin["id"], "--json"], check=False, env=env)
        if result.returncode:
            raise BootstrapError("Codex plugin removal failed")
        if _plugin_status(component, codex_home)[0] != "missing":
            raise BootstrapError("Codex plugin removal postcondition failed")
        _remove_plugin_receipt(component_id)
        return "changed"
    if state == "match" and operation != "upgrade":
        if _plugin_owned(component_id, component, codex_home):
            return "no-op"
        raise BootstrapError("Codex plugin must pass the dedicated adoption gate")
    if state != "missing" and not _plugin_owned(component_id, component, codex_home):
        raise BootstrapError("refusing external or drifted Codex plugin update")
    target = component.get("_candidate_target") or component.get("_lock_target")
    marketplace = ({**plugin["marketplace"], "ref": target.get("ref")}
                   if isinstance(target, dict) else plugin["marketplace"])
    if not isinstance(marketplace.get("ref"), str):
        raise BootstrapError("Codex plugin requires a locked marketplace commit")
    _prepare_plugin_marketplace(codex, codex_home, marketplace)
    result = subprocess.run([codex, "plugin", "marketplace", "add", marketplace["source"], "--ref", marketplace["ref"], "--json"], check=False, env=env)
    if result.returncode:
        raise BootstrapError("Codex marketplace registration failed")
    result = subprocess.run([codex, "plugin", "add", plugin["id"], "--json"], check=False, env=env)
    if result.returncode:
        raise BootstrapError("Codex plugin installation failed")
    if _plugin_status(component, codex_home)[0] != "match":
        raise BootstrapError("Codex plugin version/source postcondition failed")
    snapshot = _plugin_snapshot(component, codex_home)
    if snapshot is None or snapshot["version"] != (target or {}).get("version") or snapshot["ref"] != (target or {}).get("ref"):
        raise BootstrapError("Codex plugin exact target verification failed")
    _write_plugin_receipt(component_id, component, snapshot)
    return "changed"


def component_operation(args: Any, catalog: dict[str, Any], key: str, component: dict[str, Any], operation: str) -> str:
    codex_home = args.codex_home
    candidate = getattr(args, "candidate", None)
    if component.get("delivery") == "client":
        return local_clients.operate(key, component, operation,
            adopt=bool(getattr(args, "kconf", None) and args.kconf.syms["PENNIX_ADOPT_EXISTING_PACKAGES"].tri_value),
            expected_sha256=getattr(args, "expected_sha256", None), candidate=candidate)
    if isinstance(component.get("plugin"), dict):
        return _plugin_operation(key, component, codex_home, operation)
    if component.get("delivery") == "collection":
        return _operate_collection(component, codex_home, operation, candidate=candidate)
    if component.get("delivery") == "native":
        if operation != "uninstall":
            raise BootstrapError(f"{key} is owned by its native installer")
        raise BootstrapError(f"{key} cannot be uninstalled by lifecycle")
    metadata = component.get("package")
    if not isinstance(metadata, dict):
        raise BootstrapError(f"{key} has no package owner")
    current_host = host.detect_host()
    if not current_host["supported"]:
        raise BootstrapError(str(current_host["reason"]))
    state, observed = probe_component(component, codex_home)
    owned = package_owned(key, component, observed, codex_home)
    if operation == "uninstall":
        if state == "missing":
            return "no-op"
        if state not in {"match", "outdated"} or not owned:
            raise BootstrapError(f"refusing external or drifted package uninstall: {key}")
        installer = current_host["installers"].get(metadata.get("source"))
        if not isinstance(installer, str):
            raise BootstrapError(f"no installer for {key}")
        result = subprocess.run(host.package_remove_command(installer, metadata["name"], metadata.get("prefix")), check=False)
        if result.returncode:
            raise BootstrapError(f"package uninstall failed: {key}")
        _remove_package_receipt(key)
        return "changed"
    _ensure_npm_prefix(metadata)
    if state != "missing":
        package_identity(component)
        if not owned:
            kconf = getattr(args, "kconf", None)
            adopted = bool(kconf and kconf.syms.get("PENNIX_ADOPT_EXISTING_PACKAGES")
                          and kconf.syms["PENNIX_ADOPT_EXISTING_PACKAGES"].tri_value)
            if not adopted:
                raise BootstrapError(f"package is external; enable explicit adoption first: {key}")
            _write_package_receipt(key, component, observed or "")
            owned = True
            adopted_now = True
        else:
            adopted_now = False
        kconf = getattr(args, "kconf", None)
        if state == "match" and not getattr(args, "explicit_update", False):
            return "adopted" if adopted_now else "no-op"
        if not getattr(args, "explicit_update", False):
            raise BootstrapError(f"package updates require pennix update: {key}")
    installer = current_host["installers"].get(metadata.get("source"))
    if not isinstance(installer, str):
        raise BootstrapError(f"no installer for {key}")
    package_name = metadata["name"]
    expected = _target_version(component)
    install_name = f"{package_name}@{expected}" if metadata.get("source") == "npm" and expected else package_name
    if metadata.get("source") == "uv" and expected:
        install_name = f"{package_name}=={expected}"
    with tempfile.TemporaryDirectory(prefix="pennix-package-") as directory:
        candidate = getattr(args, "candidate", None)
        package_file = None
        if candidate:
            install_name = candidate["path"]
            if metadata.get("source") == "official":
                installer = "pacman"
                package_file = Path(candidate["path"])
            elif metadata.get("source") == "aur":
                source_tree = Path(candidate["path"])
                build = subprocess.run(
                    ["makepkg", "--cleanbuild", "--force", "--noconfirm"], cwd=source_tree,
                    env={**os.environ, "BUILDDIR": str(source_tree), "PKGDEST": str(source_tree),
                         "SRCDEST": str(source_tree / "sources")},
                    capture_output=True, text=True, timeout=1800, check=False,
                )
                if build.returncode:
                    raise BootstrapError(f"fixed AUR candidate build failed: {key}")
                artifacts = sorted(source_tree.glob(f"{metadata['name']}-*.pkg.tar.*"))
                if len(artifacts) != 1:
                    raise BootstrapError(f"fixed AUR candidate did not produce one package: {key}")
                identity = subprocess.run(["pacman", "-Qp", str(artifacts[0])], capture_output=True,
                                          text=True, timeout=30, check=False)
                fields = identity.stdout.split()
                if (identity.returncode or len(fields) != 2 or fields[0] != metadata["name"]
                        or fields[1] != expected):
                    raise BootstrapError(f"fixed AUR package identity mismatch: {key}")
                installer = "pacman"
                package_file = artifacts[0]
        asset = metadata.get("asset")
        if asset and not candidate:
            path = Path(directory) / "package.tgz"
            if asset.get("private"):
                downloaded = subprocess.run(["gh", "release", "download", asset["tag"], "--repo", asset["repo"],
                                             "--pattern", asset["name"], "--output", str(path)],
                                            capture_output=True, timeout=120, check=False)
                if downloaded.returncode:
                    raise BootstrapError("private package asset unavailable; verify native gh authentication")
            else:
                owned_files.download(asset["url"], path)
            if hashlib.sha256(path.read_bytes()).hexdigest() != asset["sha256"]:
                raise BootstrapError("package asset SHA-256 mismatch")
            install_name = str(path)
        install_command = (["sudo", "pacman", "-U", "--needed", "--noconfirm", str(package_file)]
                           if package_file is not None else
                           host.package_install_command(installer, install_name, metadata.get("registry"),
                                                        metadata.get("prefix"), metadata.get("allow_scripts")))
        result = subprocess.run(install_command, check=False)
    if result.returncode:
        raise BootstrapError(f"package install failed: {key}")
    new_state, _ = probe_component(component, codex_home)
    if new_state != "match":
        raise BootstrapError(f"package postcondition failed: {key}")
    _, new_observed = probe_component(component, codex_home)
    if new_observed is None:
        raise BootstrapError(f"package version cannot be recorded: {key}")
    _write_package_receipt(key, component, new_observed)
    return "changed"


def static_operation(args: Any, key: str, component: dict[str, Any], operation: str, catalog: dict[str, Any] | None = None) -> str:
    if key == "codex-config":
        raise BootstrapError("codex-config is owned by the TOML writer")
    if component.get("delivery") == "collection":
        return _operate_collection(component, args.codex_home, operation)
    return _operate_static(key, args.codex_home, component, operation, getattr(args, "expected_sha256", None))
