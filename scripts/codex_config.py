"""Merge explicit Codex preferences; preserve the first ownership baseline."""
from __future__ import annotations

import os
import hashlib
import json
from pathlib import Path
from collections.abc import Mapping, MutableMapping
from urllib.parse import urlsplit
import re
import shutil
from tomlkit import document, dumps, parse
import owned_files as files
import model_catalog

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
    if (not isinstance(record, dict) or type(record.get("schema")) is not int
            or record["schema"] not in {STATE_SCHEMA, 4}
            or set(record) != ({"schema", "path", "before", "after"} |
                              ({"catalog", "retained_catalogs"} if record["schema"] == 4 else set()))
            or record["path"] != str(path) or not isinstance(record["before"], dict)
            or not isinstance(record["after"], dict) or set(record["before"]) != set(record["after"])):
        raise CodexConfigError("Codex 配置归属版本、字段或 home 冲突")
    if record["schema"] == 3 and "model_catalog_json" in record["after"]:
        raise CodexConfigError("旧归属协议不能证明模型目录的所有权")
    if record["schema"] == 4:
        if not isinstance(record["retained_catalogs"], list):
            raise CodexConfigError("Codex 模型目录保留归属无效")
        descriptors = record["retained_catalogs"]
        if record["catalog"] is not None:
            descriptors = [record["catalog"], *descriptors]
            if record["after"].get("model_catalog_json") != model_catalog.reference(record["catalog"]):
                raise CodexConfigError("Codex 模型目录引用与归属不符")
        generations = [model_catalog._directory(value).name for value in descriptors]
        if len(set(generations)) != len(generations):
            raise CodexConfigError("Codex 模型目录归属重复")
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


def expected(values: dict, *, catalog: dict | None = None) -> dict:
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
    if values.get("PENNIX_CODEX_MANAGED_CATALOG") == "y" and catalog is not None:
        want["model_catalog_json"] = model_catalog.reference(catalog)
    return want


def _get(doc, key):
    current = doc
    for part in key.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current.unwrap() if hasattr(current, "unwrap") else current


def _same(left, right) -> bool:
    try:
        return files.json_bytes(left) == files.json_bytes(right)
    except (TypeError, ValueError):
        return False


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
    if record and any(not _same(_get(doc, key), value) for key, value in record["after"].items()):
        raise CodexConfigError("Codex 受管字段已发生外部漂移，拒绝覆盖或卸载")
    if record and record.get("catalog") is not None:
        model_catalog.read_generation(record["catalog"])


def _catalog_baseline(value) -> None:
    if value is not None:
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise CodexConfigError("首次模型目录基线不是明确的绝对路径")
        raw = files.read(Path(value))
        if raw is None:
            raise CodexConfigError("首次模型目录基线已不可用，未恢复坏引用")
        model_catalog.parse_catalog(raw)


def catalog_adoption_digest(path: Path) -> str | None:
    doc, _ = _read(path)
    value = _get(doc, "model_catalog_json")
    if value is None:
        return None
    _catalog_baseline(value)
    return hashlib.sha256(files.json_bytes({
        "path": str(path), "reference": value,
        "catalog_sha256": hashlib.sha256(files.read(Path(value))).hexdigest(),
    })).hexdigest()


def _catalog_context(path: Path, values: dict, lock_path: Path) -> tuple[dict, dict]:
    doc, raw = _read(path)
    record = _load_record(path)
    _assert_current(doc, record)
    if values.get("PENNIX_CODEX_MANAGED_CATALOG") != "y":
        raise CodexConfigError("未选择受管模型目录，不能刷新静态配置资产")
    for key, value in expected(values).items():
        _set(doc, key, value)
    binary = Path(shutil.which("codex") or "/usr/bin/codex")
    identity = model_catalog.binary_identity(binary)
    scope = model_catalog.provider_scope(doc.unwrap())[1]
    tool = hashlib.sha256()
    for source in (Path(__file__), Path(model_catalog.__file__), model_catalog.RULE_PATH):
        tool.update(source.read_bytes())
    def digest(value):
        return hashlib.sha256(value).hexdigest() if value is not None else None
    context = {"home": str(Path.home()), "path": str(path), "state_root": str(files.state_root()),
               "config_sha256": digest(raw), "settings_sha256": digest(files.json_bytes(values)),
               "ownership_sha256": digest(files.read(_state_path(), private=True)),
               "lock_path": str(lock_path), "lock_sha256": digest(files.read(lock_path, private=True)),
               "tool_sha256": tool.hexdigest(), "binary": identity, "provider_scope_sha256": scope}
    return doc.unwrap(), context


