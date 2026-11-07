"""Consume the installed v2 read-only registry plan; Lifecycle owns every write."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile
import tomllib

import tomlkit
import owned_files as files


class ContextModeError(RuntimeError):
    pass


EVENTS = {"SessionStart": "session_start", "UserPromptSubmit": "user_prompt_submit",
          "PreToolUse": "pre_tool_use", "PostToolUse": "post_tool_use"}
SKILLS = {"ctx-doctor", "ctx-stats", "ctx-upgrade"}


def _require(condition, message="context-mode v2 只读计划不符合已支持合同"):
    if not condition:
        raise ContextModeError(message)


def parse_plan(value, root: Path, home: Path, node: str):
    _require(isinstance(value, dict) and set(value) == {"command", "print"} and value["command"] == "setup")
    plan = value["print"]
    _require(isinstance(plan, dict) and set(plan) == {"agent", "steps", "verify"}
             and plan["agent"] == "codex" and plan["verify"] == "context-mode doctor --agents codex")
    steps = plan["steps"]
    _require(isinstance(steps, list) and len(steps) == 5)
    config, hook, *commands = steps
    _require(isinstance(config, dict) and set(config) == {"kind", "file", "format", "key", "block"}
             and config["kind"] == "config" and config["file"] == str(home / "config.toml")
             and config["format"] == "toml" and config["key"] == ["mcp_servers", "context-mode"])
    _require(isinstance(config["block"], str) and len(config["block"]) <= 16384)
    document = tomllib.loads(config["block"])
    _require(set(document) == {"mcp_servers", "hooks"} and set(document["mcp_servers"]) == {"context-mode"}
             and set(document["hooks"]) == {"state"})
    mcp = document["mcp_servers"]["context-mode"]
    expected = {"command": node, "args": [str(root / "server.mjs"), "--agent", "codex"],
                "omit_tools_from": ["deferred"], "default_tools_approval_mode": "approve"}
    _require(mcp == expected)
    _require(isinstance(hook, dict) and set(hook) == {"kind", "file", "block"}
             and hook["kind"] == "hook" and hook["file"] == str(home / "hooks.json")
             and isinstance(hook["block"], str) and len(hook["block"]) <= 16384)
    hook_document = json.loads(hook["block"], object_pairs_hook=files.unique_pairs)
    _require(set(hook_document) == {"hooks"})
    hooks = hook_document["hooks"]
    _require(isinstance(hooks, dict) and set(hooks) == set(EVENTS))
    trust = document["hooks"]["state"]
    _require(isinstance(trust, dict) and len(trust) == 4)
    for event, native in EVENTS.items():
        entries = hooks[event]
        _require(isinstance(entries, list) and len(entries) == 1 and isinstance(entries[0], dict))
        entry = entries[0]
        _require(set(entry) == ({"hooks", "matcher"} if event in {"PreToolUse", "PostToolUse"} else {"hooks"}))
        if "matcher" in entry:
            _require(entry["matcher"] == "^(Bash|apply_patch|mcp__context_mode__ctx_.*)$")
        dispatch = entry["hooks"]
        _require(isinstance(dispatch, list) and len(dispatch) == 1 and isinstance(dispatch[0], dict)
                 and set(dispatch[0]) == {"type", "command", "timeout"})
        command = dispatch[0]
        _require(command["type"] == "command" and type(command["timeout"]) is int and command["timeout"] == 5
                 and isinstance(command["command"], str)
                 and shlex.split(command["command"]) == [node, str(root / "hook.mjs"), "--agent", "codex"])
        key = f"{home / 'hooks.json'}:{native}:0:0"
        item = trust.get(key)
        _require(isinstance(item, dict) and set(item) == {"trusted_hash"}
                 and isinstance(item["trusted_hash"], str) and re.fullmatch(r"sha256:[0-9a-f]{64}", item["trusted_hash"]))
    skills = {}
    for step in commands:
        _require(isinstance(step, dict) and set(step) == {"kind", "file", "block"} and step["kind"] == "skill")
        name = Path(step["file"]).parent.name if isinstance(step["file"], str) else ""
        _require(name in SKILLS and name not in skills and step["file"] == str(home / "skills" / name / "SKILL.md"))
        body = step["block"]
        _require(isinstance(body, str) and len(body) <= 8192 and body.startswith(f"---\nname: {name}\n")
                 and "disable-model-invocation: true\n" in body)
        skills[name] = body
    _require(set(skills) == SKILLS)
    return {"mcp": mcp, "hooks": hooks, "trust": trust, "skills": skills}


def target(home: Path):
    root = Path.home() / ".local/lib/node_modules/@pennixrv/context-mode"
    entry = Path.home() / ".local/bin/context-mode"
    try:
        package = json.loads(files.read(root / "package.json") or b"{}", object_pairs_hook=files.unique_pairs)
        _require(package.get("name") == "@pennixrv/context-mode"
                 and re.fullmatch(r"2\.[0-9]+\.[0-9]+-pennix\.[0-9]+", package.get("version", ""))
                 and entry.resolve(strict=True) == root / "cli.mjs")
        node = shutil.which("node")
        _require(node is not None, "context-mode v2 需要受支持的 Node")
        node = str(Path(node).resolve(strict=True))
        # The only subprocess is the fixed, read-only plan request. No plan argv is executed.
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as error:
            result = subprocess.run([node, str(root / "cli.mjs"), "setup", "--print", "codex", "--json"],
                                    env={**os.environ, "CODEX_HOME": str(home)}, stdout=output, stderr=error,
                                    timeout=30, check=False)
            _require(result.returncode == 0 and output.tell() <= 131072 and error.tell() <= 16384)
            output.seek(0)
            desired = parse_plan(json.load(output, object_pairs_hook=files.unique_pairs), root, home, node)
            # Codex filters its MCP child environment. Preserve the selected native
            # home explicitly; otherwise a custom CODEX_HOME falls back to HOME/.codex.
            desired["mcp"]["env"] = {"CODEX_HOME": str(home)}
            return desired
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as error:
        raise ContextModeError("无法核验 context-mode v2 包及只读计划") from error


def validate_receipt(value):
    base = {"schema", "key", "codex_home", "desired_digest", "mcp", "hooks", "hooks_file_created"}
    _require(isinstance(value, dict) and type(value.get("schema")) is int and value["schema"] in (1, 2)
             and set(value) == (base | {"trust", "skills"} if value["schema"] == 2 else base)
             and value["key"] == "context-mode" and isinstance(value["codex_home"], str)
             and Path(value["codex_home"]).is_absolute()
             and re.fullmatch(r"[0-9a-f]{64}", value.get("desired_digest", ""))
             and isinstance(value["mcp"], dict) and isinstance(value["hooks"], dict)
             and type(value["hooks_file_created"]) is bool, "context-mode native receipt 无效")
    if value["schema"] == 2:
        _require(isinstance(value["trust"], dict) and isinstance(value["skills"], dict)
                 and set(value["skills"]) == SKILLS and len(value["trust"]) == 4)
        prefix = str(Path(value["codex_home"]) / "hooks.json")
        for key, item in value["trust"].items():
            _require(any(re.fullmatch(re.escape(f"{prefix}:{event}:") + r"[0-9]+:0", key) for event in EVENTS.values())
                     and isinstance(item, dict) and set(item) == {"trusted_hash"}
                     and isinstance(item["trusted_hash"], str) and re.fullmatch(r"sha256:[0-9a-f]{64}", item["trusted_hash"]))
        _require(all(isinstance(body, str) and len(body) <= 8192 for body in value["skills"].values()))
    return value


def trust_for_hooks(home, hooks, trust):
    mapped = {}
    for event, native in EVENTS.items():
        positions = [i for i, entry in enumerate(hooks.get(event, []))
                     if any("context-mode" in str(h.get("command", "")) for h in entry.get("hooks", []))]
        _require(len(positions) == 1, "无法唯一定位合并后的 context-mode Hook")
        prefix = f"{home / 'hooks.json'}:{native}:"
        values = [v for k, v in trust.items() if k.startswith(prefix)]
        _require(len(values) == 1)
        mapped[f"{prefix}{positions[0]}:0"] = values[0]
    return mapped


def matches(owner, home, record):
    document, _ = owner._hooks(home)
    if owner._server("context-mode", home) != record["mcp"] or owner._managed_hooks(document) != record["hooks"]:
        return False
    if record.get("schema") == 1 or "skills" not in record:
        return True
    if trust_for_hooks(home, document["hooks"], record["trust"]) != record["trust"]:
        return False
    state = owner._toml(home / "config.toml").get("hooks", {}).get("state", {})
    return (all(state.get(k) == v for k, v in record["trust"].items())
            and all(files.read(home / "skills" / name / "SKILL.md") == body.encode()
                    for name, body in record["skills"].items()))


def auxiliary(owner, home, previous, desired):
    """Preflight all owned fields, then compare-and-replace files; return an undo log."""
    old_trust, old_skills = previous.get("trust", {}), previous.get("skills", {})
    new_trust, new_skills = desired.get("trust", {}), desired.get("skills", {})
    path = home / "config.toml"
    before = files.read(path)
    document = tomlkit.parse(before.decode() if before else "")
    state = document.get("hooks", {}).get("state", {})
    for key in set(old_trust) | set(new_trust):
        _require(state.get(key) == old_trust.get(key), "context-mode trust 漂移或由其他 owner 管理")
    pending = []
    for name in set(old_skills) | set(new_skills):
        _require(name in SKILLS)
        skill = home / "skills" / name / "SKILL.md"
        raw = files.read(skill)
        _require(raw == (old_skills[name].encode() if name in old_skills else None),
                 "context-mode 命令 Skill 漂移或由其他 owner 管理")
        # Unknown siblings are never silently adopted as part of a command directory.
        if skill.parent.exists():
            _require({p.name for p in skill.parent.iterdir()} <= {"SKILL.md"}, "context-mode 命令目录含未知资产")
        pending.append((skill, raw, new_skills[name].encode() if name in new_skills else None))
    if old_trust or new_trust:
        hooks = document.setdefault("hooks", tomlkit.table())
        state = hooks.setdefault("state", tomlkit.table())
        for key in old_trust:
            state.pop(key, None)
        for key, value in new_trust.items():
            state[key] = value
        if not state:
            hooks.pop("state", None)
        if not hooks:
            document.pop("hooks", None)
        pending.insert(0, (path, before, tomlkit.dumps(document).encode()))
    return pending


def apply(owner, home, desired, receipt, *, repair=False):
    _require(owner._toml(home / "config.toml").get("features", {}).get("hooks") is True,
             "context-mode 要求已启用 Codex features.hooks")
    if receipt and not matches(owner, home, receipt):
        _require(repair and receipt.get("schema") == 1 and owner._server("context-mode", home) == receipt["mcp"],
                 "context-mode 已有归属条目漂移，拒绝覆盖")
    hooks_document, hooks_before = owner._hooks(home)
    old_hooks = owner._managed_hooks(hooks_document) if repair else (receipt["hooks"] if receipt else {})
    projected = copy.deepcopy(hooks_document.get("hooks", {}))
    for event in old_hooks:
        projected[event] = [entry for entry in projected[event] if not owner._marked(entry)]
    for event, entries in desired["hooks"].items():
        projected.setdefault(event, []).extend(entries)
    trust = trust_for_hooks(home, projected, desired["trust"])
    auxiliary(owner, home, receipt or {}, {"trust": trust, "skills": desired["skills"]})  # no writes
    previous_raw = files.read(owner._receipt_path("context-mode"), private=True)
    old_mcp = receipt["mcp"] if receipt else None
    created = receipt["hooks_file_created"] if receipt else hooks_before is None
    changed_mcp, changed_hooks, writes = False, False, []
    try:
        if old_mcp != desired["mcp"]:
            changed_mcp = True
            if old_mcp is not None:
                owner._mcp_remove(home, old_mcp)
            owner._mcp_add(home, desired["mcp"])
        owner._edit_hooks(home, old_hooks, desired["hooks"])
        changed_hooks = True
        for path, before, after in auxiliary(owner, home, receipt or {}, {"trust": trust, "skills": desired["skills"]}):
            files.replace(path, after, before)
            writes.append((path, before, after))
        record = {**desired, "trust": trust}
        _require(matches(owner, home, record), "context-mode 写入后核验失败")
        owner._write_receipt("context-mode", desired, previous=previous_raw, hooks_file_created=created, trust=trust)
    except BaseException as error:
        try:
            for path, before, after in reversed(writes):
                files.replace(path, before, after)
            if changed_hooks:
                owner._edit_hooks(home, desired["hooks"], old_hooks, remove_empty=created)
            if changed_mcp:
                current = owner._server("context-mode", home)
                core = {k: v for k, v in desired["mcp"].items() if k in {"command", "args", "env"}}
                _require(current is None or current == desired["mcp"] or current == core or current == old_mcp,
                         "MCP 写入结果未知或发生外部变化")
                if current is not None and current != old_mcp:
                    owner._mcp_remove(home, current)
                if old_mcp is not None and owner._server("context-mode", home) is None:
                    owner._mcp_add(home, old_mcp)
        except BaseException as rollback:
            raise ContextModeError("context-mode 接入失败且恢复未完成；保留现场") from rollback
        raise ContextModeError("context-mode 接入失败；已撤回本次条目") from error
    return "repaired" if repair else "changed"


def unconfigure(owner, home, receipt):
    previous = files.read(owner._receipt_path("context-mode"), private=True)
    auxiliary(owner, home, receipt, {})
    writes, removed_hooks, removed_mcp = [], False, False
    try:
        owner._mcp_remove(home, receipt["mcp"])
        removed_mcp = True
        for path, before, after in auxiliary(owner, home, receipt, {}):
            files.replace(path, after, before)
            writes.append((path, before, after))
        owner._edit_hooks(home, receipt["hooks"], {}, remove_empty=receipt["hooks_file_created"])
        removed_hooks = True
        files.replace(owner._receipt_path("context-mode"), None, previous)
    except BaseException as error:
        try:
            if removed_hooks:
                owner._edit_hooks(home, {}, receipt["hooks"])
            for path, before, after in reversed(writes):
                files.replace(path, before, after)
            current = owner._server("context-mode", home)
            _require(current is None or current == receipt["mcp"], "MCP 撤回结果未知或发生外部变化")
            if current is None:
                owner._mcp_add(home, receipt["mcp"])
        except BaseException as rollback:
            raise ContextModeError("context-mode 撤回失败且恢复未完成；保留现场") from rollback
        raise ContextModeError("context-mode 撤回失败；已恢复本组件条目") from error
    for name in receipt.get("skills", {}):
        directory = home / "skills" / name
        if directory.is_dir() and not any(directory.iterdir()):
            directory.rmdir()
    return "changed; preserved context-mode index/session data"
