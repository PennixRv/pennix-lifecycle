"""Ownership-aware adapters for native MCP integration owners."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tomllib

import owned_files


class NativeIntegrationError(RuntimeError):
    pass


TIER_LIMITS = {
    "compact": (20_000, 18_000, {"FASTCTX_GREP_TOKEN_BUDGET": 9_000, "FASTCTX_GLOB_TOKEN_BUDGET": 4_500, "FASTCTX_RUN_TOKEN_BUDGET": 9_000, "FASTCTX_JOB_OUTPUT_TOKEN_BUDGET": 4_500}),
    "standard": (60_000, 54_000, {"FASTCTX_GREP_TOKEN_BUDGET": 10_800, "FASTCTX_GLOB_TOKEN_BUDGET": 5_400, "FASTCTX_RUN_TOKEN_BUDGET": 10_800, "FASTCTX_JOB_OUTPUT_TOKEN_BUDGET": 5_400}),
    "high": (100_000, 90_000, {"FASTCTX_GREP_TOKEN_BUDGET": 10_800, "FASTCTX_GLOB_TOKEN_BUDGET": 5_400, "FASTCTX_RUN_TOKEN_BUDGET": 10_800, "FASTCTX_JOB_OUTPUT_TOKEN_BUDGET": 5_400}),
}


def _receipt_path(key: str) -> Path:
    return owned_files.state_root() / "native" / f"{key}.json"


def _digest(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _desired(key: str, codex_home: Path, *, enabled: bool, tier: str) -> dict:
    if key == "fastctx":
        if tier not in TIER_LIMITS:
            raise NativeIntegrationError(f"未知 FastCtx tier: {tier}")
        return {"key": key, "enabled": enabled, "tier": tier, "codex_home": str(codex_home)}
    if key == "codegraph":
        return {"key": key, "enabled": enabled, "codex_home": str(codex_home)}
    raise NativeIntegrationError(f"未知 native integration: {key}")


def _read_receipt(key: str) -> dict | None:
    value = owned_files.read_json(_receipt_path(key))
    if value is None:
        return None
    if (not isinstance(value, dict) or set(value) != {"schema", "key", "codex_home", "desired_digest"}
            or value.get("schema") != 1 or value.get("key") != key
            or not all(isinstance(value.get(name), str) for name in ("codex_home", "desired_digest"))):
        raise NativeIntegrationError(f"{key} native integration receipt 无效")
    return value


def _write_receipt(key: str, desired: dict) -> None:
    path = _receipt_path(key)
    owned_files.replace(
        path,
        owned_files.json_bytes({
            "schema": 1,
            "key": key,
            "codex_home": desired["codex_home"],
            "desired_digest": _digest(desired),
        }),
        owned_files.read(path, private=True),
    )


def _command(name: str) -> str | None:
    return shutil.which(name)


def _run(command: list[str], timeout: int = 600) -> None:
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise NativeIntegrationError("native owner 未完成") from error
    if result.returncode:
        raise NativeIntegrationError(f"native owner 拒绝操作（退出码 {result.returncode}）")


def _reverse(key: str, command: str, codex_home: Path) -> None:
    if key == "fastctx":
        _run([command, "unapply", "--codex-home", str(codex_home), "--yes"])
    else:
        _run([command, "uninstall", "--target", "codex", "--location", "global", "--yes", "--keep-cli"])


def _toml(path: Path) -> dict:
    raw = owned_files.read(path)
    if raw is None:
        return {}
    try:
        return tomllib.loads(raw.decode("utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError) as error:
        raise NativeIntegrationError(f"native owner 配置不是有效 TOML: {path}") from error


def _fastctx_current(codex_home: Path, desired: dict) -> bool:
    config = _toml(codex_home / "config.toml")
    server = config.get("mcp_servers", {}).get("fastctx")
    if not isinstance(server, dict) or server.get("args") != ["serve"]:
        return False
    command = server.get("command")
    if not isinstance(command, str) or Path(command).name != "fastctx":
        return False
    limits = TIER_LIMITS[desired["tier"]]
    if config.get("tool_output_token_limit") != limits[0]:
        return False
    env = server.get("env")
    if not isinstance(env, dict) or env.get("FASTCTX_TOKEN_BUDGET") != str(limits[1]):
        return False
    for key, value in limits[2].items():
        if env.get(key) != str(value):
            return False
    settings = _toml(Path.home() / ".fastctx/config.toml")
    return settings.get("tier") == desired["tier"]


def _fastctx_present(codex_home: Path) -> bool:
    server = _toml(codex_home / "config.toml").get("mcp_servers", {}).get("fastctx")
    return isinstance(server, dict)


def _codegraph_current(codex_home: Path) -> bool:
    config = _toml(codex_home / "config.toml")
    server = config.get("mcp_servers", {}).get("codegraph")
    return isinstance(server, dict) and server.get("command") == "codegraph" and server.get("args") == ["serve", "--mcp"]


def _codegraph_present(codex_home: Path) -> bool:
    server = _toml(codex_home / "config.toml").get("mcp_servers", {}).get("codegraph")
    return isinstance(server, dict)


def inspect(key: str, codex_home: Path, *, enabled: bool, tier: str) -> dict[str, object]:
    desired = _desired(key, codex_home, enabled=enabled, tier=tier)
    receipt = _read_receipt(key)
    current = _fastctx_current(codex_home, desired) if key == "fastctx" else _codegraph_current(codex_home)
    present = _fastctx_present(codex_home) if key == "fastctx" else _codegraph_present(codex_home)
    if not enabled:
        return {"state": "owned" if receipt else "absent", "owned": bool(receipt), "detail": "native integration receipt" if receipt else "disabled"}
    if receipt is None:
        return {"state": "external" if present else "absent", "owned": False, "detail": "native config has no lifecycle receipt" if present else "not configured"}
    if receipt["codex_home"] != str(codex_home):
        return {"state": "blocked", "owned": False, "detail": "native receipt belongs to another CODEX_HOME"}
    if receipt["desired_digest"] != _digest(desired):
        return {"state": "drifted", "owned": True, "detail": "workflow native integration target changed"}
    return {"state": "current" if current else "drifted", "owned": True, "detail": "native owner configuration"}


def apply(key: str, codex_home: Path, *, tier: str) -> str:
    desired = _desired(key, codex_home, enabled=True, tier=tier)
    status = inspect(key, codex_home, enabled=True, tier=tier)
    if status["state"] == "external":
        raise NativeIntegrationError(f"{key} native 配置由外部 owner 管理，拒绝接管")
    if status["state"] == "blocked":
        raise NativeIntegrationError(str(status["detail"]))
    command = _command(key)
    if not command:
        raise NativeIntegrationError(f"{key} native owner 不可用")
    if key == "fastctx":
        _run([command, "apply", "--codex-home", str(codex_home), "--tier", tier, "--guidance", "none", "--yes"])
    else:
        _run([command, "install", "--target", "codex", "--location", "global", "--yes"])
    try:
        _write_receipt(key, desired)
    except BaseException as error:
        try:
            _reverse(key, command, codex_home)
        except BaseException as rollback:
            raise NativeIntegrationError(f"{key} native 配置已写入但归属记录失败，且恢复失败") from rollback
        raise NativeIntegrationError(f"{key} native 配置归属记录失败，已恢复原配置") from error
    result = inspect(key, codex_home, enabled=True, tier=tier)
    if result["state"] != "current":
        raise NativeIntegrationError(f"{key} native owner 写入后核验失败")
    return "changed"


def unconfigure(key: str, codex_home: Path, *, tier: str) -> str:
    desired = _desired(key, codex_home, enabled=False, tier=tier)
    receipt = _read_receipt(key)
    if receipt is None:
        return "preserved"
    if receipt["codex_home"] != str(codex_home):
        raise NativeIntegrationError(f"{key} native receipt belongs to another CODEX_HOME")
    status = inspect(key, codex_home, enabled=True, tier=tier)
    if status["state"] != "current":
        raise NativeIntegrationError(f"{key} native 配置无法安全撤回: {status['state']}")
    command = _command(key)
    if not command:
        raise NativeIntegrationError(f"{key} native owner 不可用")
    receipt_path = _receipt_path(key)
    before_receipt = owned_files.read(receipt_path, private=True)
    owned_files.replace(receipt_path, None, before_receipt)
    try:
        _reverse(key, command, codex_home)
    except BaseException as error:
        try:
            owned_files.replace(receipt_path, before_receipt, owned_files.read(receipt_path, private=True))
        except BaseException as rollback:
            raise NativeIntegrationError(f"{key} native 撤回失败且归属记录恢复失败") from rollback
        raise NativeIntegrationError(f"{key} native 撤回失败，已恢复归属记录") from error
    return "changed"