def prepare_catalog_review(path: Path, values: dict, lock_path: Path) -> dict:
    native, context = _catalog_context(path, values, lock_path)
    source, proof = model_catalog.acquire(Path(context["binary"]["path"]), native)
    # Identity may not change between context capture and native acquisition.
    if proof["binary"] != context["binary"] or proof["provider_scope_sha256"] != context["provider_scope_sha256"]:
        raise CodexConfigError("模型目录采集期间来源身份改变")
    value = model_catalog.candidate(source, proof)
    record = _load_record(path)
    unchanged = False
    if record and record.get("catalog") is not None:
        fixed = model_catalog.read_generation(record["catalog"])
        previous = json.loads(fixed["contents"]["proof.json"])
        unchanged = (fixed["contents"]["source.json"] == source
                     and fixed["contents"]["rule.md"] == model_catalog.rule_bytes()
                     and all(previous[key] == proof[key] for key in (
                         "binary", "provider_scope_sha256", "endpoint_identity_sha256"))
                     and inspect(path, values)["state"] == "current")
        if unchanged:
            value = fixed
    review = model_catalog.stage_review(value, context)
    return {"operation": "update", "component": "codex-config", "status": "no-op" if unchanged else "review",
            "review_sha256": review["review_sha256"], "catalog": review["catalog"],
            "binary": context["binary"], "models": proof["effective_models"],
            "source_sha256": proof["source_sha256"],
            "adoption_sha256": catalog_adoption_digest(path)
                if _get(_read(path)[0], "model_catalog_json") is not None
                and (not record or "model_catalog_json" not in record["after"]) else None}


def reviewed_catalog(path: Path, values: dict, lock_path: Path, digest: str) -> dict:
    native, context = _catalog_context(path, values, lock_path)
    value = model_catalog.load_review(digest, context)
    model_catalog.compatible(value, Path(context["binary"]["path"]), native)
    return value


def catalog_repair_review(path: Path) -> dict | None:
    doc, raw = _read(path)
    record = _load_record(path)
    if not record or record.get("catalog") is None:
        return None
    if any(not _same(_get(doc, key), value) for key, value in record["after"].items()):
        raise CodexConfigError("目录修复不能覆盖或释放已漂移的配置字段")
    value, observed = model_catalog.repair_candidate(record["catalog"])
    binding = {"path": str(path), "config_sha256": hashlib.sha256(raw).hexdigest(),
               "ownership_sha256": hashlib.sha256(files.read(_state_path(), private=True)).hexdigest(),
               "catalog": record["catalog"], "observed": observed}
    return {"catalog_repair_sha256": hashlib.sha256(files.json_bytes(binding)).hexdigest(),
            "catalog": record["catalog"], "observed": observed, "candidate": value}


def repair_catalog(path: Path, digest: str) -> str:
    review = catalog_repair_review(path)
    if review is None or not digest or review["catalog_repair_sha256"] != digest:
        raise CodexConfigError("目录修复需 plan 中当前配置与资产的精确摘要")
    doc, raw = _read(path)
    model_catalog.compatible(review["candidate"], Path(shutil.which("codex") or "/usr/bin/codex"), doc.unwrap())
    record_raw = files.read(_state_path(), private=True)
    _commit(path, raw, raw, record_raw, record_raw, catalog_candidate=review["candidate"],
            asset_repair={"catalog": review["catalog"], "before": review["observed"]})
    observed, _ = _read(path)
    _assert_current(observed, _load_record(path))
    model_catalog.compatible(review["candidate"], Path(shutil.which("codex") or "/usr/bin/codex"),
                             observed.unwrap())
    return "changed"


