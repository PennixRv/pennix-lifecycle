"""Consume the installed v2 read-only registry plan; Lifecycle owns every write."""
from __future__ import annotations

import copy
import json
import os
import queue
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile
import tomllib

import tomlkit
import owned_files as files
from codex_hook_state import list_hooks


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
    skill_roots = set()
    for step in commands:
        _require(isinstance(step, dict) and set(step) == {"kind", "file", "block"} and step["kind"] == "skill")
        name = Path(step["file"]).parent.name if isinstance(step["file"], str) else ""
        skill_root = Path(step["file"]).parent.parent
        _require(name in SKILLS and name not in skills and skill_root in {home / "skills", Path.home() / ".agents/skills"}
                 and step["file"] == str(skill_root / name / "SKILL.md"))
        skill_roots.add(skill_root)
        body = step["block"]
        _require(isinstance(body, str) and len(body) <= 8192 and body.startswith(f"---\nname: {name}\n")
                 and "disable-model-invocation: true\n" in body)
        skills[name] = body
    _require(set(skills) == SKILLS)
    _require(len(skill_roots) == 1)
    result = {"mcp": mcp, "hooks": hooks, "trust": trust, "skills": skills}
    if skill_root != home / "skills":
        result["skill_root"] = str(skill_root)
    return result


def validate_path(value):
    _require(isinstance(value, str) and value and "\x00" not in value
             and all(Path(item).is_absolute() for item in value.split(os.pathsep)),
             "context-mode PATH 必须是非空的绝对目录列表")
    return value


def configured_path(home: Path):
    directories = []
    temporary = home / "tmp"
    for item in os.environ.get("PATH", "").split(os.pathsep):
        path = Path(item)
        if (not path.is_absolute() or not path.is_dir() or path.is_relative_to(temporary)
                or any(part.startswith("codex-arg0") for part in path.parts)):
            continue
        if item not in directories:
            directories.append(item)
    value = validate_path(os.pathsep.join(directories))
    _require(str(Path.home() / ".local/bin") in directories and shutil.which("node", path=value),
             "context-mode PATH 缺少用户 CLI 目录或 Node；请从已核验的原生环境配置")
    return value


def target(home: Path, path=None, *, agent="codex", parser=parse_plan):
    root = Path.home() / ".local/lib/node_modules/@pennixrv/context-mode"
    entry = Path.home() / ".local/bin/context-mode"
    try:
        package = json.loads(files.read(root / "package.json") or b"{}", object_pairs_hook=files.unique_pairs)
        _require(package.get("name") == "@pennixrv/context-mode"
                 and re.fullmatch(r"2\.[0-9]+\.[0-9]+-pennix\.[0-9]+", package.get("version", ""))
                 and entry.resolve(strict=True) == root / "cli.mjs")
        node = shutil.which("node", path=path)
        _require(node is not None, "context-mode v2 需要受支持的 Node")
        node = str(Path(node).resolve(strict=True))
        # The only subprocess is the fixed, read-only plan request. No plan argv is executed.
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as error:
            _require(agent in {"codex", "copilot-cli"})
            environment = {**os.environ, "CODEX_HOME" if agent == "codex" else "COPILOT_HOME": str(home)}
            if path is not None:
                environment["PATH"] = path
            result = subprocess.run([node, str(root / "cli.mjs"), "setup", "--print", agent, "--json"],
                                    env=environment, stdout=output, stderr=error,
                                    timeout=30, check=False)
            _require(result.returncode == 0 and output.tell() <= 131072 and error.tell() <= 16384)
            output.seek(0)
            desired = parser(json.load(output, object_pairs_hook=files.unique_pairs), root, home, node)
            # Codex filters its MCP child environment. Preserve the selected native
            # home explicitly; otherwise a custom CODEX_HOME falls back to HOME/.codex.
            desired["mcp"]["env"] = {"CODEX_HOME" if agent == "codex" else "COPILOT_HOME": str(home)}
            if path is not None:
                desired["mcp"]["env"]["PATH"] = validate_path(path)
            return desired
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as error:
        raise ContextModeError("无法核验 context-mode v2 包及只读计划") from error


