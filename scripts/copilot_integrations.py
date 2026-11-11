"""Copilot native configuration targets; shared Lifecycle lock and journal own writes."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess

import context_mode_integration as context
import owned_files as files

Error = context.ContextModeError
require = context._require
HOST = "copilot-cli"


def paths(key, home):
    require(key in {"codegraph", "context-mode"})
    require(home.is_absolute() and ".." not in home.parts)
    files.safe_path(home)
    token = hashlib.sha256(str(home).encode()).hexdigest()
    return (files.state_root() / "native" / HOST / key / f"{token}.json",
            files.shared_root() / "targets" / HOST / key / f"{token}.json")


def bind(key, home, *, register=False):
    receipt, target = paths(key, home)
    expected = {"schema": 1, "host": HOST, "key": key, "home": str(home),
                "user_home": str(Path.home()), "state_root": str(files.state_root())}
    before = files.read(target, private=True)
    require(before is None or files.read_json(target) == expected, "Copilot 目标已绑定其他状态目录；保留现场")
    if before is None and register:
        files.replace(target, files.json_bytes(expected), None)
    return receipt


def document(path):
    raw = files.read(path)
    try:
        value = json.loads(raw, object_pairs_hook=files.unique_pairs) if raw is not None else None
    except (ValueError, UnicodeError) as error:
        raise Error(f"Copilot 配置不是有效 JSON: {path}") from error
    require(value is None or isinstance(value, dict), f"Copilot 配置不是 JSON 对象: {path}")
    return value or {}


def server(key, home):
    servers = document(home / "mcp-config.json").get("mcpServers", {})
    require(isinstance(servers, dict), "Copilot mcpServers 不是对象")
    return servers.get(key)


def run(args, home):
    command = shutil.which(args[0])
    require(command is not None, f"原生 owner 不可用: {args[0]}")
    try:
        result = subprocess.run([command, *args[1:]], env={**os.environ, "COPILOT_HOME": str(home)},
                                capture_output=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise Error("Copilot 原生 owner 未完成；保留事务") from error
    require(result.returncode == 0, f"Copilot 原生 owner 拒绝操作（退出码 {result.returncode}）")
    return result.stdout


def parse_plan(value, root, home, node):
    require(isinstance(value, dict) and set(value) == {"command", "print"} and value["command"] == "setup")
    plan = value["print"]
    require(isinstance(plan, dict) and set(plan) == {"agent", "steps", "verify"}
            and plan["agent"] == HOST and plan["verify"] == "context-mode doctor --agents copilot-cli")
    steps = plan["steps"]
    require(isinstance(steps, list) and len(steps) == 5)
    mcp, hook, *skills = steps
    require(mcp == {"kind": "run", "argv": ["copilot", "mcp", "add", "context-mode", "--",
                                            node, str(root / "server.mjs"), "--agent", HOST]})
    require(isinstance(hook, dict) and set(hook) == {"kind", "file", "block"}
            and hook["kind"] == "hook" and hook["file"] == str(home / "hooks/context-mode.json")
            and isinstance(hook["block"], str) and len(hook["block"]) <= 16384)
    hooks = json.loads(hook["block"], object_pairs_hook=files.unique_pairs)
    require(isinstance(hooks, dict) and set(hooks) == {"version", "hooks"} and hooks["version"] == 1
            and isinstance(hooks["hooks"], dict)
            and set(hooks["hooks"]) == {"sessionStart", "userPromptSubmitted", "preToolUse", "postToolUse"})
    for entries in hooks["hooks"].values():
        require(isinstance(entries, list) and len(entries) == 1 and isinstance(entries[0], dict))
        entry = entries[0]
        require(set(entry) == {"type", "command", "powershell", "timeoutSec"}
                and entry["type"] == "command" and entry["timeoutSec"] == 5
                and isinstance(entry["command"], str) and entry["powershell"] == "& " + entry["command"]
                and shlex.split(entry["command"]) == [node, str(root / "hook.mjs"), "--agent", HOST])
    content = {hook["file"]: hook["block"]}
    names = set()
    for step in skills:
        require(isinstance(step, dict) and set(step) == {"kind", "file", "block"}
                and step["kind"] == "skill" and isinstance(step["file"], str))
        name = Path(step["file"]).parent.name
        require(name in context.SKILLS and name not in names
                and step["file"] == str(home / "skills" / name / "SKILL.md"))
        raw = files.read(root / "pi/skills" / name / "SKILL.md")
        require(raw is not None and isinstance(step["block"], str) and len(step["block"]) <= 8192)
        expected = raw.decode()
        for tool in ("doctor", "stats", "upgrade"):
            expected = expected.replace(f"ctx_{tool}", f"context-mode-ctx_{tool}")
        require(step["block"] == expected, "Context Mode 命令 Skill 与固定包模板不一致")
        content[step["file"]] = step["block"]
        names.add(name)
    require(names == context.SKILLS)
    return {"mcp": {"type": "local", "command": node, "args": [str(root / "server.mjs"), "--agent", HOST],
                    "tools": ["*"]}, "files": content}


def desired(key, home):
    if key == "codegraph":
        require(shutil.which("codegraph") is not None, "CodeGraph owner 不可用")
        return {"mcp": {"type": "stdio", "command": "codegraph", "args": ["serve", "--mcp"],
                        "tools": ["*"], "deferTools": "never"}, "files": {}}
    value = context.target(home, context.configured_path(home), agent=HOST, parser=parse_plan)
    return value


def policy(home):
    settings = document(home / "settings.json")
    require(settings.get("disableAllHooks") is not True, "Copilot 整体禁用 Hooks；不自动改变用户策略")
    disabled = settings.get("disabledHooks", [])
    require(isinstance(disabled, list) and all(isinstance(x, str) for x in disabled), "Copilot disabledHooks 无效")
    require(not any("context-mode" in x for x in disabled), "Copilot 已禁用 Context Mode Hooks；保留用户选择")


def verify_hooks(home):
    output = run(["context-mode", "doctor", "--agents", HOST, "--json"], home)
    require(len(output) <= 1024 * 1024, "Context Mode doctor 输出超过核验上限")
    try:
        value = json.loads(output, object_pairs_hook=files.unique_pairs)
        rows = [a for a in value["agents"] if a["id"] == HOST]
        checks = [c for a in rows for c in a["checks"] if c["name"] == "hook-enabled"]
        require(len(rows) == 1 and len(checks) == 1 and checks[0].get("ok") is True,
                "Copilot 未证明自有 Hooks 已启用；不绕过用户或管理策略")
    except (ValueError, KeyError, TypeError) as error:
        raise Error("Copilot Hook 原生核验结果未知") from error


def read(key, home):
    path = bind(key, home)
    value = files.read_json(path)
    if value is None:
        return None
    validate_record(value, key, home)
    return value


def validate_record(value, key, home, *, previous=False):
    require(isinstance(value, dict) and set(value) == {"schema", "host", "key", "home", "desired", "baseline", "directories", "skill_directories_before", "operation", "rollback"}
            and value["schema"] == 1 and value["host"] == HOST and value["key"] == key and value["home"] == str(home),
            "Copilot receipt 未知、损坏或目标不匹配")
    operation = value["operation"]
    require(isinstance(operation, dict) and set(operation) == {"id", "action"}
            and operation["action"] in {"configure", "unconfigure"}
            and (operation["id"] is None or isinstance(operation["id"], str) and operation["id"]),
            "Copilot 事务步骤身份无效")
    allowed = {str(home / "hooks/context-mode.json"), *(str(home / "skills" / n / "SKILL.md") for n in context.SKILLS)} if key == "context-mode" else set()
    snapshots = [value["desired"], value["baseline"]]
    rollback = value["rollback"]
    if rollback is not None:
        require(not previous and isinstance(rollback, dict) and set(rollback) == {"state", "receipt", "skill_directories_before"},
                "Copilot 恢复快照无效或已有未完成配置")
        snapshots.append(rollback["state"])
        if rollback["receipt"] is not None:
            validate_record(rollback["receipt"], key, home, previous=True)
        require(rollback["skill_directories_before"] is None or isinstance(rollback["skill_directories_before"], list)
                and all(isinstance(x, str) for x in rollback["skill_directories_before"]), "Copilot 恢复目录快照无效")
    for item in snapshots:
        require(isinstance(item, dict) and set(item) == {"mcp", "files"} and isinstance(item["files"], dict)
                and set(item["files"]) == allowed and (item["mcp"] is None or isinstance(item["mcp"], dict))
                and all(v is None or isinstance(v, str) and len(v) <= 16384 for v in item["files"].values()),
                "Copilot receipt 内容边界无效")
    collections = {str(Path.home() / ".agents/skills" / n) for n in ("pennix-skills", "pennix-skills-extra")}
    require(isinstance(value["directories"], list) and all(isinstance(x, str) and x in collections for x in value["directories"])
            and len(value["directories"]) == len(set(value["directories"]))
            and (key == "context-mode" or not value["directories"]), "Copilot Skill 目录归属记录无效")
    require(value["skill_directories_before"] is None or isinstance(value["skill_directories_before"], list)
            and all(isinstance(x, str) for x in value["skill_directories_before"]), "Copilot Skill 基线无效")


def matches(key, home, value):
    return server(key, home) == value["mcp"] and all(
        files.read(Path(p)) == (None if body is None else body.encode()) for p, body in value["files"].items())


def inspect(key, home, *, enabled=True):
    record = read(key, home)
    if record is None:
        present = server(key, home) is not None or key == "context-mode" and any(
            files.read(home / part) is not None for part in ["hooks/context-mode.json", *(f"skills/{n}/SKILL.md" for n in context.SKILLS)])
        return {"state": "external" if present else "absent", "owned": False, "detail": "Copilot 独立目标尚无归属回执"}
    if not matches(key, home, record["desired"]):
        return {"state": "drifted", "owned": True, "detail": "Copilot 受管配置已漂移或事务未完成"}
    directories = document(home / "settings.json").get("skillDirectories", [])
    if not isinstance(directories, list) or any(x not in directories for x in record["directories"]):
        return {"state": "drifted", "owned": True, "detail": "Copilot 受管 Skill 目录登记已改变"}
    if key == "context-mode" and enabled:
        policy(home)
        verify_hooks(home)
    state = "current" if not enabled or desired(key, home) == record["desired"] else "update-required"
    return {"state": state, "owned": True, "detail": "Copilot 独立 native receipt"}


def validate(key, home, action):
    status = inspect(key, home, enabled=action == "configure")
    require(status["state"] not in {"external", "drifted", "blocked"}, "Copilot 配置漂移或归属不明；保留现场")
    if action == "configure":
        desired(key, home)
        if key == "context-mode":
            policy(home)
    return status


def restore_server(key, home, expected, baseline):
    require(server(key, home) == expected, "Copilot MCP 操作期间改变；拒绝覆盖")
    path = home / "mcp-config.json"
    before = files.read(path)
    value = document(path)
    if baseline is None:
        value.get("mcpServers", {}).pop(key, None)
    else:
        value.setdefault("mcpServers", {})[key] = baseline
    files.replace(path, files.json_bytes(value), before, mode=path.stat().st_mode & 0o777 if before is not None else 0o600)


def apply(key, home, *, repair=False, operation_id=None):
    effect = ["none"]
    try:
        return _apply(key, home, repair=repair, operation_id=operation_id, effect=effect)
    except (Error, files.OwnershipError, OSError, ValueError) as cause:
        error = Error(str(cause))
        error.effect = effect[0]
        raise error from cause


def _apply(key, home, *, repair, operation_id, effect):
    record = read(key, home)
    require(record is None or record["rollback"] is None, "Copilot 已有未完成配置；先恢复同一事务")
    if repair and record:
        require(server(key, home) in (None, record["desired"]["mcp"]), "Copilot MCP 有未知改动；不能修复覆盖")
        require(all(files.read(Path(p)) in (None, body.encode()) for p, body in record["desired"]["files"].items()),
                "Copilot 文件有未知改动；不能修复覆盖")
        if key == "context-mode":
            policy(home)
    else:
        validate(key, home, "configure")
    want = desired(key, home)
    if record and record["desired"] == want and inspect(key, home)["state"] == "current":
        return "no-op"
    baseline = record["baseline"] if record else {"mcp": server(key, home), "files": {
        p: None if (body := files.read(Path(p))) is None else body.decode() for p in want["files"]}}
    directories = list(record["directories"]) if record else []
    old = {"mcp": server(key, home), "files": {
        p: None if (body := files.read(Path(p))) is None else body.decode() for p in want["files"]}}
    path = bind(key, home)
    pending = {"schema": 1, "host": HOST, "key": key, "home": str(home),
               "desired": want, "baseline": baseline, "directories": directories,
               "operation": {"id": operation_id, "action": "configure"},
               "rollback": {"state": old, "receipt": record,
                            "skill_directories_before": document(home / "settings.json").get("skillDirectories")},
               "skill_directories_before": record["skill_directories_before"] if record else
                   document(home / "settings.json").get("skillDirectories")}
    before_receipt = files.read(path)
    effect[0] = "unknown"
    bind(key, home, register=True)
    # Persist the exact result and baseline before invoking an external writer.
    files.replace(path, files.json_bytes(pending), before_receipt)
    try:
        if key == "codegraph":
            run(["codegraph", "install", "--target", HOST, "--location", "global", "--yes"], home)
        else:
            if old["mcp"] is not None:
                run(["copilot", "mcp", "remove", key], home)
            mcp = want["mcp"]
            args = ["copilot", "mcp", "add", key]
            for name, value in mcp.get("env", {}).items():
                args += ["--env", f"{name}={value}"]
            run([*args, "--", mcp["command"], *mcp["args"]], home)
        require(server(key, home) == want["mcp"], "Copilot MCP 安装结果与固定计划不一致")
        for p, body in want["files"].items():
            prior = old["files"][p]
            files.replace(Path(p), body.encode(), None if prior is None else prior.encode())
        settings = document(home / "settings.json")
        registered = settings.get("skillDirectories", [])
        require(isinstance(registered, list), "Copilot Skill 目录列表无效")
        listed = json.loads(run(["copilot", "skill", "list", "--json"], home))
        require(isinstance(listed, list), "Copilot Skill 清单无效")
        for name in ("pennix-skills", "pennix-skills-extra") if key == "context-mode" else ():
            directory = Path.home() / ".agents/skills" / name
            if directory.is_dir() and str(directory) not in registered and not any(
                    isinstance(x, dict) and isinstance(x.get("path"), str)
                    and Path(x["path"]).is_relative_to(directory) for x in listed):
                if str(directory) not in directories:
                    directories.append(str(directory))
                files.replace(path, files.json_bytes(pending), files.read(path))
                run(["copilot", "skill", "add", str(directory)], home)
        require(inspect(key, home)["state"] == "current", "Copilot 配置后置条件未通过")
        pending["rollback"] = None
        files.replace(path, files.json_bytes(pending), files.read(path))
    except (Error, OSError, ValueError) as error:
        # Recovery only consumes exact recorded values; never overwrite an unknown partial effect.
        raise Error("Copilot 操作未完成；原生 recover 必须核验同一目标与后置条件") from error
    return "changed"


def complete(key, home, *, operation_id):
    """Finalize the same configure step only after its native postconditions hold."""
    record = read(key, home)
    require(record is not None and record["operation"] == {"id": operation_id, "action": "configure"},
            "Copilot 完成核验必须匹配同一配置步骤")
    require(inspect(key, home)["state"] == "current", "Copilot 完成核验未满足后置条件")
    if record["rollback"] is not None:
        path = bind(key, home)
        before = files.read(path)
        record["rollback"] = None
        files.replace(path, files.json_bytes(record), before)


def restore_directories(home, record):
    path = home / "settings.json"
    registered = document(path).get("skillDirectories", [])
    require(isinstance(registered, list), "Copilot Skill 目录清单无效")
    for directory in record["directories"]:
        if directory in registered:
            run(["copilot", "skill", "remove", directory], home)
    before = files.read(path)
    settings = document(path)
    if record["directories"] and record["skill_directories_before"] is None and settings.get("skillDirectories") == []:
        settings.pop("skillDirectories")
        files.replace(path, files.json_bytes(settings), before)


def unconfigure(key, home, *, recovery=False, operation_id=None):
    effect = ["none"]
    try:
        return _unconfigure(key, home, recovery=recovery, operation_id=operation_id, effect=effect)
    except (Error, files.OwnershipError, OSError, ValueError) as cause:
        error = Error(str(cause))
        error.effect = effect[0]
        raise error from cause


def _unconfigure(key, home, *, recovery, operation_id, effect):
    if not recovery:
        validate(key, home, "unconfigure")
    record = read(key, home)
    if record is None:
        return "no-op"
    path = bind(key, home)
    if recovery:
        require(record["operation"] == {"id": operation_id, "action": "unconfigure"}, "Copilot 恢复不属于同一撤回步骤")
    else:
        record["operation"] = {"id": operation_id, "action": "unconfigure"}
        effect[0] = "unknown"
        files.replace(path, files.json_bytes(record), files.read(path))
    current = server(key, home)
    require(current in (record["desired"]["mcp"], record["baseline"]["mcp"]), "Copilot MCP 存在未知改动")
    changes = []
    for p, body in record["baseline"]["files"].items():
        before = files.read(Path(p))
        baseline = None if body is None else body.encode()
        require(before in (baseline, record["desired"]["files"][p].encode()), "Copilot 文件存在未知改动")
        changes.append((Path(p), baseline, before))
    effect[0] = "unknown"
    restore_directories(home, record)
    for path, baseline, before in changes:
        files.replace(path, baseline, before)
    restore_server(key, home, current, record["baseline"]["mcp"])
    path = bind(key, home)
    files.replace(path, None, files.read(path))
    return "changed"


def recover(key, home, *, operation_id=None):
    """Restore a pending configure only when every effect matches a recorded value."""
    record = read(key, home)
    require(record is not None and record["operation"] == {"id": operation_id, "action": "configure"},
            "Copilot 缺少同一配置步骤的原生恢复记录；保留 unknown")
    rollback = record["rollback"]
    require(rollback is not None, "Copilot 没有未完成配置的恢复快照；保留现场")
    prior = rollback["state"]
    current = server(key, home)
    require(current in (record["desired"]["mcp"], prior["mcp"], None),
            "Copilot MCP 存在未知改动；不能恢复")
    changes = []
    for p, body in prior["files"].items():
        before = files.read(Path(p))
        baseline = None if body is None else body.encode()
        require(before in (baseline, record["desired"]["files"][p].encode()), "Copilot 文件存在未知改动；不能恢复")
        changes.append((Path(p), baseline, before))
    previous = rollback["receipt"]
    restore_directories(home, {"directories": [d for d in record["directories"]
        if previous is None or d not in previous["directories"]],
        "skill_directories_before": rollback["skill_directories_before"]})
    for path, baseline, before in changes:
        files.replace(path, baseline, before)
    restore_server(key, home, current, prior["mcp"])
    path = bind(key, home)
    files.replace(path, None if previous is None else files.json_bytes(previous), files.read(path))
    return "restored"
