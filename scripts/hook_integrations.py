"""Own the finite Codex Hook entries for the local AoE and OpenPeon clients."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import shlex

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
    if key == "peon-ping":
        root = Path.home() / ".openpeon"
        adapter = shlex.quote(str(root / "adapters/codex.sh"))
        command = f"if [ -f {adapter} ]; then CLAUDE_PEON_DIR={shlex.quote(str(root))} bash {adapter} >/dev/null 2>&1 || true; fi"
        return {event: [{"hooks": [{"type": "command", "command": command, "timeout": 5}]}]
                for event in ("PermissionRequest", "PreCompact", "Stop")}
    raise HookError("未知本地 Hook owner")


def _marked(key: str, entry: dict) -> bool:
    marker = "aoe-hooks" if key == "aoe" else ".openpeon/adapters/codex.sh"
    return isinstance(entry, dict) and any(isinstance(hook, dict) and marker in str(hook.get("command", ""))
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
