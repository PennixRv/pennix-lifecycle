"""Merge explicit Codex preferences; preserve the first ownership baseline."""
from __future__ import annotations

import os
from pathlib import Path
from collections.abc import Mapping, MutableMapping
from urllib.parse import urlsplit
import re
from tomlkit import document, parse
import owned_files as files

CodexConfigError = files.OwnershipError
STATE_SCHEMA = 3
OWNED = {
    "features.remote_compaction_v2": "PENNIX_CODEX_REMOTE_COMPACTION",
    "features.hooks": "PENNIX_CODEX_HOOKS",
    "features.goals": "PENNIX_CODEX_GOALS",
}
CHOICES = {
    "model_reasoning_effort": "REASONING",
    "plan_mode_reasoning_effort": "PLAN_REASONING",
    "model_reasoning_summary": "SUMMARY",
    "model_verbosity": "VERBOSITY",
    "approval_policy": "APPROVAL",
    "sandbox_mode": "SANDBOX",
    "web_search": "WEB_SEARCH",
    "service_tier": "SERVICE_TIER",
    "hide_agent_reasoning": "HIDE_REASONING",
    "show_raw_agent_reasoning": "RAW_REASONING",
    "tui.animations": "ANIMATIONS",
    "tui.show_tooltips": "TOOLTIPS",
    "tui.vim_mode_default": "VIM",
    "tui.alternate_screen": "ALT_SCREEN",
    "file_opener": "FILE_OPENER",
    "history.persistence": "HISTORY",
    "agents.enabled": "AGENTS",
    "features.memories": "MEMORIES",
}


def _state_path() -> Path:
    return files.state_root() / "codex-config.json"


def _read(path: Path):
    raw = files.read(path)
    try:
        return parse(raw.decode()) if raw is not None else document(), raw
    except Exception as error:
        raise CodexConfigError("Codex config.toml 不是有效 UTF-8 TOML") from error


def _load_record(path: Path):
    record = files.read_json(_state_path())
    if record is None:
        return None
    if (not isinstance(record, dict) or set(record) != {"schema", "path", "before", "after"}
            or type(record["schema"]) is not int or record["schema"] != STATE_SCHEMA
            or record["path"] != str(path) or not isinstance(record["before"], dict)
            or not isinstance(record["after"], dict) or set(record["before"]) != set(record["after"])):
        raise CodexConfigError("Codex 配置归属版本、字段或 home 冲突")
    return record


def is_owned(path: Path) -> bool:
    doc, _ = _read(path)
    record = _load_record(path)
    _assert_current(doc, record)
    return record is not None


def _choice(values, name):
    prefix = "PENNIX_CODEX_" + name + "_"
    active = [key[len(prefix):].lower().replace("_", "-") for key, value in values.items()
              if key.startswith(prefix) and value == "y"]
    if len(active) != 1:
        raise CodexConfigError(f"Codex choice 必须有且只有一个选项: {name}")
    return active[0]


