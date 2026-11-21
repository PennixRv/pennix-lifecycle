"""Own the registered AoE Codex Hook entries."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import tomllib

import owned_files as files


class HookError(RuntimeError):
    pass


def read_document(home: Path) -> tuple[dict, bytes | None]:
    raw = files.read(home / "hooks.json")
    try:
        value = json.loads(raw, object_pairs_hook=files.unique_pairs) if raw is not None else {}
    except (ValueError, UnicodeError) as error:
        raise HookError("hooks.json 不是有效 JSON") from error
    if (not isinstance(value, dict) or not isinstance(value.get("hooks", {}), dict)
            or any(not isinstance(entries, list) for entries in value.get("hooks", {}).values())):
        raise HookError("hooks.json 结构无效")
    return value, raw


def _path(key: str) -> Path:
    return files.state_root() / "hooks" / f"{key}.json"


def desired(key: str) -> dict:
    if key == "aoe":
        command = "sh -c '[ -n \"$AOE_INSTANCE_ID\" ] || exit 0; [ -n \"$AOE_HOOK_BIN\" ] || exit 0; [ -x \"$AOE_HOOK_BIN\" ] || exit 0; \"$AOE_HOOK_BIN\" __extract-session-id --field session-id --agent codex 2>/dev/null; exit 0 # aoe-hooks'"
        return {"SessionStart": [{"hooks": [{"type": "command", "command": command}]}]}
    raise HookError("未知本地 Hook owner")


def _marked(key: str, entry: dict) -> bool:
    return key == "aoe" and isinstance(entry, dict) and any(isinstance(hook, dict) and "aoe-hooks" in str(hook.get("command", ""))
                                           for hook in entry.get("hooks", []))


def _read(key: str, home: Path) -> tuple[dict, dict, bytes | None]:
    document, raw = read_document(home)
    selected = {event: marked for event, entries in document.get("hooks", {}).items()
                if (marked := [entry for entry in entries if _marked(key, entry)])}
    return document, selected, raw


def _receipt(key: str, home: Path) -> dict | None:
    record = files.read_json(_path(key))
    if record is not None and (not isinstance(record, dict) or set(record) != {"schema", "home", "hooks"}
                               or record["schema"] != 1 or record["home"] != str(home) or not isinstance(record["hooks"], dict)):
        raise HookError("本地 Hook receipt 无效或属于其他 CODEX_HOME")
    return record


def inspect(key: str, home: Path, *, enabled: bool = True) -> dict:
    _, current, _ = _read(key, home)
    record = _receipt(key, home)
    if record is None:
        return {"state": "external" if current else "absent", "owned": False}
    return {"state": ("current" if current == desired(key) else "update-required")
            if current == record["hooks"] else "drifted", "owned": True}


def preserve_foreign_trust(home: Path, before: dict, after: dict, owned) -> None:
    raw = files.read(home / "config.toml")
    try:
        trust = tomllib.loads(raw.decode() if raw else "").get("hooks", {}).get("state", {})
    except (UnicodeError, tomllib.TOMLDecodeError) as error:
        raise HookError("无法核验原生 Hook trust；保留现场") from error
    if not isinstance(trust, dict):
        raise HookError("原生 Hook trust 结构无效")
    events = {"SessionStart": "session_start", "PreToolUse": "pre_tool_use", "PostToolUse": "post_tool_use",
              "UserPromptSubmit": "user_prompt_submit", "PreCompact": "pre_compact", "Stop": "stop",
              "PermissionRequest": "permission_request"}
    for event, entries in before.items():
        for index, entry in enumerate(entries):
            if owned(entry) or event not in events:
                continue
            positions = [i for i, item in enumerate(after.get(event, [])) if item == entry]
            if positions == [index]:
                continue
            prefix = f"{home / 'hooks.json'}:{events[event]}:{index}:"
            if any(key.startswith(prefix) for key in trust):
                raise HookError("操作会移动其他 owner 的 trusted Hook；保留现场")


def apply(key: str, home: Path, *, adopt: bool = False, remove: bool = False) -> str:
    record = _receipt(key, home)
    document, current, before = _read(key, home)
    if record and record["hooks"] != current:
        raise HookError("本地 Hook 已漂移，拒绝覆盖")
    if remove and record is None:
        return "preserved"
    want = {} if remove else desired(key)
    if record is None and current and (not adopt or current != want):
        raise HookError("本地 Hook 需显式接管且必须匹配已核验的原生条目")
    original_hooks = copy.deepcopy(document.get("hooks", {}))
    hooks = document.setdefault("hooks", {})
    for event in set(current) | set(want):
        remaining = iter(copy.deepcopy(want.get(event, [])))
        entries = []
        for entry in hooks.get(event, []):
            if _marked(key, entry):
                replacement = next(remaining, None)
                if replacement is not None:
                    entries.append(replacement)
            else:
                entries.append(entry)
        entries.extend(remaining)
        if entries:
            hooks[event] = entries
        else:
            hooks.pop(event, None)
    if not hooks:
        document.pop("hooks", None)
    preserve_foreign_trust(home, original_hooks, hooks, lambda entry: _marked(key, entry))
    after = files.json_bytes(document) if document else None
    receipt_before = files.read(_path(key), private=True)
    receipt_after = None if remove else files.json_bytes({"schema": 1, "home": str(home), "hooks": want})
    files.replace(home / "hooks.json", after, before)
    try:
        files.replace(_path(key), receipt_after, receipt_before)
    except BaseException:
        files.replace(home / "hooks.json", before, after)
        raise
    return "changed" if before != after or receipt_before != receipt_after else "no-op"
