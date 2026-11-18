"""Consume CodeGraph's native installer and trust only its reviewed Codex handlers."""
import copy
from pathlib import Path
import queue
import re
import shlex
import tempfile

import codex_hook_state
import context_mode_integration as shared
import hook_integrations
import owned_files as files

EVENTS = {"SessionStart": "session_start", "UserPromptSubmit": "user_prompt_submit"}


def require(condition, message="CodeGraph 原生 Hook 合同不符"):
    if not condition:
        raise shared.ContextModeError(message)


def marked(entry):
    return isinstance(entry, dict) and any(isinstance(h, dict) and "codegraph" in str(h.get("command", ""))
                                          and "prompt-hook" in str(h.get("command", ""))
                                          for h in entry.get("hooks", []))


def managed(document):
    return {event: selected for event, entries in document.get("hooks", {}).items()
            if (selected := [entry for entry in entries if marked(entry)])}


def target(owner):
    command = owner._command("codegraph")
    with tempfile.TemporaryDirectory(prefix="pennix-codegraph-") as directory:
        staging = Path(directory)
        owner._run([command, "install", "--target", "codex", "--location", "global", "--yes"], staging)
        require(owner._mcp_fields("codegraph", staging) == {"command": "codegraph", "args": ["serve", "--mcp"]},
                "CodeGraph 原生 MCP 合同已变化")
        document, _ = owner._hooks(staging)
    hooks = document.get("hooks", {})
    if not hooks:
        return {}  # Older installed releases retain their MCP-only receipt until the package update.
    require(set(hooks) == set(EVENTS) and set(document) == {"hooks"})
    expected = {"hooks": [{"type": "command", "command": "codegraph prompt-hook --agent codex",
                           "timeout": 2, "additionalContextLimit": 0}]}
    require(all(entries == [expected] for entries in hooks.values()))
    hooks = copy.deepcopy(hooks)
    for entries in hooks.values():
        entries[0]["hooks"][0]["command"] = shlex.join([command, "prompt-hook", "--agent", "codex"])
    return {"hooks": hooks}


def validate_receipt(value):
    base = {"schema", "key", "codex_home", "desired_digest", "mcp", "baseline"}
    require(isinstance(value, dict) and type(value.get("schema")) is int and value["schema"] in (1, 2)
            and set(value) == (base if value["schema"] == 1 else base | {"hooks", "trust", "hooks_file_created"})
            and value["key"] == "codegraph" and isinstance(value["codex_home"], str)
            and Path(value["codex_home"]).is_absolute()
            and isinstance(value.get("desired_digest"), str) and re.fullmatch(r"[0-9a-f]{64}", value["desired_digest"])
            and isinstance(value["mcp"], dict) and (value["baseline"] is None or isinstance(value["baseline"], dict)),
            "CodeGraph native receipt 无效")
    if value["schema"] == 2:
        require(isinstance(value["hooks"], dict) and set(value["hooks"]) == set(EVENTS)
                and isinstance(value["trust"], dict) and len(value["trust"]) == 2
                and type(value["hooks_file_created"]) is bool)
        for event, native in EVENTS.items():
            entries = value["hooks"][event]
            require(isinstance(entries, list) and len(entries) == 1 and isinstance(entries[0], dict)
                    and set(entries[0]) == {"hooks"} and isinstance(entries[0]["hooks"], list) and len(entries[0]["hooks"]) == 1)
            handler = entries[0]["hooks"][0]
            require(isinstance(handler, dict) and set(handler) == {"type", "command", "timeout", "additionalContextLimit"}
                    and handler["type"] == "command" and type(handler["timeout"]) is int and handler["timeout"] == 2
                    and type(handler["additionalContextLimit"]) is int and handler["additionalContextLimit"] == 0
                    and isinstance(handler["command"], str))
            try:
                argv = shlex.split(handler["command"])
                require(len(argv) == 4 and Path(argv[0]).is_absolute() and argv[1:] == ["prompt-hook", "--agent", "codex"])
            except ValueError as error:
                raise shared.ContextModeError("CodeGraph receipt 命令无效") from error
            keys = [k for k in value["trust"] if re.fullmatch(re.escape(f"{value['codex_home']}/hooks.json:{native}:") + r"[0-9]+:0", k)]
            require(len(keys) == 1)
            item = value["trust"][keys[0]]
            require(isinstance(item, dict) and set(item) == {"trusted_hash"}
                    and isinstance(item["trusted_hash"], str) and re.fullmatch(r"sha256:[0-9a-f]{64}", item["trusted_hash"]))
    return value