def expected(values: dict) -> dict:
    want = {key: values[symbol] == "y" for key, symbol in OWNED.items()}
    for key, name in CHOICES.items():
        value = _choice(values, name)
        if value == "native":
            continue
        want[key] = {"on": True, "off": False}.get(value, value)
    if values.get("PENNIX_CODEX_MODEL_CUSTOM") == "y":
        want["model"] = values["PENNIX_CODEX_MODEL_ID"]
    if values.get("PENNIX_CODEX_HISTORY_LIMIT") == "y":
        want["history.max_bytes"] = int(values["PENNIX_CODEX_HISTORY_BYTES"])
    if values.get("PENNIX_CODEX_PROVIDER_CUSTOM") == "y":
        provider = values["PENNIX_CODEX_PROVIDER_ID"]
        if (not re.fullmatch(r"[A-Za-z0-9_-]+", provider)
                or provider.lower() in {"openai", "ollama", "lmstudio"}):
            raise CodexConfigError("Codex provider ID 无效或使用了保留名称")
        url = values["PENNIX_CODEX_PROVIDER_URL"]
        parsed = urlsplit(url)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise CodexConfigError("Codex Responses URL 必须是无凭据、查询参数和片段的 HTTP(S) URL")
        try:
            parsed.port
        except ValueError as error:
            raise CodexConfigError("Codex Responses URL 端口无效") from error
        if not values["PENNIX_CODEX_PROVIDER_NAME"].strip():
            raise CodexConfigError("Codex provider 显示名不能为空")
        want["model_provider"] = provider
        prefix = "model_providers." + provider + "."
        want[prefix + "name"] = values["PENNIX_CODEX_PROVIDER_NAME"]
        want[prefix + "base_url"] = url
        want[prefix + "wire_api"] = "responses"
        if values.get("PENNIX_CODEX_AUTH_API_KEY") == "y":
            secrets = values.get("PENNIX_SECRETS_FILE", "$HOME/.config/pennix/secrets.toml")
            secrets_path = Path(os.path.expandvars(os.path.expanduser(secrets)))
            if not secrets_path.is_absolute() or ".." in secrets_path.parts:
                raise CodexConfigError("Codex secrets.toml 路径必须是无穿越绝对路径")
            prefix += "auth."
            want[prefix + "command"] = str(Path.home() / ".local/bin/pennix")
            want[prefix + "args"] = ["secrets", "codex-token", "--secrets-file", str(secrets_path)]
        else:
            want[prefix + "requires_openai_auth"] = True
    return want


def _get(doc, key):
    current = doc
    for part in key.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current.unwrap() if hasattr(current, "unwrap") else current


def _set(doc, key, value):
    parts = key.split(".")
    current = doc
    for part in parts[:-1]:
        if part not in current:
            if value is None:
                return
            current[part] = {}
        if not isinstance(current[part], MutableMapping):
            raise CodexConfigError("Codex 受管字段前缀不是 table，拒绝覆盖")
        current = current[part]
    if value is None:
        current.pop(parts[-1], None)
    else:
        current[parts[-1]] = value


def _assert_current(doc, record):
    if record and any(_get(doc, key) != value for key, value in record["after"].items()):
        raise CodexConfigError("Codex 受管字段已发生外部漂移，拒绝覆盖或卸载")


def inspect(path: Path, values: dict) -> dict:
    doc, _ = _read(path)
    record = _load_record(path)
    _assert_current(doc, record)
    want = expected(values)
    return {"state": "current" if all(_get(doc, key) == value for key, value in want.items()) else "pending",
            "target": str(path), "owned_fields": sorted(record["after"]) if record else [],
            "requested_fields": sorted(want), "scope": "user-defaults; native callers may override"}


def _commit(path, before_raw, after_raw, record_raw, after_record):
    state = _state_path()
    files.replace(path, after_raw, before_raw)
    try:
        files.replace(state, after_record, record_raw)
    except BaseException:
        # Only restore bytes that still belong to this attempt.
        files.replace(path, before_raw, after_raw)
        raise


def apply(path: Path, values: dict) -> str:
    doc, raw = _read(path)
    record = _load_record(path)
    _assert_current(doc, record)
    want = expected(values)
    before = dict(record["before"]) if record else {}
    for key in want:
        if key not in before:
            before[key] = _get(doc, key)
    # Native/default relinquishes a field by restoring its first baseline.
    for key in set(before) - set(want):
        _set(doc, key, before.pop(key))
    for key, value in want.items():
        _set(doc, key, value)
    after_raw = doc.as_string().encode()
    after_record = files.json_bytes({"schema": STATE_SCHEMA, "path": str(path), "before": before, "after": want})
    record_raw = files.read(_state_path(), private=True)
    if after_raw == raw and after_record == record_raw:
        return "no-op"
    _commit(path, raw, after_raw, record_raw, after_record)
    observed, _ = _read(path)
    _assert_current(observed, _load_record(path))
    return "changed"


def uninstall(path: Path) -> str:
    record = _load_record(path)
    if record is None:
        return "no-op"
    doc, raw = _read(path)
    _assert_current(doc, record)
    for key, value in record["before"].items():
        _set(doc, key, value)
    _commit(path, raw, doc.as_string().encode(), files.read(_state_path(), private=True), None)
    return "changed"
