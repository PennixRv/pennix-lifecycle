"""Owner operations for the versioned lifecycle tool."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import host


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
    if not isinstance(value, dict) or value.get("schema") != 2 or not isinstance(value.get("components"), dict):
        raise BootstrapError("catalog schema 2 is required")
    for key, component in value["components"].items():
        if not isinstance(key, str) or not isinstance(component, dict):
            raise BootstrapError("catalog component is invalid")
        if component.get("delivery") not in {"package", "plugin", "native", "static", "collection", "state"}:
            raise BootstrapError(f"catalog delivery is invalid: {key}")
        if component.get("delivery") != "state" and not isinstance(component.get("actions"), dict):
            raise BootstrapError(f"catalog actions are missing: {key}")
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
    value = component.get("approved_version")
    return normalize_version(str(value)) if isinstance(value, str) else None


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
    revision = component["template"].get("revision")
    if isinstance(revision, str) and revision.startswith("sha256:"):
        digest = "sha256:" + hashlib.sha256(value.encode()).hexdigest()
        if digest != revision:
            raise BootstrapError(f"static template hash mismatch: {name}")
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


def _static_state(component_id: str, codex_home: Path, component: dict[str, Any]) -> str:
    path = _static_target(component_id, codex_home)
    current = _read(path)
    if component_id == "codex-agents":
        if not current:
            return "missing"
        return "current" if current == _template(component) else "drifted"
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


def _operate_static(component_id: str, codex_home: Path, component: dict[str, Any], operation: str) -> str:
    path = _static_target(component_id, codex_home)
    state = _static_state(component_id, codex_home, component)
    if component_id == "codex-agents":
        if operation == "uninstall":
            if state == "missing":
                return "no-op"
            if state != "current":
                raise BootstrapError(f"refusing {state} AGENTS.md")
            path.unlink()
            return "changed"
        if state == "current":
            return "no-op"
        if state != "missing":
            raise BootstrapError(f"refusing {state} AGENTS.md")
        _write(path, _template(component))
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
    contract = component.get("collection_contract")
    name = contract.get("directory", component.get("adapter")) if isinstance(contract, dict) else None
    if not isinstance(name, str) or not name:
        raise BootstrapError("collection directory is missing")
    return codex_home / "skills" / name


def _collection_state_path() -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")).expanduser()
    return base / "pennix-lifecycle" / "collections.json"


def _collection_names(component: dict[str, Any]) -> set[str]:
    contract = component.get("collection_contract")
    names = contract.get("skills") if isinstance(contract, dict) else None
    if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
        raise BootstrapError("collection skills are missing")
    return set(names)


def _collection_key(component: dict[str, Any]) -> str:
    key = component.get("adapter")
    if not isinstance(key, str) or not key:
        raise BootstrapError("collection adapter is missing")
    return key


def _collection_manifest() -> dict[str, Any]:
    path = _collection_state_path()
    if not path.exists():
        return {"schema": 1, "collections": {}}
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise BootstrapError("collection ownership record is unsafe")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BootstrapError("collection ownership record is invalid") from error
    if not isinstance(value, dict) or value.get("schema") != 1 or not isinstance(value.get("collections"), dict):
        raise BootstrapError("collection ownership record version is unsupported")
    return value


def _write_collection_manifest(value: dict[str, Any]) -> None:
    path = _collection_state_path()
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    if path.parent.is_symlink() or not path.parent.is_dir():
        raise BootstrapError("collection ownership directory is unsafe")
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def collection_owned(component: dict[str, Any], codex_home: Path) -> bool:
    destination = _collection_destination(component, codex_home)
    record = _collection_manifest().get("collections", {}).get(_collection_key(component))
    return (
        isinstance(record, dict)
        and record.get("destination") == str(destination)
        and record.get("skills") == sorted(_collection_names(component))
    )


def _collection_state(component: dict[str, Any], codex_home: Path) -> str:
    destination = _collection_destination(component, codex_home)
    names = _collection_names(component)
    if not destination.exists():
        return "missing"
    if destination.is_symlink() or not destination.is_dir():
        return "blocked"
    present = {item.name for item in destination.iterdir() if item.is_dir()}
    if names <= present:
        return "match"
    return "partial"


def _git_tree(source: dict[str, Any], paths: list[str]) -> Path:
    repo = source.get("repo")
    ref = source.get("ref")
    if not isinstance(repo, str) or not isinstance(ref, str):
        raise BootstrapError("collection source is incomplete")
    staging = Path(tempfile.mkdtemp(prefix="pennix-collection-"))
    try:
        result = subprocess.run(
            ["git", "clone", "--filter=blob:none", "--no-checkout", f"https://github.com/{repo}.git", str(staging)],
            capture_output=True, text=True, timeout=180, check=False,
        )
        if result.returncode:
            raise BootstrapError("collection source clone failed")
        result = subprocess.run(["git", "-C", str(staging), "checkout", "--detach", ref], capture_output=True, text=True, timeout=60, check=False)
        if result.returncode:
            raise BootstrapError("collection source ref is unavailable")
        return staging
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _operate_collection(component: dict[str, Any], codex_home: Path, operation: str) -> str:
    state = _collection_state(component, codex_home)
    destination = _collection_destination(component, codex_home)
    names = _collection_names(component)
    if operation == "uninstall":
        if state == "missing":
            return "no-op"
        if state != "match":
            raise BootstrapError(f"refusing {state} collection")
        if not collection_owned(component, codex_home):
            return "preserved"
        for name in names:
            target = destination / name
            if target.is_symlink() or not target.is_dir():
                raise BootstrapError(f"collection target is unsafe: {name}")
            shutil.rmtree(target)
        try:
            destination.rmdir()
        except OSError:
            pass
        manifest = _collection_manifest()
        manifest["collections"].pop(_collection_key(component), None)
        if manifest["collections"]:
            _write_collection_manifest(manifest)
        else:
            _collection_state_path().unlink(missing_ok=True)
        return "changed"
    if state == "match":
        return "no-op"
    if state != "missing":
        raise BootstrapError(f"refusing {state} collection")
    contract = component["collection_contract"]
    source = contract["source"]
    staging = _git_tree(source, source.get("paths", []))
    temporary_destination: Path | None = None
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.parent.is_symlink() or not destination.parent.is_dir():
            raise BootstrapError("collection parent is unsafe")
        temporary_destination = Path(tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent))
        for relative in source["paths"]:
            source_path = staging / relative
            name = Path(relative).name
            if name not in names or not source_path.is_dir():
                raise BootstrapError(f"collection source path is invalid: {relative}")
            shutil.copytree(source_path, temporary_destination / name)
        if destination.exists() or destination.is_symlink():
            raise BootstrapError("collection destination changed during install")
        os.replace(temporary_destination, destination)
        temporary_destination = None
    finally:
        if temporary_destination is not None:
            shutil.rmtree(temporary_destination, ignore_errors=True)
        shutil.rmtree(staging, ignore_errors=True)
    if _collection_state(component, codex_home) != "match":
        raise BootstrapError("collection postcondition failed")
    manifest = _collection_manifest()
    manifest["collections"][_collection_key(component)] = {
        "destination": str(destination),
        "skills": sorted(names),
        "source_ref": source.get("ref"),
    }
    _write_collection_manifest(manifest)
    return "changed"


def _plugin_status(component: dict[str, Any], codex_home: Path) -> tuple[str, str | None]:
    plugin = component.get("plugin")
    if not isinstance(plugin, dict):
        return "blocked", None
    codex = shutil.which("codex")
    if not codex:
        return "missing", None
    result = subprocess.run([codex, "plugin", "list", "--json"], capture_output=True, text=True, check=False)
    if result.returncode:
        return "blocked", None
    try:
        installed = json.loads(result.stdout).get("installed", [])
    except json.JSONDecodeError:
        return "blocked", None
    for item in installed:
        if isinstance(item, dict) and item.get("pluginId") == plugin.get("id"):
            return ("match" if item.get("enabled") is True else "drifted"), str(item.get("version", ""))
    return "missing", None


def probe_component(component: dict[str, Any], codex_home: Path | None = None, destination: str | None = None, home_directory: Path | None = None) -> tuple[str, str | None]:
    codex_home = codex_home or Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    delivery = component.get("delivery")
    adapter = component.get("adapter")
    if adapter in {"codex-agents", "tmux-config"}:
        state = _static_state(adapter, codex_home, component)
        return {"missing": "missing", "current": "match"}.get(state, "blocked" if state == "blocked" else "drifted"), state
    if delivery == "collection":
        state = _collection_state(component, codex_home)
        return {"missing": "missing", "match": "match"}.get(state, "drifted" if state == "partial" else state), str(_collection_destination(component, codex_home))
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
    expected = _target_version(component)
    policy = component.get("version_policy", "pinned")
    state = "match" if policy == "repository-latest" or expected is None or observed == expected else "drifted"
    return state, observed


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


def _plugin_operation(component: dict[str, Any], codex_home: Path, operation: str) -> str:
    plugin = component["plugin"]
    codex = shutil.which("codex")
    if not codex:
        raise BootstrapError("Codex CLI is unavailable")
    state, _ = _plugin_status(component, codex_home)
    if operation == "uninstall":
        if state == "missing":
            return "no-op"
        result = subprocess.run([codex, "plugin", "remove", plugin["id"], "--json"], check=False)
        if result.returncode:
            raise BootstrapError("Codex plugin removal failed")
        return "changed"
    if state == "match":
        return "no-op"
    marketplace = plugin["marketplace"]
    result = subprocess.run([codex, "plugin", "marketplace", "add", marketplace["source"], "--ref", marketplace["ref"], "--json"], check=False)
    if result.returncode:
        raise BootstrapError("Codex marketplace registration failed")
    result = subprocess.run([codex, "plugin", "add", plugin["id"], "--json"], check=False)
    if result.returncode:
        raise BootstrapError("Codex plugin installation failed")
    return "changed"


def component_operation(args: Any, catalog: dict[str, Any], key: str, component: dict[str, Any], operation: str) -> str:
    codex_home = args.codex_home
    if isinstance(component.get("plugin"), dict):
        return _plugin_operation(component, codex_home, operation)
    if component.get("delivery") == "collection":
        return _operate_collection(component, codex_home, operation)
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
    state, _ = probe_component(component, codex_home)
    if operation == "uninstall":
        if state == "missing":
            return "no-op"
        if state != "match":
            raise BootstrapError(f"refusing {state} package uninstall: {key}")
        installer = current_host["installers"].get(metadata.get("source"))
        if not isinstance(installer, str):
            raise BootstrapError(f"no installer for {key}")
        result = subprocess.run(host.package_remove_command(installer, metadata["name"], metadata.get("prefix")), check=False)
        if result.returncode:
            raise BootstrapError(f"package uninstall failed: {key}")
        return "changed"
    _ensure_npm_prefix(metadata)
    if state == "match" and operation == "install":
        return "no-op"
    installer = current_host["installers"].get(metadata.get("source"))
    if not isinstance(installer, str):
        raise BootstrapError(f"no installer for {key}")
    package_name = metadata["name"]
    expected = _target_version(component)
    install_name = f"{package_name}@{expected}" if metadata.get("source") == "npm" and expected else package_name
    result = subprocess.run(
        host.package_install_command(installer, install_name, metadata.get("registry"), metadata.get("prefix"), metadata.get("allow_scripts")),
        check=False,
    )
    if result.returncode:
        raise BootstrapError(f"package install failed: {key}")
    new_state, _ = probe_component(component, codex_home)
    if new_state != "match":
        raise BootstrapError(f"package postcondition failed: {key}")
    return "changed"


def static_operation(args: Any, key: str, component: dict[str, Any], operation: str, catalog: dict[str, Any] | None = None) -> str:
    if key == "codex-config":
        raise BootstrapError("codex-config is owned by the TOML writer")
    if component.get("delivery") == "collection":
        return _operate_collection(component, args.codex_home, operation)
    return _operate_static(key, args.codex_home, component, operation)