def matches(owner, home, record):
    document, _ = owner._hooks(home)
    if managed(document) != record.get("hooks", {}):
        return False
    state = owner._toml(home / "config.toml").get("hooks", {}).get("state", {})
    if not all(state.get(k) == v for k, v in record.get("trust", {}).items()):
        return False
    if record.get("hooks"):
        if owner._toml(home / "config.toml").get("features", {}).get("hooks") is not True:
            return False
        return native_trust(owner, home, document["hooks"], trusted=True) == record["trust"]
    return True


def native_trust(owner, home, hooks, *, trusted=False):
    try:
        rows = [h for group in codex_hook_state.list_hooks(owner._command("context-mode"), home, home) for h in group["hooks"]]
        result = {}
        for event, entries in hooks.items():
            for index, entry in enumerate(entries):
                if not marked(entry):
                    continue
                key = f"{home / 'hooks.json'}:{EVENTS[event]}:{index}:0"
                found = [row for row in rows if row.get("key") == key]
                require(len(found) == 1, "原生状态缺少 CodeGraph Hook")
                row = found[0]
                require(row.get("sourcePath") == str(home / "hooks.json") and row.get("matcher") is None
                        and row.get("command") == entry["hooks"][0]["command"]
                        and isinstance(row.get("currentHash"), str) and re.fullmatch(r"sha256:[0-9a-f]{64}", row["currentHash"])
                        and row.get("enabled") is not False and (not trusted or row.get("trustStatus") == "trusted"),
                        "CodeGraph Hook 原生身份或信任核验失败")
                result[key] = {"trusted_hash": row["currentHash"]}
        require(len(result) == 2)
        return result
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, queue.Empty) as error:
        raise shared.ContextModeError("无法核验 CodeGraph 原生 Hook 状态") from error


def configure(owner, home, desired, receipt, *, remove=False):
    document, hooks_before = owner._hooks(home)
    require(matches(owner, home, receipt or {}), "CodeGraph Hook 或 trust 已漂移，拒绝覆盖")
    previous = receipt or {}
    old_hooks = previous.get("hooks", {})
    new_hooks = {} if remove else desired.get("hooks", {})
    projected = shared.merge_hooks(document.get("hooks", {}), old_hooks, new_hooks)
    # Keep following foreign groups at their original native trust positions on removal.
    if remove:
        projected = copy.deepcopy(document.get("hooks", {}))
        for event, entries in old_hooks.items():
            index = projected[event].index(entries[0])
            projected[event][index] = {"hooks": []}
            if index == len(projected[event]) - 1:
                projected[event].pop()
            if not projected[event]:
                projected.pop(event)
    hook_integrations.preserve_foreign_trust(home, document.get("hooks", {}), projected, marked)
    if new_hooks:
        require(owner._toml(home / "config.toml").get("features", {}).get("hooks") is True,
                "CodeGraph 主动路由要求已启用 Codex features.hooks")
    before_mcp = owner._mcp_fields("codegraph", home)
    baseline = previous.get("baseline", before_mcp)
    wanted_mcp = baseline if remove else desired["mcp"]
    receipt_before = files.read(owner._receipt_path("codegraph"), private=True)
    document["hooks"] = projected
    if not projected:
        document.pop("hooks", None)
    created = previous.get("hooks_file_created", hooks_before is None)
    hooks_after = None if remove and created and not document else files.json_bytes(document)
    writes = []
    changed_mcp = False
    config_before = files.read(home / "config.toml")
    config_after = config_before
    try:
        owner._mcp_edit("codegraph", home, wanted_mcp, before_mcp)
        changed_mcp = True
        config_after = files.read(home / "config.toml")
        if new_hooks or old_hooks:
            files.replace(home / "hooks.json", hooks_after, hooks_before)
            writes.append((home / "hooks.json", hooks_before, hooks_after))
        trust = native_trust(owner, home, projected) if new_hooks else {}
        for path, before, after in shared.auxiliary(owner, home, previous, {"trust": trust}):
            files.replace(path, after, before)
            writes.append((path, before, after))
        if new_hooks:
            require(native_trust(owner, home, projected, trusted=True) == trust,
                    "CodeGraph Hook trust 写入后核验失败")
        if remove:
            files.replace(owner._receipt_path("codegraph"), None, receipt_before)
        else:
            owner._write_receipt("codegraph", desired, previous=receipt_before, baseline=baseline,
                                 hooks_file_created=created, trust=trust)
    except BaseException as error:
        try:
            for path, before, after in reversed(writes):
                files.replace(path, before, after)
            if changed_mcp:
                files.replace(home / "config.toml", config_before, config_after)
        except BaseException as rollback:
            raise shared.ContextModeError("CodeGraph 接入恢复未完成；保留现场") from rollback
        raise shared.ContextModeError("CodeGraph 接入失败；已撤回本次条目") from error
    return "changed"