def validate_receipt(value):
    base = {"schema", "key", "codex_home", "desired_digest", "mcp", "hooks", "hooks_file_created"}
    _require(isinstance(value, dict) and type(value.get("schema")) is int and value["schema"] in (1, 2, 3)
             and set(value) == (base | {"trust", "skills", "skill_root"} if value["schema"] == 3 else base | {"trust", "skills"} if value["schema"] == 2 else base)
             and value["key"] == "context-mode" and isinstance(value["codex_home"], str)
             and Path(value["codex_home"]).is_absolute()
             and re.fullmatch(r"[0-9a-f]{64}", value.get("desired_digest", ""))
             and isinstance(value["mcp"], dict) and isinstance(value["hooks"], dict)
             and type(value["hooks_file_created"]) is bool, "context-mode native receipt 无效")
    environment = value["mcp"].get("env", {})
    _require(isinstance(environment, dict), "context-mode native receipt env 无效")
    if "PATH" in environment:
        validate_path(environment["PATH"])
    if value["schema"] >= 2:
        _require(isinstance(value["trust"], dict) and isinstance(value["skills"], dict)
                 and set(value["skills"]) == SKILLS and len(value["trust"]) == 4)
        prefix = str(Path(value["codex_home"]) / "hooks.json")
        for key, item in value["trust"].items():
            _require(any(re.fullmatch(re.escape(f"{prefix}:{event}:") + r"[0-9]+:0", key) for event in EVENTS.values())
                     and isinstance(item, dict) and set(item) == {"trusted_hash"}
                     and isinstance(item["trusted_hash"], str) and re.fullmatch(r"sha256:[0-9a-f]{64}", item["trusted_hash"]))
        _require(all(isinstance(body, str) and len(body) <= 8192 for body in value["skills"].values()))
    if value["schema"] == 3:
        _require(value["skill_root"] == str(Path.home() / ".agents/skills"), "共享 Skill receipt 目标不符")
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


def merge_hooks(hooks, previous, desired):
    """Replace owned groups in place so other owners keep their positional trust keys."""
    merged = copy.deepcopy(hooks)
    for event in set(previous) | set(desired):
        entries = merged.get(event, [])
        positions = [i for i, entry in enumerate(entries) if entry in previous.get(event, [])]
        if positions:
            _require(len(positions) == 1 and len(previous[event]) == 1)
            entries[positions[0]:positions[0] + 1] = copy.deepcopy(desired.get(event, []))
        else:
            entries.extend(copy.deepcopy(desired.get(event, [])))
        if entries:
            merged[event] = entries
        else:
            merged.pop(event, None)
    return merged


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
            and all(files.read(Path(record.get("skill_root", home / "skills")) / name / "SKILL.md") == body.encode()
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
    old_root = Path(previous.get("skill_root", home / "skills"))
    new_root = Path(desired.get("skill_root", home / "skills"))
    old_files = {old_root / name / "SKILL.md": body.encode() for name, body in old_skills.items()}
    new_files = {new_root / name / "SKILL.md": body.encode() for name, body in new_skills.items()}
    if previous.get("skill_root") and not new_skills:
        # Other hosts and unknown Codex profiles can still consume this shared user resource.
        new_files = dict(old_files)
    for skill in old_files.keys() | new_files.keys():
        _require(skill.parent.name in SKILLS)
        raw = files.read(skill)
        _require(raw == old_files.get(skill) or skill not in old_files and raw == new_files.get(skill),
                 "context-mode 命令 Skill 漂移或由其他 owner 管理")
        # Unknown siblings are never silently adopted as part of a command directory.
        if skill.parent.exists():
            _require({p.name for p in skill.parent.iterdir()} <= {"SKILL.md"}, "context-mode 命令目录含未知资产")
        pending.append((skill, raw, new_files.get(skill)))
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


def legacy_trust(owner, home, receipt, document):
    """Adopt only native-verified trust for the exact handlers already owned by v1."""
    if not receipt or receipt.get("schema") != 1:
        return receipt or {}
    state = owner._toml(home / "config.toml").get("hooks", {}).get("state", {})
    expected = {}
    events = {**EVENTS, "PreCompact": "pre_compact", "Stop": "stop"}
    for event, entries in receipt["hooks"].items():
        _require(event in events and len(entries) == 1 and len(entries[0].get("hooks", [])) == 1)
        positions = [i for i, entry in enumerate(document["hooks"].get(event, [])) if entry == entries[0]]
        _require(len(positions) == 1, "旧 Context Mode Hook 无法唯一定位")
        key = f"{home / 'hooks.json'}:{events[event]}:{positions[0]}:0"
        if key in state:
            expected[key] = entries[0]
    if not expected:
        return receipt
    try:
        rows = [h for group in list_hooks(owner._command("context-mode"), home, home) for h in group["hooks"]]
        trust = {}
        for key, entry in expected.items():
            found = [h for h in rows if h.get("key") == key]
            _require(len(found) == 1, "原生 Hook 状态缺少旧归属条目")
            row, handler = found[0], entry["hooks"][0]
            value = state[key]
            _require(row.get("sourcePath") == str(home / "hooks.json")
                     and row.get("command") == handler["command"]
                     and row.get("matcher") == entry.get("matcher")
                     and isinstance(value, dict) and set(value) == {"trusted_hash"}
                     and row.get("currentHash") == value["trusted_hash"]
                     and row.get("trustStatus") == "trusted", "旧 Context Mode trust 与原生状态不符")
            trust[key] = dict(value)
        return {**receipt, "trust": trust}
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, queue.Empty) as error:
        raise ContextModeError("无法核验旧 Context Mode trust；保留原配置") from error


