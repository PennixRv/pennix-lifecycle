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


class NativeIntegrationError(RuntimeError):
    pass


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
    raw = owned_files.read(codex_home / "hooks.json")
    try:
        value = json.loads(raw, object_pairs_hook=owned_files.unique_pairs) if raw is not None else {}
    except (ValueError, UnicodeError) as error:
        raise NativeIntegrationError("hooks.json 不是有效 JSON") from error
    if (not isinstance(value, dict) or not isinstance(value.get("hooks", {}), dict)
            or any(not isinstance(entries, list) for entries in value.get("hooks", {}).values())):
        raise NativeIntegrationError("hooks.json 结构无效")
    return value, raw


def _marked(entry: object) -> bool:
    return isinstance(entry, dict) and isinstance(entry.get("hooks"), list) and any(
        isinstance(hook, dict) and "context-mode" in str(hook.get("command", ""))
        for hook in entry["hooks"]
    )


def _managed_hooks(document: dict) -> dict:
    return {event: selected for event, entries in document.get("hooks", {}).items()
            if (selected := [entry for entry in entries if _marked(entry)])}


def _edit_hooks(codex_home: Path, previous: dict, desired: dict, *, remove_empty: bool = False) -> None:
    document, before = _hooks(codex_home)
    if _managed_hooks(document) != previous:
        raise NativeIntegrationError("context-mode Hook 条目已漂移或由其他 owner 管理")
    hooks = document.setdefault("hooks", {})
    for event in previous:
        hooks[event] = [entry for entry in hooks[event] if not _marked(entry)]
        if not hooks[event]:
            del hooks[event]
    for event, entries in desired.items():
        hooks.setdefault(event, []).extend(copy.deepcopy(entries))
    if not hooks:
        document.pop("hooks", None)
    output = None if not document and remove_empty else owned_files.json_bytes(document)
    owned_files.replace(codex_home / "hooks.json", output, before)


def _context_target(codex_home: Path) -> dict:
    root = Path.home() / ".local/lib/node_modules/@pennixrv/context-mode"
    command = Path.home() / ".local/bin/context-mode"
    try:
        package = json.loads(owned_files.read(root / "package.json") or b"{}")
        if package.get("name") != "@pennixrv/context-mode" or command.resolve(strict=True) != root / "cli.bundle.mjs":
            raise NativeIntegrationError("context-mode 包和受管绝对入口归属不一致")
        raw = owned_files.read(root / "configs/codex/hooks.json")
        hooks = json.loads(raw or b"{}", object_pairs_hook=owned_files.unique_pairs)["hooks"]
        if not isinstance(hooks, dict) or set(hooks) != EVENTS:
            raise NativeIntegrationError("已安装包未声明完整六类 Codex Hook")
        hooks = copy.deepcopy(hooks)
        for event, entries in hooks.items():
            if not isinstance(entries, list) or len(entries) != 1 or not isinstance(entries[0], dict):
                raise NativeIntegrationError("包内 Hook 结构不符合合同")
            dispatch = entries[0].get("hooks")
            if dispatch != [{"type": "command", "command": f"context-mode hook codex {event.lower()}"}]:
                raise NativeIntegrationError("包内 Hook 命令不符合合同")
            dispatch[0]["command"] = f"{shlex.quote(str(command))} hook codex {event.lower()}"
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise NativeIntegrationError("无法核验 context-mode 程序与包内 Hook 来源") from error
    return {"mcp": {"command": str(command), "env": {"CONTEXT_MODE_PLATFORM": "codex", "CODEX_HOME": str(codex_home)}},
            "hooks": hooks}


def _desired(key: str, codex_home: Path, *, enabled: bool) -> dict:
    if key not in {"codegraph", "context-mode", "siyuan"}:
        raise NativeIntegrationError(f"未知 native integration: {key}")
    value = {"key": key, "enabled": enabled, "codex_home": str(codex_home)}
    if key == "context-mode":
        value.update(_context_target(codex_home))
    elif key == "codegraph":
        value["mcp"] = {"command": _command(key), "args": ["serve", "--mcp"]}
    else:
        helper = codex_home / "skills/pennix-skills/pennix-siyuan-knowledge/scripts/mcp_headers.py"
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