def catalog_inventory(path: Path, values: dict, lock_path: Path) -> dict:
    native, context = _catalog_context(path, values, lock_path)
    source, proof = model_catalog.acquire(Path(context["binary"]["path"]), native)
    current = _load_record(path)
    descriptor = current.get("catalog") if current else None
    matched = False
    if descriptor is not None:
        fixed = model_catalog.read_generation(descriptor)
        previous = json.loads(fixed["contents"]["proof.json"])
        matched = (descriptor["files"]["source.json"] == hashlib.sha256(source).hexdigest()
                   and descriptor["files"]["rule.md"] == hashlib.sha256(model_catalog.rule_bytes()).hexdigest()
                   and all(previous[key] == proof[key] for key in (
                       "binary", "provider_scope_sha256", "endpoint_identity_sha256")))
    current_configuration = matched and inspect(path, values)["state"] == "current"
    return {"state": "current" if current_configuration else "update", "available": None,
            "catalog_source_state": "fresh-native-verified", "source_sha256": proof["source_sha256"],
            "models": proof["effective_models"], "binary": proof["binary"],
            "next": "pennix update codex-config --plan" if not current_configuration else None}


def inspect(path: Path, values: dict, *, defer_catalog: bool = False) -> dict:
    doc, raw = _read(path)
    record = _load_record(path)
    _assert_current(doc, record)
    selected_catalog = values.get("PENNIX_CODEX_MANAGED_CATALOG") == "y"
    catalog = record.get("catalog") if record else None
    want = expected(values, catalog=catalog)
    if selected_catalog and defer_catalog:
        if catalog is None and _get(doc, "model_catalog_json") is not None:
            if values.get("PENNIX_CODEX_CATALOG_ADOPT_SHA256") != catalog_adoption_digest(path):
                raise CodexConfigError("整套更新前须精确接纳现有外部模型目录")
        return {"state": "pending", "target": str(path), "catalog_checkpoint": True,
                "scope": "after approved dependencies; fresh native source requires separate exact review"}
    if selected_catalog and catalog is None:
        return {"state": "blocked", "target": str(path),
                "detail": "受管模型目录尚无固定来源；先显式 update codex-config",
                "catalog_adoption_sha256": catalog_adoption_digest(path)}
    if selected_catalog:
        candidate = model_catalog.with_current_rule(catalog)
        model_catalog.compatible(candidate, Path(shutil.which("codex") or "/usr/bin/codex"), doc.unwrap())
        want["model_catalog_json"] = model_catalog.reference(candidate["descriptor"])
    retired_owned = record is not None and any(record["after"].get(key) is not None for key in RETIRED)
    formatted = values.get("PENNIX_CODEX_MANAGED_TEMPLATE") != "y" or dumps(doc.unwrap()).encode() == raw
    owned = values.get("PENNIX_CODEX_MANAGED_TEMPLATE") != "y" or record is not None and all(
        key in record["after"] and record["after"][key] == value for key, value in want.items())
    return {"state": "current" if not retired_owned and formatted and owned and all(_same(_get(doc, key), value) for key, value in want.items()) else "pending",
            "target": str(path), "owned_fields": sorted(record["after"]) if record else [],
            "requested_fields": sorted(want), "scope": "user-defaults; native callers may override",
            **({"catalog_source_state": "fixed-not-refreshed"} if selected_catalog else {})}


def _commit(path, before_raw, after_raw, record_raw, after_record, *, catalog_candidate=None, asset_repair=None):
    state = _state_path()
    _record_intent(path, before_raw, after_raw, record_raw, after_record, asset_repair=asset_repair)
    if catalog_candidate is not None:
        model_catalog.publish(catalog_candidate, repairs=asset_repair["before"] if asset_repair else None)
    files.replace(path, after_raw, before_raw)
    try:
        files.replace(state, after_record, record_raw)
    except BaseException:
        # Only restore bytes that still belong to this attempt.
        files.replace(path, before_raw, after_raw)
        raise
    if after_record is None and record_raw:
        _withdraw_catalogs(json.loads(record_raw), parse(after_raw.decode()))


def _withdraw_catalogs(record: dict, doc) -> None:
    catalogs = list(record.get("retained_catalogs", []))
    if record.get("catalog") is not None:
        catalogs.append(record["catalog"])
    for descriptor in catalogs:
        if _get(doc, "model_catalog_json") == model_catalog.reference(descriptor):
            raise CodexConfigError("仍有配置引用待删除模型目录，未清理")
    for descriptor in catalogs:
        model_catalog.remove_generation(descriptor)


def _introduced_catalogs(intent: dict) -> list[dict]:
    def owned(record):
        if not record:
            return []
        return [*record.get("retained_catalogs", []),
                *([record["catalog"]] if record.get("catalog") is not None else [])]
    before = {value["generation"] for value in owned(intent["record_before"])}
    return [value for value in owned(intent["record_after"]) if value["generation"] not in before]


