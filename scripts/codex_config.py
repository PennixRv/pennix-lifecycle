"""Merge explicit Codex preferences; preserve the first ownership baseline."""
from __future__ import annotations

import os
import hashlib
from pathlib import Path
from collections.abc import Mapping, MutableMapping
from urllib.parse import urlsplit
import re
from tomlkit import document, dumps, parse
import owned_files as files

CodexConfigError = files.OwnershipError
STATE_SCHEMA = 3
OWNED = {
    "features.hooks": "PENNIX_CODEX_HOOKS",
    "features.goals": "PENNIX_CODEX_GOALS",
}
RETIRED = {"features.remote_compaction_v2"}
NATIVE_DEFAULTS = {
    "personality", "approvals_reviewer", "cli_auth_credentials_store",
    "tui.status_line", "tui.status_line_use_colors", "tui.terminal_title", "tui.theme",
}
IDLE_AGENT_FIELDS = {"agents.max_threads", "agents.max_depth", "agents.interrupt_message"}
IDLE_MEMORY_FIELDS = {"memories." + key for key in (
    "disable_on_external_context", "max_raw_memories_for_consolidation", "max_rollout_age_days",
    "max_rollouts_per_startup", "max_unused_days", "min_rate_limit_remaining_percent", "min_rollout_idle_hours",
)}
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
    managed = values.get("PENNIX_CODEX_MANAGED_TEMPLATE") == "y"
    want = {key: values[symbol] == "y" for key, symbol in OWNED.items()}
    for key, name in CHOICES.items():
        value = _choice(values, name)
        if value == "native":
            if managed:
                want[key] = None
            continue
        want[key] = {"on": True, "off": False}.get(value, value)
    if values.get("PENNIX_CODEX_MODEL_CUSTOM") == "y":
        want["model"] = values["PENNIX_CODEX_MODEL_ID"]
    if values.get("PENNIX_CODEX_HISTORY_LIMIT") == "y":
        want["history.max_bytes"] = int(values["PENNIX_CODEX_HISTORY_BYTES"])
    elif managed:
        want["history.max_bytes"] = None
    if managed:
        want.update({key: None for key in NATIVE_DEFAULTS | RETIRED})
        for key, symbol in {
            "model_context_window": "CONTEXT_WINDOW",
            "model_auto_compact_token_limit": "AUTO_COMPACT_TOKENS",
        }.items():
            want[key] = int(values["PENNIX_CODEX_" + symbol])
        if not 0 < want["model_auto_compact_token_limit"] <= want["model_context_window"]:
            raise CodexConfigError("Codex 自动压缩阈值必须为正整数且不超过上下文窗口")
        scope = values["PENNIX_CODEX_COMPACT_SCOPE"]
        if scope not in {"total", "body_after_prefix"}:
            raise CodexConfigError("Codex 自动压缩 scope 无效")
        want["model_auto_compact_token_limit_scope"] = scope
        for key, symbol in {
            "features.default_mode_request_user_input": "DEFAULT_MODE_INPUT",
            "check_for_update_on_startup": "CHECK_UPDATES",
            "suppress_unstable_features_warning": "SUPPRESS_UNSTABLE",
            "analytics.enabled": "ANALYTICS",
            "feedback.enabled": "FEEDBACK",
        }.items():
            want[key] = values["PENNIX_CODEX_" + symbol] == "y"
        want["log_dir"] = str(Path(os.path.expandvars(os.path.expanduser(values["PENNIX_CODEX_HOME"]))) / "logs")
        context = values["PENNIX_CODEX_SEARCH_CONTEXT"]
        if context not in {"low", "medium", "high"}:
            raise CodexConfigError("Codex 搜索上下文必须为 low、medium 或 high")
        want["tools.web_search.context_size"] = context
        for key in ("city", "country", "region", "timezone"):
            want["tools.web_search.location." + key] = values["PENNIX_CODEX_SEARCH_" + key.upper()]
        if want.get("agents.enabled") is False:
            want["features.multi_agent_v2"] = False
            want.update({key: None for key in IDLE_AGENT_FIELDS})
        if want.get("features.memories") is False:
            want.update({key: None for key in IDLE_MEMORY_FIELDS})
            want.update({"memories.generate_memories": False, "memories.use_memories": False})
        want["features.code_mode.direct_only_tool_namespaces"] = None
    if values.get("PENNIX_CODEX_PROVIDER_CUSTOM") == "y":
        provider = values["PENNIX_CODEX_PROVIDER_ID"]
        if (not re.fullmatch(r"[A-Za-z0-9_-]+", provider)
                or provider in {"openai", "ollama", "lmstudio"}):
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
        if managed:
            want[prefix + "capabilities.remote_compaction"] = None
        if values.get("PENNIX_CODEX_WEB_SEARCH_LIVE") == "y" or values.get("PENNIX_CODEX_WEB_SEARCH_CACHED") == "y":
            want[prefix + "supports_standalone_web_search"] = True
        if values.get("PENNIX_CODEX_AUTH_API_KEY") == "y":
            if managed:
                want[prefix + "requires_openai_auth"] = False
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
    parents = []
    for part in parts[:-1]:
        if part not in current:
            if value is None:
                return
            current[part] = {}
        if not isinstance(current[part], MutableMapping):
            raise CodexConfigError("Codex 受管字段前缀不是 table，拒绝覆盖")
        parents.append((current, part))
        current = current[part]
    if value is None:
        current.pop(parts[-1], None)
        for parent, part in reversed(parents):
            if parent[part]:
                break
            parent.pop(part)
    else:
        current[parts[-1]] = value


