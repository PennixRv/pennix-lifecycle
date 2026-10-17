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
    "remote_compaction_v2": "PENNIX_CODEX_REMOTE_COMPACTION",
    "hooks": "PENNIX_CODEX_HOOKS",
    "goals": "PENNIX_CODEX_GOALS",
    "memories": "PENNIX_CODEX_MEMORIES",
}
AGENTS_ENABLED = "PENNIX_CODEX_AGENTS_ENABLED"


class CodexConfigError(RuntimeError):
    """Codex TOML cannot be safely inspected or changed."""


def _state_path() -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")).expanduser()
    return base / "pennix-lifecycle" / "codex-config.json"


def _read(path: Path) -> tuple[Any, bytes | None]:
    if not path.exists():
        return document(), None
    if path.is_symlink() or not path.is_file():
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
    payload = {"schema": 1, "path": str(path), "before": before, "after": after}
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
    if not state.exists():
        return None
    if state.is_symlink() or not state.is_file() or stat.S_IMODE(state.stat().st_mode) != 0o600:
        raise CodexConfigError("Codex 配置归属记录不安全")
    try:
        value = json.loads(state.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CodexConfigError("Codex 配置归属记录无效") from error
    if not isinstance(value, dict) or value.get("schema") != 1:
        raise CodexConfigError("Codex 配置归属记录版本不受支持")
    return value


def expected(values: dict[str, str]) -> dict[str, bool]:
    return {key: values[symbol] == "y" for key, symbol in OWNED.items()} | {
        "agents.enabled": values[AGENTS_ENABLED] == "y"
    }


def inspect(path: Path, values: dict[str, str]) -> dict[str, Any]:
    doc, _ = _read(path)
    want = expected(values)
    observed_raw = {key: doc.get(key) for key in OWNED}
    agents = doc.get("agents")
    observed_raw["agents.enabled"] = agents.get("enabled") if hasattr(agents, "get") else None
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
    before = {key: doc.get(key) for key in OWNED}
    agents = doc.get("agents")
    before["agents.enabled"] = agents.get("enabled") if isinstance(agents, dict) else None
    for key, value in want.items():
        if key == "agents.enabled":
            if "agents" not in doc or not isinstance(doc["agents"], dict):
                doc["agents"] = {}
            doc["agents"]["enabled"] = value
        else:
            doc[key] = value
    if before == want:
        return "no-op"
    _write(path, doc)
    _record(path, before, want)
    return "changed"


def uninstall(path: Path) -> str:
    record = _load_record()
    if record is None:
        return "no-op"
    if record.get("path") != str(path) or not isinstance(record.get("before"), dict):
        raise CodexConfigError("Codex 配置归属记录目标不匹配")
    doc, _ = _read(path)
    for key in OWNED:
        value = record["before"].get(key)
        if value is None:
            doc.pop(key, None)
        else:
            doc[key] = value
    value = record["before"].get("agents.enabled")
    if value is None:
        agents = doc.get("agents")
        if isinstance(agents, dict):
            agents.pop("enabled", None)
            if not agents:
                doc.pop("agents", None)
    else:
        if "agents" not in doc or not isinstance(doc["agents"], dict):
            doc["agents"] = {}
        doc["agents"]["enabled"] = value
    _write(path, doc)
    _state_path().unlink(missing_ok=True)
    return "changed"