def _write_receipt(key: str, desired: dict, *, previous: bytes | None, hooks_file_created: bool = False, baseline: dict | None = None) -> None:
    record = {"schema": 1, "key": key, "codex_home": desired["codex_home"], "desired_digest": _digest(desired)}
    if key == "context-mode":
        record.update(mcp=desired["mcp"], hooks=desired["hooks"], hooks_file_created=hooks_file_created)
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
    document, _ = _hooks(codex_home)
    return _server("context-mode", codex_home) == record["mcp"] and _managed_hooks(document) == record["hooks"]


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
    desired = _desired(key, codex_home, enabled=True)
    return {"state": "current" if receipt["desired_digest"] == _digest(desired) else "update-required",
            "owned": True, "detail": "native owner configuration"}


def _mcp_add(codex_home: Path, expected: dict) -> None:
    if _server("context-mode", codex_home) is not None:
        raise NativeIntegrationError("拒绝覆盖已有 context-mode MCP")
    args = [_command("context-mode"), "mcp", "add", "context-mode"]
    for key, value in expected["env"].items():
        args += ["--env", f"{key}={value}"]
    _run([*args, "--", expected["command"]], codex_home)


def _mcp_remove(codex_home: Path, expected: dict) -> None:
    if _server("context-mode", codex_home) != expected:
        raise NativeIntegrationError("context-mode MCP 发生漂移，拒绝移除")
    _run([_command("context-mode"), "mcp", "remove", "context-mode"], codex_home)


def _context_apply(codex_home: Path, desired: dict, receipt: dict | None) -> str:
    if _toml(codex_home / "config.toml").get("features", {}).get("hooks") is not True:
        raise NativeIntegrationError("context-mode 要求已启用 Codex features.hooks")
    if receipt and not _context_matches(codex_home, receipt):
        raise NativeIntegrationError("context-mode 已有归属条目漂移，拒绝覆盖")
    before_receipt = owned_files.read(_receipt_path("context-mode"), private=True)
    _, hooks_before = _hooks(codex_home)
    old_hooks = receipt["hooks"] if receipt else {}
    hooks_created = receipt["hooks_file_created"] if receipt else hooks_before is None
    mcp_added = False
    hooks_written = False
    try:
        if receipt is None:
            _mcp_add(codex_home, desired["mcp"])
            mcp_added = True
        _edit_hooks(codex_home, old_hooks, desired["hooks"])
        hooks_written = True
        if not _context_matches(codex_home, desired):
            raise NativeIntegrationError("context-mode 写入后核验失败")
        _write_receipt("context-mode", desired, previous=before_receipt, hooks_file_created=hooks_created)
    except BaseException as error:
        try:
            if hooks_written:
                _edit_hooks(codex_home, desired["hooks"], old_hooks, remove_empty=hooks_created)
            if mcp_added or (receipt is None and _server("context-mode", codex_home) == desired["mcp"]):
                _mcp_remove(codex_home, desired["mcp"])
        except BaseException as rollback:
            raise NativeIntegrationError("context-mode 接入失败且按归属撤回未完成；保留现场") from rollback
        raise NativeIntegrationError("context-mode 接入失败；已撤回本次条目") from error
    return "changed"


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


def apply(key: str, codex_home: Path, *, adopt: bool = False) -> str:
    if key in {"aoe", "peon-ping"}:
        return hook_integrations.apply(key, codex_home, adopt=adopt)
    status = inspect(key, codex_home, enabled=True)
    if status["state"] == "blocked" or status["state"] == "external" and not (adopt and can_adopt(key, codex_home)):
        raise NativeIntegrationError(f"{key} native 配置无法安全接管: {status['state']}")
    if status["state"] == "current":
        return "no-op"
    desired = _desired(key, codex_home, enabled=True)
    if key == "context-mode":
        return _context_apply(codex_home, desired, _read_receipt(key))
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
        hooks_removed = False
        try:
            _edit_hooks(codex_home, receipt["hooks"], {}, remove_empty=receipt["hooks_file_created"])
            hooks_removed = True
            _mcp_remove(codex_home, receipt["mcp"])
            owned_files.replace(path, None, before)
        except BaseException as error:
            try:
                if _server(key, codex_home) is None:
                    _mcp_add(codex_home, receipt["mcp"])
                if hooks_removed:
                    _edit_hooks(codex_home, {}, receipt["hooks"])
            except BaseException as rollback:
                raise NativeIntegrationError("context-mode 撤回失败且恢复未完成；保留现场") from rollback
            raise NativeIntegrationError("context-mode 撤回失败；已恢复本组件条目") from error
        return "changed; preserved context-mode index/session data"
    _mcp_edit(key, codex_home, receipt["baseline"], receipt["mcp"])
    try:
        owned_files.replace(path, None, before)
    except BaseException:
        _mcp_edit(key, codex_home, receipt["mcp"], receipt["baseline"])
        raise
    return "changed"