def _assert_current(doc, record):
    if record and any(_get(doc, key) != value for key, value in record["after"].items()):
        raise CodexConfigError("Codex 受管字段已发生外部漂移，拒绝覆盖或卸载")


def inspect(path: Path, values: dict) -> dict:
    doc, raw = _read(path)
    record = _load_record(path)
    _assert_current(doc, record)
    want = expected(values)
    retired_owned = record is not None and any(record["after"].get(key) is not None for key in RETIRED)
    formatted = values.get("PENNIX_CODEX_MANAGED_TEMPLATE") != "y" or dumps(doc.unwrap()).encode() == raw
    owned = values.get("PENNIX_CODEX_MANAGED_TEMPLATE") != "y" or record is not None and all(
        key in record["after"] and record["after"][key] == value for key, value in want.items())
    return {"state": "current" if not retired_owned and formatted and owned and all(_get(doc, key) == value for key, value in want.items()) else "pending",
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


def apply(path: Path, values: dict, expected_sha256: str | None = None) -> str:
    doc, raw = _read(path)
    record = _load_record(path)
    want = expected(values)
    managed = values.get("PENNIX_CODEX_MANAGED_TEMPLATE") == "y"
    if managed:
        namespaces = _get(doc, "features.code_mode.direct_only_tool_namespaces")
        if namespaces not in (None, []):
            raise CodexConfigError("Code Mode 已有非空偏好，拒绝按空配置清理")
        prefix = "model_providers." + values.get("PENNIX_CODEX_PROVIDER_ID", "") + "."
        if record and values.get("PENNIX_CODEX_PROVIDER_CUSTOM") == "y":
            want.update({key: None for key in record["after"]
                         if key.startswith("model_providers.") and not key.startswith(prefix)})
    if expected_sha256 is None:
        _assert_current(doc, record)
    else:
        if raw is None or hashlib.sha256(raw).hexdigest() != expected_sha256:
            raise CodexConfigError("Codex 配置与已审阅摘要不符，未释放归属")
        if record is None:
            raise CodexConfigError("释放归属需要既有可信 Codex 配置记录")
        for key, value in record["after"].items():
            actual = _get(doc, key)
            # Only a released field restored to its first baseline can bypass drift rejection.
            if actual != value and not (key not in want and actual == record["before"][key]):
                raise CodexConfigError("Codex 漂移字段未恢复首次基线或仍被选择，未释放归属")
    before = dict(record["before"]) if record else {}
    for key in want:
        if key not in before:
            before[key] = _get(doc, key)
    # Native/default relinquishes a field by restoring its first baseline.
    for key in set(before) - set(want):
        baseline = before.pop(key)
        _set(doc, key, None if key in RETIRED else baseline)
    for key, value in want.items():
        _set(doc, key, value)
    after_raw = (dumps(doc.unwrap()) if managed else doc.as_string()).encode()
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
        _set(doc, key, None if key in RETIRED else value)
    _commit(path, raw, doc.as_string().encode(), files.read(_state_path(), private=True), None)
    return "changed"
