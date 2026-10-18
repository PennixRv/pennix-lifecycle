"""Drive the non-Codex connection owners through their native contracts."""
from __future__ import annotations

import json
import hashlib
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import config_model
import secret_store
import native_integrations


class ConnectionError(RuntimeError):
    """A connection owner is unavailable or rejected a lifecycle operation."""


def _command(spec: dict[str, Any], codex_home: Path) -> list[str]:
    raw = spec.get("command")
    if not isinstance(raw, list) or not raw or any(not isinstance(item, str) or not item for item in raw):
        raise ConnectionError("owner command contract is invalid")
    values = [item.replace("$CODEX_HOME", str(codex_home)).replace("$HOME", str(Path.home())) for item in raw]
    executable = shutil.which(values[0]) if not os.path.isabs(values[0]) else values[0]
    if not executable:
        raise ConnectionError(f"owner command unavailable: {values[0]}")
    if not os.path.isfile(executable) or any(os.path.isabs(item) and not Path(item).is_file() for item in values[1:]):
        raise ConnectionError("owner command or script unavailable")
    values[0] = executable
    return values


def _secret_payload(spec: dict[str, Any], secrets_path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    fields = spec.get("secrets", {})
    if not isinstance(fields, dict):
        raise ConnectionError("owner secret contract is invalid")
    for name, reference in fields.items():
        if not isinstance(name, str) or not isinstance(reference, str) or "." not in reference:
            raise ConnectionError("owner secret reference is invalid")
        table, field = reference.split(".", 1)
        result[name] = secret_store.get(secrets_path, table, field)
    return result


def _values(spec: dict[str, Any], kconf: Any) -> dict[str, str]:
    result: dict[str, str] = {}
    fields = spec.get("values", {})
    if not isinstance(fields, dict):
        raise ConnectionError("owner value contract is invalid")
    for name, key in fields.items():
        if not isinstance(name, str) or not isinstance(key, str):
            raise ConnectionError("owner value reference is invalid")
        value = config_model.config_value(kconf, key)
        if not value.strip():
            raise ConnectionError(f"缺少连接配置项: {key}")
        result[name] = value
    return result


def _run(command: list[str], action: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        result = subprocess.run(
            [*command, "lifecycle", action, "--json"],
            input=json.dumps(payload or {}, ensure_ascii=False),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ConnectionError(f"连接 owner {action} 未完成") from error
    if result.returncode:
        raise ConnectionError(f"连接 owner {action} 拒绝操作（退出码 {result.returncode}）")
    try:
        value = json.loads(result.stdout)
    except (TypeError, ValueError) as error:
        raise ConnectionError(f"连接 owner {action} 回执不是有效 JSON") from error
    if not isinstance(value, dict) or not isinstance(value.get("state"), str) or not isinstance(value.get("owned"), bool):
        raise ConnectionError(f"连接 owner {action} 回执格式无效")
    return value


def _desired_digest(target: str, values: dict[str, str], secrets: dict[str, str]) -> str:
    canonical = json.dumps(
        {"target": target, "values": values, "secrets": secrets},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def target_specs(catalog: dict[str, Any]) -> dict[str, dict[str, Any]]:
    value = catalog.get("configuration_targets")
    if not isinstance(value, dict):
        raise ConnectionError("catalog 缺少 configuration_targets")
    for target, spec in value.items():
        if not isinstance(target, str) or not isinstance(spec, dict):
            raise ConnectionError("configuration target contract is invalid")
        for key in ("select", "owner", "component", "command", "values", "secrets"):
            if key not in spec:
                raise ConnectionError(f"configuration target {target} 缺少 {key}")
    return value


def inspect_target(
    target: str,
    spec: dict[str, Any],
    codex_home: Path,
    desired_digest: str | None = None,
) -> dict[str, Any]:
    try:
        payload: dict[str, Any] = {"target": target}
        if desired_digest is not None:
            payload["desired_digest"] = desired_digest
        return {"target": target, **_run(_command(spec, codex_home), "inspect", payload)}
    except ConnectionError as error:
        return {"target": target, "state": "blocked", "owned": False, "detail": str(error)}


def plan(kconf: Any, catalog: dict[str, Any], codex_home: Path, secrets_path: Path, *, installing: set[str] | None = None) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for target, spec in target_specs(catalog).items():
        selected = config_model.config_value(kconf, spec["select"]) == "y"
        desired: tuple[dict[str, str], dict[str, str]] | None = None
        if selected:
            try:
                values = _values(spec, kconf)
                secrets = _secret_payload(spec, secrets_path)
                desired = (values, secrets)
            except (ConnectionError, secret_store.SecretError) as error:
                status = inspect_target(target, spec, codex_home)
                status.update(state="blocked", detail=str(error))
            else:
                try:
                    _command(spec, codex_home)
                except ConnectionError:
                    status = ({"target": target, "state": "pending-owner", "owned": False,
                               "detail": "输入已预检；安装 owner 后重新核验"}
                              if spec["component"] in (installing or set()) else
                              inspect_target(target, spec, codex_home))
                else:
                    status = inspect_target(target, spec, codex_home, _desired_digest(target, *desired))
                    if status["state"] == "blocked" and spec["component"] in (installing or set()):
                        status = {"target": target, "state": "pending-owner", "owned": False,
                                  "detail": "输入已预检；更新 owner 后重新核验其原生合同"}
        else:
            status = inspect_target(target, spec, codex_home)
        if target == "siyuan":
            try:
                mcp = native_integrations.inspect("siyuan", codex_home, enabled=selected)
                status["mcp_state"] = mcp["state"]
                if selected and status["state"] not in {"blocked", "invalid", "external", "pending-owner"}:
                    if mcp["state"] in {"blocked", "drifted"} or (mcp["state"] == "external" and not (
                            config_model.config_value(kconf, "PENNIX_ADOPT_EXISTING_PACKAGES") == "y"
                            and native_integrations.can_adopt("siyuan", codex_home))):
                        status.update(state="blocked", detail="SiYuan MCP 配置漂移或无法安全接管")
                    elif status["state"] == "current" and mcp["state"] != "current":
                        status["state"] = "configuration-required"
                status["owned"] = bool(status.get("owned") or mcp.get("owned"))
            except (native_integrations.NativeIntegrationError, OSError) as error:
                status.update(state="blocked", detail=str(error))
        if selected:
            action = "keep" if status["state"] == "current" else "configure"
            if status["state"] in {"blocked", "invalid", "external", "environment-override"}:
                action = "blocked"
            if status["state"] == "adoptable" and config_model.config_value(kconf, "PENNIX_ADOPT_EXISTING_PACKAGES") != "y":
                action = "blocked"
        else:
            action = "unconfigure" if status.get("owned") else "keep"
        result[target] = {"selected": selected, "action": action, **status}
    return result


def apply_target(target: str, spec: dict[str, Any], kconf: Any, codex_home: Path, secrets_path: Path, action: str) -> str:
    if action == "keep":
        return "no-op"
    command = _command(spec, codex_home)
    if action == "unconfigure":
        if target == "siyuan":
            native_integrations.unconfigure("siyuan", codex_home)
        payload = {"target": target}
    elif action == "configure":
        values = _values(spec, kconf)
        secrets = _secret_payload(spec, secrets_path)
        payload = {
            "target": target,
            "values": values,
            "secrets": secrets,
            "desired_digest": _desired_digest(target, values, secrets),
            "adopt_existing": config_model.config_value(kconf, "PENNIX_ADOPT_EXISTING_PACKAGES") == "y",
        }
    else:
        raise ConnectionError(f"unsupported connection action: {action}")
    value = _run(command, action, payload)
    expected_state = "current" if action == "configure" else "absent"
    if value["state"] != expected_state or (action == "configure" and not value["owned"]):
        raise ConnectionError(
            f"连接 owner {action} 未达到后置条件: {value.get('state')}"
        )
    if target == "siyuan" and action == "configure":
        native_integrations.apply("siyuan", codex_home,
            adopt=config_model.config_value(kconf, "PENNIX_ADOPT_EXISTING_PACKAGES") == "y")
    return "changed"


def functional_checks(catalog: dict[str, Any], connections: dict, codex_home: Path) -> dict:
    """Run only explicitly requested native probes; never expose their response bodies."""
    result = {}
    with tempfile.TemporaryDirectory(prefix="pennix-functional-") as directory:
        project = Path(directory)
        (project / "example.py").write_text("def lifecycle_probe():\n    return 'ok'\n")
        for target, status in connections.items():
            if not status["selected"]:
                continue
            command = catalog["configuration_targets"][target].get("functional_command")
            if status["action"] != "keep" or not command:
                result[target] = {"state": "blocked", "detail": "本地未就绪或 owner 未声明功能核验入口"}
                continue
            try:
                command = _command({"command": command}, codex_home)
                command = [value.replace("$VERIFY_PROJECT", directory) for value in command]
                completed = subprocess.run(command, cwd=project, capture_output=True, timeout=300, check=False)
                result[target] = {"state": "passed" if completed.returncode == 0 else "failed",
                                  "exit_code": completed.returncode}
            except (ConnectionError, OSError, subprocess.TimeoutExpired):
                result[target] = {"state": "failed", "detail": "owner 功能核验未完成"}
    return result