def _intent_path(transaction: str, step_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", transaction):
        raise CodexConfigError("Codex 配置事务身份无效")
    return files.state_root() / "transactions" / transaction / (
        "codex-config-" + hashlib.sha256(step_id.encode()).hexdigest() + ".json")


def _record_intent(path, before_raw, after_raw, record_raw, after_record, *, asset_repair=None) -> None:
    import apply_journal

    journal = apply_journal.read()
    if journal is None:
        return  # Isolated legacy direct-owner calls retain exception compensation.
    steps = [step for step in journal["steps"] if step["component"] == "codex-config"
             and step["status"] == "started"]
    if (journal.get("schema") != 2 or len(steps) != 1
            or journal["identity"] != {"home": str(Path.home()), "codex_home": str(path.parent),
                                       "state_root": str(files.state_root())}):
        raise CodexConfigError("Codex 配置缺少同目标的唯一已启动 journal 步骤")
    step = steps[0]
    if step["target"].get("codex_config_protocol") != 1:
        raise CodexConfigError("Codex 配置 journal 未登记本次持久写入协议")
    before_record = json.loads(record_raw) if record_raw else None
    desired_record = json.loads(after_record) if after_record else None
    fields = set((before_record or {}).get("after", {})) | set((desired_record or {}).get("after", {}))
    before_doc = parse(before_raw.decode()) if before_raw is not None else document()
    after_doc = parse(after_raw.decode()) if after_raw is not None else document()
    intent = {"schema": 1, "transaction": journal["transaction"], "step": step["id"],
              "identity": journal["identity"], "path": str(path),
              "fields_before": {key: _get(before_doc, key) for key in fields},
              "fields_after": {key: _get(after_doc, key) for key in fields},
              "record_before": before_record, "record_after": desired_record, "asset_repair": asset_repair}
    destination = _intent_path(journal["transaction"], step["id"])
    raw = files.json_bytes(intent)
    existing = files.read(destination, private=True)
    if existing is not None and existing != raw:
        raise CodexConfigError("Codex 配置同次恢复意图已存在且不同")
    files.replace(destination, raw, existing)
    target = {**step["target"], "codex_config_intent": hashlib.sha256(raw).hexdigest()}
    apply_journal.set_target(journal, step["id"], target)


def prepare_journal(journal: dict, step_id: str) -> dict:
    import apply_journal

    step = apply_journal.step(journal, step_id)
    if step["component"] != "codex-config" or step["status"] != "pending":
        raise CodexConfigError("Codex 配置只能在 pending 步骤登记写入协议")
    return apply_journal.set_target(journal, step_id, {**step["target"], "codex_config_protocol": 1})


def recover(path: Path, journal: dict, step: dict, resolution: str) -> dict:
    """Prove or restore this exact journal step without replacing unrelated TOML."""
    if resolution not in {"completed", "restored", "not-started"}:
        raise CodexConfigError("Codex 配置恢复结果无效")
    expected_identity = {"home": str(Path.home()), "codex_home": str(path.parent),
                         "state_root": str(files.state_root())}
    if (journal.get("schema") != 2 or journal.get("identity") != expected_identity
            or step.get("component") != "codex-config" or step.get("status") not in {"started", "unknown"}
            or type(step.get("target", {}).get("codex_config_protocol")) is not int
            or step["target"]["codex_config_protocol"] != 1):
        raise CodexConfigError("Codex 配置恢复没有绑定同一目标或可恢复步骤")
    digest = step["target"].get("codex_config_intent")
    if step["target"].get("codex_config_protocol") == 1 and digest is None:
        if resolution == "not-started":
            # Protocol publishes the intent digest before the first active mutation.
            return {"resolution": resolution, "effect": "none", "desired_applied": False}
        raise CodexConfigError("Codex 配置尚无已提交写入意图，只能核验 not-started")
    if (journal.get("schema") != 2 or step["component"] != "codex-config"
            or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
        raise CodexConfigError("缺少同次 Codex 配置持久意图，不能释放 unknown")
    raw = files.read(_intent_path(journal["transaction"], step["id"]), private=True)
    if raw is None or hashlib.sha256(raw).hexdigest() != digest:
        raise CodexConfigError("Codex 配置恢复材料缺失或摘要不符")
    try:
        intent = json.loads(raw, object_pairs_hook=files.unique_pairs)
    except (UnicodeError, ValueError) as error:
        raise CodexConfigError("Codex 配置恢复材料不是有效 JSON") from error
    if (not isinstance(intent, dict) or set(intent) != {"schema", "transaction", "step", "identity", "path",
            "fields_before", "fields_after", "record_before", "record_after", "asset_repair"}
            or type(intent["schema"]) is not int or intent["schema"] != 1
            or intent["transaction"] != journal["transaction"] or intent["step"] != step["id"]
            or intent["identity"] != journal["identity"] or intent["identity"] != expected_identity
            or intent["path"] != str(path) or not isinstance(intent["fields_before"], dict)
            or not isinstance(intent["fields_after"], dict)
            or set(intent["fields_before"]) != set(intent["fields_after"])):
        raise CodexConfigError("Codex 配置恢复材料身份或字段冲突")
    repair = intent["asset_repair"]
    if repair is not None:
        if (not isinstance(repair, dict) or set(repair) != {"catalog", "before"}
                or repair["catalog"] != (intent["record_before"] or {}).get("catalog")
                or intent["record_before"] != intent["record_after"]
                or not isinstance(repair["before"], dict)
                or set(repair["before"]) != {"derived.json", "manifest.json"}):
            raise CodexConfigError("同次目录修复的归属或前值无效")
        for digest_before in repair["before"].values():
            if digest_before is not None and (not isinstance(digest_before, str)
                    or not re.fullmatch(r"[0-9a-f]{64}", digest_before)):
                raise CodexConfigError("同次目录修复的前值摘要无效")
    current, current_raw = _read(path)
    record_raw = files.read(_state_path(), private=True)
    before_record = files.json_bytes(intent["record_before"]) if intent["record_before"] is not None else None
    after_record = files.json_bytes(intent["record_after"]) if intent["record_after"] is not None else None
    if record_raw not in (before_record, after_record):
        raise CodexConfigError("Codex 配置归属有第三方修改，不能恢复")
    for key, before in intent["fields_before"].items():
        if not any(_same(_get(current, key), value) for value in (before, intent["fields_after"][key])):
            raise CodexConfigError("Codex 配置受管字段有第三方修改，不能恢复")
    repair_value = None
    if repair is not None:
        repair_value, observed_assets = model_catalog.repair_candidate(repair["catalog"])
        desired_assets = {"derived.json": repair["catalog"]["files"]["derived.json"],
                          "manifest.json": hashlib.sha256(files.json_bytes(repair["catalog"])).hexdigest()}
        if any(observed_assets[name] not in (before, desired_assets[name])
               for name, before in repair["before"].items()):
            raise CodexConfigError("目录修复目标有第三方修改，不能恢复")
        if resolution == "restored":
            raise CodexConfigError("不能把未知损坏内容恢复为活动目录；核验 completed 或未开始的 not-started")
        if resolution == "not-started" and observed_assets != repair["before"]:
            raise CodexConfigError("同次目录修复已写入资产，不能证明未开始")
    if resolution == "completed":
        if record_raw != after_record or any(not _same(_get(current, key), value) for key, value in intent["fields_after"].items()):
            raise CodexConfigError("同次 Codex 配置后置条件尚未全部完成")
        if repair_value is not None:
            model_catalog.compatible(repair_value, Path(shutil.which("codex") or "/usr/bin/codex"), current.unwrap())
            model_catalog.publish(repair_value, repairs=observed_assets)
        if intent["record_after"] and intent["record_after"].get("catalog") is not None:
            model_catalog.compatible(model_catalog.read_generation(intent["record_after"]["catalog"]),
                Path(shutil.which("codex") or "/usr/bin/codex"), current.unwrap())
        if intent["record_after"] is None and intent["record_before"]:
            _withdraw_catalogs(intent["record_before"], current)
    elif resolution == "not-started":
        if record_raw != before_record or any(not _same(_get(current, key), value) for key, value in intent["fields_before"].items()):
            raise CodexConfigError("不能证明同次 Codex 配置未开始")
        if any(model_catalog._directory(value).exists() for value in _introduced_catalogs(intent)):
            raise CodexConfigError("同次模型目录资产已经开始写入；应核验 restored")
    else:
        if intent["record_before"] and intent["record_before"].get("catalog") is not None:
            previous = model_catalog.read_generation(intent["record_before"]["catalog"])
            restored_doc = parse(current.as_string())
            for key, value in intent["fields_before"].items():
                _set(restored_doc, key, value)
            model_catalog.compatible(previous, Path(shutil.which("codex") or "/usr/bin/codex"),
                                     restored_doc.unwrap())
        if "model_catalog_json" in intent["fields_before"]:
            _catalog_baseline(intent["fields_before"]["model_catalog_json"])
        for key, value in intent["fields_before"].items():
            _set(current, key, value)
        files.replace(path, current.as_string().encode(), current_raw)
        files.replace(_state_path(), before_record, record_raw)
        observed, _ = _read(path)
        if (files.read(_state_path(), private=True) != before_record
                or any(not _same(_get(observed, key), value) for key, value in intent["fields_before"].items())):
            raise CodexConfigError("Codex 配置恢复前值核验失败")
        _withdraw_catalogs({"retained_catalogs": _introduced_catalogs(intent)}, observed)
    return {"resolution": resolution, "intent_sha256": digest, "desired_applied": resolution == "completed"}


def apply(path: Path, values: dict, expected_sha256: str | None = None, *, catalog_candidate=None) -> str:
    doc, raw = _read(path)
    record = _load_record(path)
    catalog = None
    if values.get("PENNIX_CODEX_MANAGED_CATALOG") == "y":
        if catalog_candidate is not None:
            catalog = catalog_candidate
        elif record and record.get("catalog") is not None:
            catalog = model_catalog.with_current_rule(record["catalog"])
        else:
            raise CodexConfigError("受管模型目录缺少已审阅固定候选；先显式 update codex-config")
        if (not record or "model_catalog_json" not in record["after"]) and _get(doc, "model_catalog_json") is not None:
            approved = values.get("PENNIX_CODEX_CATALOG_ADOPT_SHA256")
            if not approved or approved != catalog_adoption_digest(path):
                raise CodexConfigError("外部模型目录未经精确摘要接纳，未覆盖")
    elif catalog_candidate is not None:
        raise CodexConfigError("未选择受管目录，拒绝提交目录候选")
    want = expected(values, catalog=catalog["descriptor"] if catalog else None)
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
            if not _same(actual, value) and not (key not in want and _same(actual, record["before"][key])):
                raise CodexConfigError("Codex 漂移字段未恢复首次基线或仍被选择，未释放归属")
    before = dict(record["before"]) if record else {}
    for key in want:
        if key not in before:
            before[key] = _get(doc, key)
    # Native/default relinquishes a field by restoring its first baseline.
    for key in set(before) - set(want):
        baseline = before.pop(key)
        if key == "model_catalog_json":
            _catalog_baseline(baseline)
        _set(doc, key, None if key in RETIRED else baseline)
    for key, value in want.items():
        _set(doc, key, value)
    if catalog is not None:
        model_catalog.compatible(catalog, Path(shutil.which("codex") or "/usr/bin/codex"), doc.unwrap())
    after_raw = (dumps(doc.unwrap()) if managed else doc.as_string()).encode()
    desired_record = {"schema": STATE_SCHEMA, "path": str(path), "before": before, "after": want}
    if catalog is not None or record and record["schema"] == 4:
        retained = list(record.get("retained_catalogs", [])) if record else []
        if record and record.get("catalog") is not None:
            retained.append(record["catalog"])
        current_catalog = catalog["descriptor"] if catalog else None
        retained = {item["generation"]: item for item in retained
                    if current_catalog is None or item != current_catalog}
        desired_record.update(schema=4, catalog=current_catalog, retained_catalogs=list(retained.values()))
    after_record = files.json_bytes(desired_record)
    record_raw = files.read(_state_path(), private=True)
    if after_raw == raw and after_record == record_raw:
        return "no-op"
    _commit(path, raw, after_raw, record_raw, after_record, catalog_candidate=catalog)
    observed, _ = _read(path)
    _assert_current(observed, _load_record(path))
    if catalog is not None:
        model_catalog.compatible(catalog, Path(shutil.which("codex") or "/usr/bin/codex"), observed.unwrap())
    return "changed"


def uninstall(path: Path) -> str:
    record = _load_record(path)
    if record is None:
        return "no-op"
    doc, raw = _read(path)
    _assert_current(doc, record)
    for key, value in record["before"].items():
        if key == "model_catalog_json":
            _catalog_baseline(value)
        _set(doc, key, None if key in RETIRED else value)
    _commit(path, raw, doc.as_string().encode(), files.read(_state_path(), private=True), None)
    return "changed"