def apply(owner, home, desired, receipt, *, repair=False):
    _require(owner._toml(home / "config.toml").get("features", {}).get("hooks") is True,
             "context-mode 要求已启用 Codex features.hooks")
    if receipt and not matches(owner, home, receipt):
        _require(repair and receipt.get("schema") == 1 and owner._server("context-mode", home) == receipt["mcp"],
                 "context-mode 已有归属条目漂移，拒绝覆盖")
    hooks_document, hooks_before = owner._hooks(home)
    previous = legacy_trust(owner, home, receipt, hooks_document)
    old_hooks = owner._managed_hooks(hooks_document) if repair else (receipt["hooks"] if receipt else {})
    projected = merge_hooks(hooks_document.get("hooks", {}), old_hooks, desired["hooks"])
    trust = trust_for_hooks(home, projected, desired["trust"])
    auxiliary(owner, home, previous, {**desired, "trust": trust})  # no writes
    owner._edit_hooks(home, old_hooks, desired["hooks"], dry_run=True)
    previous_raw = files.read(owner._receipt_path("context-mode"), private=True)
    old_mcp = receipt["mcp"] if receipt else None
    created = receipt["hooks_file_created"] if receipt else hooks_before is None
    changed_mcp, changed_hooks, writes = False, False, []
    hooks_after = hooks_before
    try:
        if old_mcp != desired["mcp"]:
            changed_mcp = True
            if old_mcp is not None:
                owner._mcp_remove(home, old_mcp)
            owner._mcp_add(home, desired["mcp"])
        owner._edit_hooks(home, old_hooks, desired["hooks"])
        changed_hooks = True
        hooks_after = files.read(home / "hooks.json")
        for path, before, after in auxiliary(owner, home, previous, {**desired, "trust": trust}):
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
                files.replace(home / "hooks.json", hooks_before, hooks_after)
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
    _, hooks_before = owner._hooks(home)
    hooks_after = hooks_before
    if receipt.get("schema") == 1:
        document, _ = owner._hooks(home)
        receipt = legacy_trust(owner, home, receipt, document)
    previous = files.read(owner._receipt_path("context-mode"), private=True)
    auxiliary(owner, home, receipt, {})
    owner._edit_hooks(home, receipt["hooks"], {}, dry_run=True)
    writes, removed_hooks, removed_mcp = [], False, False
    try:
        owner._mcp_remove(home, receipt["mcp"])
        removed_mcp = True
        for path, before, after in auxiliary(owner, home, receipt, {}):
            files.replace(path, after, before)
            writes.append((path, before, after))
        owner._edit_hooks(home, receipt["hooks"], {}, remove_empty=receipt["hooks_file_created"])
        removed_hooks = True
        hooks_after = files.read(home / "hooks.json")
        files.replace(owner._receipt_path("context-mode"), None, previous)
    except BaseException as error:
        try:
            if removed_hooks:
                files.replace(home / "hooks.json", hooks_before, hooks_after)
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
    return "changed; preserved shared user skills and context-mode index/session data"
