"""Format-preserving writer for the small lifecycle-owned Codex surface."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any

from tomlkit import document, parse


OWNED = {
    "features.remote_compaction_v2": "PENNIX_CODEX_REMOTE_COMPACTION",
    "features.hooks": "PENNIX_CODEX_HOOKS",
    "features.goals": "PENNIX_CODEX_GOALS",
    "features.memories": "PENNIX_CODEX_MEMORIES",
    "model_provider": None,
    "model_providers.pennix": None,
}
AGENTS_ENABLED = "PENNIX_CODEX_AGENTS_ENABLED"
PROVIDER_ID = "pennix"
STATE_SCHEMA = 3


class CodexConfigError(RuntimeError):
    """Codex TOML cannot be safely inspected or changed."""


def _state_path() -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")).expanduser()
    return base / "pennix-lifecycle" / "codex-config.json"


def _read(path: Path) -> tuple[Any, bytes | None]:
    if path.is_symlink():
        raise CodexConfigError("Codex config.toml 不是安全的普通文件")
    if not path.exists():
        return document(), None
    if not path.is_file():
        raise CodexConfigError("Codex config.toml 不是安全的普通文件")
    try:
        raw = path.read_bytes()
        return parse(raw.decode("utf-8")), raw
    except (OSError, UnicodeDecodeError) as error:
        raise CodexConfigError("Codex config.toml 无法读取") from error
    except Exception as error:
        raise CodexConfigError("Codex config.toml 不是有效 TOML") from error


def _write(path: Path, doc: Any) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    if path.parent.is_symlink() or not path.parent.is_dir():
        raise CodexConfigError("Codex 配置目录不安全")
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise CodexConfigError("Codex config.toml 不是安全的普通文件")
    mode = 0o600
    if path.exists():
        mode = stat.S_IMODE(path.lstat().st_mode)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(doc.as_string())
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode & 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _record(path: Path, before: dict[str, Any], after: dict[str, Any]) -> None:
    state = _state_path()
    state.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    state.parent.chmod(0o700)
    payload = {"schema": STATE_SCHEMA, "path": str(path), "before": before, "after": after}
    descriptor, name = tempfile.mkstemp(prefix=f".{state.name}.", dir=state.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, state)
    finally:
        temporary.unlink(missing_ok=True)


def _load_record() -> dict[str, Any] | None:
    state = _state_path()
    if state.is_symlink():
        raise CodexConfigError("Codex 配置归属记录不安全")
    if not state.exists():
        return None
    if not state.is_file() or stat.S_IMODE(state.stat().st_mode) != 0o600:
        raise CodexConfigError("Codex 配置归属记录不安全")
    try:
        value = json.loads(state.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CodexConfigError("Codex 配置归属记录无效") from error
    if not isinstance(value, dict) or value.get("schema") != STATE_SCHEMA:
        raise CodexConfigError("Codex 配置归属记录版本不受支持")
    return value


def _pennix_command() -> str:
    configured = os.environ.get("PENNIX_COMMAND")
    if configured:
        return configured
    return str(Path.home() / ".local" / "bin" / "pennix")


def _plain(value: Any) -> Any:
    if hasattr(value, "unwrap"):
        value = value.unwrap()
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


def expected(values: dict[str, str]) -> dict[str, Any]:
    provider_url = values["PENNIX_CODEX_PROVIDER_URL"]
    return {key: values[symbol] == "y" for key, symbol in OWNED.items() if symbol is not None} | {
        "model_provider": PROVIDER_ID,
        "model_providers.pennix": {
            "name": "Pennix OpenAI-compatible provider",
            "base_url": provider_url,
            "wire_api": "responses",
            "auth": {
                "command": _pennix_command(),
                "args": ["secrets", "get", "codex.api_key"],
                "timeout_ms": 5000,
                "refresh_interval_ms": 0,
            },
        },
        "agents.enabled": values[AGENTS_ENABLED] == "y",
    }


def _get(doc: Any, path: str) -> Any:
    value = doc
    for part in path.split("."):
        value = value.get(part) if isinstance(value, dict) else None
    return value


def _set(doc: Any, path: str, value: Any) -> None:
    parts = path.split(".")
    target = doc
    for part in parts[:-1]:
        if part not in target or not isinstance(target[part], dict):
            target[part] = {}
        target = target[part]
    target[parts[-1]] = value


def _delete(doc: Any, path: str) -> None:
    parts = path.split(".")
    parents: list[tuple[Any, str]] = []
    target = doc
    for part in parts[:-1]:
        if not isinstance(target, dict) or part not in target or not isinstance(target[part], dict):
            return
        parents.append((target, part))
        target = target[part]
    if isinstance(target, dict):
        target.pop(parts[-1], None)
    for parent, part in reversed(parents):
        if isinstance(parent.get(part), dict) and not parent[part]:
            parent.pop(part, None)


def _snapshot(doc: Any) -> dict[str, Any]:
    snapshot = {key: _plain(_get(doc, key)) for key in OWNED}
    snapshot["agents.enabled"] = _plain(_get(doc, "agents.enabled"))
    return snapshot


def inspect(path: Path, values: dict[str, str]) -> dict[str, Any]:
    doc, _ = _read(path)
    want = expected(values)
    observed_raw = {key: _plain(_get(doc, key)) for key in want}
    state = "current" if all(observed_raw[key] == value for key, value in want.items()) else (
        "missing" if all(value is None for value in observed_raw.values()) else "drifted"
    )
    observed = {
        key: value if isinstance(value, bool) else ("set" if value is not None else None)
        for key, value in observed_raw.items()
    }
    return {"state": state, "target": str(path), "owned": observed}


def apply(path: Path, values: dict[str, str]) -> str:
    doc, _ = _read(path)
    want = expected(values)
    current = _snapshot(doc)
    record = _load_record()
    if record is not None:
        if (
            record.get("path") != str(path)
            or not isinstance(record.get("before"), dict)
            or not isinstance(record.get("after"), dict)
        ):
            raise CodexConfigError("Codex 配置归属记录目标不匹配")
        if current != record["after"]:
            raise CodexConfigError("Codex 配置已偏离生命周期记录，拒绝覆盖")
        before = record["before"]
    else:
        before = current
    if current == want:
        return "no-op"
    for key, value in want.items():
        _set(doc, key, value)
    _write(path, doc)
    _record(path, before, want)
    return "changed"


def uninstall(path: Path) -> str:
    record = _load_record()
    if record is None:
        return "no-op"
    if (
        record.get("path") != str(path)
        or not isinstance(record.get("before"), dict)
        or not isinstance(record.get("after"), dict)
    ):
        raise CodexConfigError("Codex 配置归属记录目标不匹配")
    doc, _ = _read(path)
    if _snapshot(doc) != record["after"]:
        raise CodexConfigError("Codex 配置已偏离生命周期记录，拒绝卸载")
    for key in OWNED:
        value = record["before"].get(key)
        if value is None:
            _delete(doc, key)
        else:
            _set(doc, key, value)
    value = record["before"].get("agents.enabled")
    _delete(doc, "agents.enabled") if value is None else _set(doc, "agents.enabled", value)
    _write(path, doc)
    _state_path().unlink(missing_ok=True)
    return "changed"
