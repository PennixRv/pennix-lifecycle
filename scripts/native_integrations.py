"""Ownership-aware adapters for native MCP integrations and shared hooks."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tomllib
import tempfile
import tomlkit

import owned_files
import hook_integrations
import context_mode_integration as context_mode
import sys


NativeIntegrationError = context_mode.ContextModeError


EVENTS = {"PreToolUse", "PostToolUse", "SessionStart", "PreCompact", "UserPromptSubmit", "Stop"}


def _receipt_path(key: str) -> Path:
    return owned_files.state_root() / "native" / f"{key}.json"


def _digest(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _toml(path: Path) -> dict:
    raw = owned_files.read(path)
    try:
        return tomllib.loads(raw.decode("utf-8")) if raw is not None else {}
    except (UnicodeError, tomllib.TOMLDecodeError) as error:
        raise NativeIntegrationError(f"native owner 配置不是有效 TOML: {path}") from error


def _hooks(codex_home: Path) -> tuple[dict, bytes | None]:
    try:
        return hook_integrations.read_document(codex_home)
    except hook_integrations.HookError as error:
        raise NativeIntegrationError(str(error)) from error


def _marked(entry: object) -> bool:
    return isinstance(entry, dict) and isinstance(entry.get("hooks"), list) and any(
        isinstance(hook, dict) and "context-mode" in str(hook.get("command", ""))
        for hook in entry["hooks"]
    )


def _managed_hooks(document: dict) -> dict:
    return {event: selected for event, entries in document.get("hooks", {}).items()
            if (selected := [entry for entry in entries if _marked(entry)])}


def _edit_hooks(codex_home: Path, previous: dict, desired: dict, *, remove_empty: bool = False, dry_run: bool = False) -> None:
    document, before = _hooks(codex_home)
    if _managed_hooks(document) != previous:
        raise NativeIntegrationError("context-mode Hook 条目已漂移或由其他 owner 管理")
    hooks = context_mode.merge_hooks(document.get("hooks", {}), previous, desired)
    # An unrelated trusted handler cannot silently move to a different native key.
    trust = _toml(codex_home / "config.toml").get("hooks", {}).get("state", {})
    events = {**context_mode.EVENTS, "PreCompact": "pre_compact", "Stop": "stop"}
    for event, entries in document.get("hooks", {}).items():
        for index, entry in enumerate(entries):
            if _marked(entry) or event not in events:
                continue
            positions = [i for i, item in enumerate(hooks.get(event, [])) if item == entry]
            if positions == [index]:
                continue
            prefix = f"{codex_home / 'hooks.json'}:{events[event]}:{index}:"
            if any(key.startswith(prefix) for key in trust):
                raise NativeIntegrationError("context-mode 操作会移动其他 owner 的 trusted Hook；保留现场")
    if dry_run:
        return
    document["hooks"] = hooks
    if not hooks:
        document.pop("hooks", None)
    output = None if not document and remove_empty else owned_files.json_bytes(document)
    owned_files.replace(codex_home / "hooks.json", output, before)


def _context_target(codex_home: Path, receipt: dict | None = None, *, configure: bool = False) -> dict:
    path = receipt["mcp"].get("env", {}).get("PATH") if receipt else None
    if path is None and configure:
        path = context_mode.configured_path(codex_home)
    return context_mode.target(codex_home, path)


def _desired(key: str, codex_home: Path, *, enabled: bool, receipt: dict | None = None, configure: bool = False) -> dict:
    if key not in {"codegraph", "context-mode", "siyuan"}:
        raise NativeIntegrationError(f"未知 native integration: {key}")
    value = {"key": key, "enabled": enabled, "codex_home": str(codex_home)}
    if key == "context-mode":
        value.update(_context_target(codex_home, receipt, configure=configure))
    elif key == "codegraph":
        value["mcp"] = {"command": _command(key), "args": ["serve", "--mcp"]}
    else:
        helper = Path.home() / ".agents/skills/pennix-skills/pennix-siyuan-knowledge/scripts/mcp_headers.py"
        python = shutil.which("python3")
        if python is None or not helper.is_file():
            raise NativeIntegrationError("SiYuan 本机 helper 不可用")
        result = subprocess.run([python, str(helper), "--metadata"], capture_output=True, text=True, timeout=15, check=False)
        try:
            url = json.loads(result.stdout)["url"]
            if result.returncode or not isinstance(url, str):
                raise ValueError()
        except (ValueError, KeyError, TypeError) as error:
            raise NativeIntegrationError("SiYuan 本机连接 metadata 不可用") from error
        value["mcp"] = {"url": url, "http_headers_helper": shlex.join([python, str(helper)])}
    return value


def _read_receipt(key: str) -> dict | None:
    value = owned_files.read_json(_receipt_path(key))
    if value is None:
        return None
    if key == "context-mode":
        return context_mode.validate_receipt(value)
    fields = {"schema", "key", "codex_home", "desired_digest"}
    if key == "context-mode":
        fields |= {"mcp", "hooks", "hooks_file_created"}
    else:
        fields |= {"mcp", "baseline"}
    if (not isinstance(value, dict) or set(value) != fields or value.get("schema") != 1 or value.get("key") != key
            or not all(isinstance(value.get(name), str) for name in ("codex_home", "desired_digest"))):
        raise NativeIntegrationError(f"{key} native integration receipt 无效")
    if key == "context-mode" and (not isinstance(value["mcp"], dict) or not isinstance(value["hooks"], dict)
                                   or not isinstance(value["hooks_file_created"], bool)):
        raise NativeIntegrationError("context-mode 条目归属记录无效")
    return value


def _write_receipt(key: str, desired: dict, *, previous: bytes | None, hooks_file_created: bool = False, baseline: dict | None = None, trust: dict | None = None) -> None:
    record = {"schema": 1, "key": key, "codex_home": desired["codex_home"], "desired_digest": _digest(desired)}
    if key == "context-mode":
        record.update(schema=2, mcp=desired["mcp"], hooks=desired["hooks"], hooks_file_created=hooks_file_created,
                      trust=trust, skills=desired["skills"])
        if "skill_root" in desired:
            record.update(schema=3, skill_root=desired["skill_root"])
    else:
        record.update(mcp=desired["mcp"], baseline=baseline)
    owned_files.replace(_receipt_path(key), owned_files.json_bytes(record), previous)


def _run(command: list[str], codex_home: Path, timeout: int = 600) -> None:
    try:
        result = subprocess.run(command, env={**os.environ, "CODEX_HOME": str(codex_home)},
                                capture_output=True, text=True, check=False, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise NativeIntegrationError("native owner 未完成") from error
    if result.returncode:
        raise NativeIntegrationError(f"native owner 拒绝操作（退出码 {result.returncode}）: {(result.stderr or result.stdout)[-2000:]}")


def _command(key: str) -> str:
    value = shutil.which("codex" if key == "context-mode" else key)
    if value is None:
        raise NativeIntegrationError(f"{key} native owner 不可用")
    return value


def _server(key: str, codex_home: Path) -> object:
    servers = _toml(codex_home / "config.toml").get("mcp_servers", {})
    if not isinstance(servers, dict):
        raise NativeIntegrationError("mcp_servers 不是 TOML table")
    return servers.get(key)


def _context_present(codex_home: Path) -> bool:
    document, _ = _hooks(codex_home)
    config = _toml(codex_home / "config.toml")
    plugins = config.get("plugins", {})
    if not isinstance(plugins, dict):
        raise NativeIntegrationError("plugins 不是 TOML table")
    plugin_enabled = any("context-mode" in name and isinstance(value, dict) and value.get("enabled", True)
                         for name, value in plugins.items())
    return _server("context-mode", codex_home) is not None or bool(_managed_hooks(document)) or plugin_enabled


def _context_matches(codex_home: Path, record: dict) -> bool:
    return context_mode.matches(sys.modules[__name__], codex_home, record)


def inspect(key: str, codex_home: Path, *, enabled: bool) -> dict[str, object]:
    if key in {"aoe", "peon-ping"}:
        return hook_integrations.inspect(key, codex_home, enabled=enabled)
    if key not in {"context-mode", "codegraph", "siyuan"}:
        raise NativeIntegrationError(f"未知 native integration: {key}")
    receipt = _read_receipt(key)
    present = _context_present(codex_home) if key == "context-mode" else _server(key, codex_home) is not None
    if receipt is None:
        return {"state": "external" if present else "absent", "owned": False, "detail": "native config has no lifecycle receipt" if present else "not configured"}
    if receipt["codex_home"] != str(codex_home):
        return {"state": "blocked", "owned": False, "detail": "native receipt belongs to another CODEX_HOME"}
    current = (_context_matches(codex_home, receipt) if key == "context-mode" else
               _mcp_fields(key, codex_home) == receipt["mcp"])
    if not current:
        return {"state": "drifted", "owned": True, "detail": "native owner configuration changed"}
    if not enabled:
        return {"state": "owned", "owned": True, "detail": "native integration receipt"}
    if key == "context-mode" and "PATH" not in receipt["mcp"].get("env", {}):
        return {"state": "update-required", "owned": True, "detail": "native MCP PATH requires explicit configuration"}
    desired = _desired(key, codex_home, enabled=True, receipt=receipt)
    return {"state": "current" if receipt["desired_digest"] == _digest(desired) else "update-required",
            "owned": True, "detail": "native owner configuration"}


def _mcp_add(codex_home: Path, expected: dict) -> None:
    if _server("context-mode", codex_home) is not None:
        raise NativeIntegrationError("拒绝覆盖已有 context-mode MCP")
    args = [_command("context-mode"), "mcp", "add", "context-mode"]
    for key, value in expected.get("env", {}).items():
        args += ["--env", f"{key}={value}"]
    _run([*args, "--", expected["command"], *expected.get("args", [])], codex_home)
    extras = {key: value for key, value in expected.items() if key not in {"command", "args", "env"}}
    if extras:
        path = codex_home / "config.toml"
        before = owned_files.read(path)
        document = tomlkit.parse(before.decode() if before else "")
        current = document.get("mcp_servers", {}).get("context-mode")
        core = {key: value for key, value in expected.items() if key in {"command", "args", "env"}}
        if current != core:
            raise NativeIntegrationError("native MCP add 结果与计划不一致")
        for key, value in extras.items():
            current[key] = value
        owned_files.replace(path, tomlkit.dumps(document).encode(), before)
    if _server("context-mode", codex_home) != expected:
        raise NativeIntegrationError("native MCP 写入后核验失败")


def _mcp_remove(codex_home: Path, expected: dict) -> None:
    if _server("context-mode", codex_home) != expected:
        raise NativeIntegrationError("context-mode MCP 发生漂移，拒绝移除")
    _run([_command("context-mode"), "mcp", "remove", "context-mode"], codex_home)


def _context_apply(codex_home: Path, desired: dict, receipt: dict | None, *, repair: bool = False) -> str:
    return context_mode.apply(sys.modules[__name__], codex_home, desired, receipt, repair=repair)


def _mcp_fields(key: str, home: Path) -> dict | None:
    server = _server(key, home)
    if server is None:
        return None
    if not isinstance(server, dict):
        raise NativeIntegrationError(f"{key} MCP 不是 table")
    names = ("url", "http_headers_helper") if key == "siyuan" else ("command", "args")
    return {name: server[name] for name in names if name in server}


def can_adopt(key: str, home: Path) -> bool:
    if key in {"aoe", "peon-ping"}:
        return hook_integrations._read(key, home)[1] == hook_integrations.desired(key)
    if key == "codegraph":
        fields = _mcp_fields(key, home)
        return bool(fields and isinstance(fields.get("command"), str)
                    and Path(fields["command"]).name == "codegraph" and fields.get("args") == ["serve", "--mcp"])
    if key == "siyuan":
        current = _mcp_fields(key, home)
        expected = _desired(key, home, enabled=True)["mcp"]
        if current is None:
            return False
        try:
            actual_helper, helper = shlex.split(current["http_headers_helper"]), shlex.split(expected["http_headers_helper"])
            return (current["url"] == expected["url"] and len(actual_helper) == len(helper) == 2
                    and Path(actual_helper[0]).samefile(helper[0]) and actual_helper[1] == helper[1])
        except (OSError, ValueError, TypeError, KeyError):
            return False
    return False


def _mcp_edit(key: str, home: Path, fields: dict | None, expected: dict | None) -> None:
    if _mcp_fields(key, home) != expected:
        raise NativeIntegrationError(f"{key} MCP 在操作期间改变")
    path = home / "config.toml"
    before = owned_files.read(path)
    document = tomlkit.parse(before.decode() if before else "")
    servers = document.setdefault("mcp_servers", tomlkit.table())
    server = servers.setdefault(key, tomlkit.table())
    for name in (("url", "http_headers_helper") if key == "siyuan" else ("command", "args")):
        if fields is not None and name in fields:
            server[name] = fields[name]
        else:
            server.pop(name, None)
    if not server:
        servers.pop(key, None)
    if not servers:
        document.pop("mcp_servers", None)
    owned_files.replace(path, tomlkit.dumps(document).encode(), before)


def apply(key: str, codex_home: Path, *, adopt: bool = False, repair: bool = False) -> str:
    if key in {"aoe", "peon-ping"}:
        return hook_integrations.apply(key, codex_home, adopt=adopt)
    status = inspect(key, codex_home, enabled=True)
    if status["state"] == "blocked" or status["state"] == "external" and not (adopt and can_adopt(key, codex_home)):
        raise NativeIntegrationError(f"{key} native 配置无法安全接管: {status['state']}")
    if status["state"] == "current":
        return "no-op"
    desired = _desired(key, codex_home, enabled=True, receipt=_read_receipt(key), configure=True)
    if key == "context-mode":
        return _context_apply(codex_home, desired, _read_receipt(key), repair=repair)
    previous = owned_files.read(_receipt_path(key), private=True)
    receipt = _read_receipt(key)
    before = _mcp_fields(key, codex_home)
    baseline = receipt["baseline"] if receipt else before
    # Ask the native owner for its contract in isolation, then merge its fields.
    if key == "codegraph":
        with tempfile.TemporaryDirectory(prefix="pennix-codegraph-") as directory:
            staging = Path(directory)
            _run([_command(key), "install", "--target", "codex", "--location", "global", "--yes"], staging)
            if _mcp_fields(key, staging) != {"command": "codegraph", "args": ["serve", "--mcp"]}:
                raise NativeIntegrationError("CodeGraph 原生配置合同已变化")
    _mcp_edit(key, codex_home, desired["mcp"], before)
    try:
        _write_receipt(key, desired, previous=previous, baseline=baseline)
    except BaseException as error:
        _mcp_edit(key, codex_home, before, desired["mcp"])
        raise NativeIntegrationError("codegraph 归属记录失败，已撤回配置") from error
    if inspect(key, codex_home, enabled=True)["state"] != "current":
        raise NativeIntegrationError(f"{key} native owner 写入后核验失败")
    return "changed"


def unconfigure(key: str, codex_home: Path) -> str:
    if key in {"aoe", "peon-ping"}:
        return hook_integrations.apply(key, codex_home, remove=True)
    receipt = _read_receipt(key)
    if receipt is None:
        return "preserved"
    if receipt["codex_home"] != str(codex_home) or inspect(key, codex_home, enabled=False)["state"] != "owned":
        raise NativeIntegrationError(f"{key} native 配置无法安全撤回")
    path = _receipt_path(key)
    before = owned_files.read(path, private=True)
    if key == "context-mode":
        return context_mode.unconfigure(sys.modules[__name__], codex_home, receipt)
    _mcp_edit(key, codex_home, receipt["baseline"], receipt["mcp"])
    try:
        owned_files.replace(path, None, before)
    except BaseException:
        _mcp_edit(key, codex_home, receipt["mcp"], receipt["baseline"])
        raise
    return "changed"
