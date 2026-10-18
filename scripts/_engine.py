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
import skill_collections
import owned_files


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
        if component.get("delivery") not in {"package", "plugin", "native", "static", "collection", "state"}:
            raise BootstrapError(f"catalog delivery is invalid: {key}")
        if component.get("delivery") != "state" and not isinstance(component.get("actions"), dict):
            raise BootstrapError(f"catalog actions are missing: {key}")
        if component.get("delivery") == "collection":
            contract = component.get("collection_contract")
            if not isinstance(contract, dict) or set(contract) != {"directory", "source"}:
                raise BootstrapError(f"collection manifest contract is invalid: {key}")
            source = contract["source"]
            if not isinstance(source, dict) or set(source) != {"repo", "ref", "manifest"}:
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


def _operate_collection(component: dict[str, Any], codex_home: Path, operation: str) -> str:
    try:
        return skill_collections.operate(component, codex_home, operation)
    except (skill_collections.CollectionError, OSError, subprocess.TimeoutExpired) as error:
        raise BootstrapError(str(error)) from error


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
    expected = _target_version(component)
    policy = component.get("version_policy", "pinned")
    state = "match" if policy == "repository-latest" or expected is None or observed == expected else "drifted"
    return state, observed


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
    receipt = _package_receipt(component_id, component)
    if receipt is None:
        return False
    if observed is None:
        observed = probe_component(component, codex_home)[1]
    return observed is not None and receipt["version"] == observed


def _write_package_receipt(component_id: str, component: dict[str, Any], observed: str) -> None:
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
    state, observed = probe_component(component, codex_home)
    owned = package_owned(key, component, observed, codex_home)
    if operation == "uninstall":
        if state == "missing":
            return "no-op"
        if state != "match" or not owned:
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
    if state == "match":
        if not owned:
            kconf = getattr(args, "kconf", None)
            adopted = bool(kconf and kconf.syms.get("PENNIX_ADOPT_EXISTING_PACKAGES")
                          and kconf.syms["PENNIX_ADOPT_EXISTING_PACKAGES"].tri_value)
            if not adopted:
                raise BootstrapError(f"package is external; enable explicit adoption first: {key}")
            _write_package_receipt(key, component, observed or "")
            return "adopted"
        if operation == "install":
            return "no-op"
        if operation == "upgrade":
            kconf = getattr(args, "kconf", None)
            if not kconf or not kconf.syms["PENNIX_UPDATE_COMPONENTS"].tri_value:
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
