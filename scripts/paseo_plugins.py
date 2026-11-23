"""Scoped lifecycle ownership for Paseo's managed Git plugins."""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
from copy import deepcopy
from pathlib import Path

import apply_journal
import component_lock
import config_model
import owned_files


class PluginError(ValueError):
    pass


def sources(catalog):
    return {key: f"paseo-git:{spec['url']}" for key, spec in catalog.get("paseo_plugins", {}).items()}


def valid_values(value, shape):
    if isinstance(shape, dict):
        return (isinstance(value, dict) and value.keys() == shape.keys()
                and all(valid_values(value[key], field) for key, field in shape.items()))
    if shape == "strings":
        return isinstance(value, list) and all(isinstance(item, str) for item in value)
    return type(value) is (bool if shape == "bool" else str)


def load_profile(path, catalog):
    raw = owned_files.read(path, limit=256 * 1024)
    try:
        profile = json.loads(raw, object_pairs_hook=owned_files.unique_pairs) if raw is not None else None
    except (ValueError, UnicodeError) as error:
        raise PluginError("Paseo 插件声明 JSON 无效") from error
    if (not isinstance(profile, dict) or set(profile) != {"schema", "plugins"}
            or type(profile["schema"]) is not int or profile["schema"] != 1
            or not isinstance(profile["plugins"], dict)
            or not profile["plugins"].keys() <= catalog.keys()):
        raise PluginError("Paseo 插件声明结构无效")
    for key, desired in profile["plugins"].items():
        spec = catalog[key]
        if (not isinstance(desired, dict) or set(desired) != {"ref", "enabled", "settings"}
                or desired["ref"] not in spec["schema_1_refs"] or type(desired["enabled"]) is not bool
                or not valid_values(desired["settings"], spec["settings"])):
            raise PluginError(f"{key} 的提交或设置超出已审核合同")
    return raw, profile["plugins"]


class Native:
    def __init__(self, home, catalog):
        self.home = home
        self.catalog = catalog

    def __call__(self, key, action="inspect", before=None, **data):
        executable = shutil.which("paseo")
        node = shutil.which("node")
        if not executable or not node:
            raise PluginError("缺少 Paseo 或 Node 原生入口")
        spec = self.catalog[key]
        request = {"id": key, "action": action, "url": spec["url"],
                   "refs": spec["schema_1_refs"], "blocks": list(spec["settings"]), **data}
        if before is not None:
            request.update(before=before, server=before["server"])
        try:
            result = subprocess.run(
                [node, str(Path(__file__).with_name("paseo-skills.mjs")), "plugins", executable, str(self.home)],
                input=json.dumps(request), capture_output=True, text=True, timeout=240, check=True)
            state = json.loads(result.stdout)
        except (subprocess.SubprocessError, ValueError) as error:
            raise PluginError(f"{key} 原生 {action} 未核验，检查 daemon、Git 身份或设置状态") from error
        plugin = state["plugin"]
        if plugin:
            if (plugin["registry"] is not None
                    and (plugin["registry"] != spec.get("adopt_registry") or plugin["ref"] != spec.get("adopt_ref"))):
                raise PluginError(f"{key} 原生注册表身份与已审核来源不符")
            if any(field["values"] is not None and not valid_values(field["values"], spec["settings"][block])
                   for block, field in plugin["settings"].items()):
                raise PluginError(f"{key} 出现未知设置字段")
        return state


def values(plugin):
    return {key: state["values"] for key, state in plugin["settings"].items()}


def changed_fields(actual, expected, prefix=""):
    if isinstance(actual, dict) and isinstance(expected, dict):
        return [path for name, value in expected.items()
                for path in changed_fields(actual.get(name), value, f"{prefix}{name}.")]
    return [prefix.removesuffix(".")] if actual != expected else []


def matches(plugin, desired):
    return bool(plugin and plugin["registry"] is None and plugin["ref"] == desired["ref"]
                and plugin["enabled"] == desired["enabled"]
                and plugin["status"] == ("running" if desired["enabled"] else "disabled")
                and values(plugin) == desired["settings"])


def preflight_install(spec, ref):
    if not shutil.which("git") or ("adopt_registry" in spec and not shutil.which("npm")):
        raise PluginError("安装前缺少 Git 或 Chat Polish 所需的 npm")
    with tempfile.TemporaryDirectory(prefix="pennix-paseo-candidate-") as directory:
        def git(*args):
            return subprocess.run(["git", "-C", directory, *args], check=True,
                                  capture_output=True, text=True, timeout=90).stdout.strip()
        try:
            git("init", "--quiet")
            git("fetch", "--quiet", "--depth=1", "--no-tags", spec["url"], ref)
            if git("rev-parse", "FETCH_HEAD") != ref:
                raise PluginError("Git 候选提交不匹配")
            git("cat-file", "-e", "FETCH_HEAD:paseo-plugin.json")
            if "adopt_registry" in spec:
                git("cat-file", "-e", "FETCH_HEAD:package-lock.json")
        except subprocess.SubprocessError as error:
            raise PluginError("固定 Git 候选或必需文件不可用，保留现有安装") from error


def _receipt_path(key):
    return owned_files.state_root() / "paseo-plugins" / f"{key}.json"


def _receipt(desired, server, home):
    return {"schema": 1, "server": server, "home": str(home), "accepted": desired}


def _lock_entry(args, key, desired):
    return {"source": args.component_lock_sources[key], "ref": desired["ref"]} if desired else None


def _set_lock(args, key, lock):
    return (component_lock.set_entry(args.component_lock, key, lock, args.component_lock_sources)
            if lock else component_lock.remove_entry(args.component_lock, key))


def recover_lock(args, active, step):
    key, target = step["component"], step["target"]
    expected = _lock_entry(args, key, target["desired"])
    if (not target.get("confirmed") or target.get("pending")
            or args.component_lock["components"].get(key) != expected
            or profile_digest(owned_files.json_bytes(_set_lock(args, key, target["previous_lock"]))) != active["lock_digest"]):
        raise PluginError("插件锁与未完成事务的检查点不符")
    apply_journal.checkpoint_lock(apply_journal.digest(args.component_lock_path))


def inspect(key, desired, state, receipt, lock, spec, home):
    plugin = state["plugin"]
    if not plugin and state.get("residue"):
        raise PluginError(f"{key} 插件缺失但设置仍存在，保留现场")
    if receipt is not None and (
            not isinstance(receipt, dict)
            or set(receipt) != {"schema", "server", "home", "accepted"}
            or receipt["schema"] != 1 or receipt["server"] != state["server"]
            or receipt["home"] != str(home)
            or not isinstance(receipt["accepted"], dict)
            or set(receipt["accepted"]) != {"ref", "enabled", "settings"}
            or receipt["accepted"]["ref"] not in spec["schema_1_refs"]
            or type(receipt["accepted"]["enabled"]) is not bool
            or not valid_values(receipt["accepted"]["settings"], spec["settings"])
            or lock != {"source": f"paseo-git:{spec['url']}", "ref": receipt["accepted"]["ref"]}):
        raise PluginError(f"{key} 本机归属与锁记录不一致")
    if receipt is None and lock is not None:
        raise PluginError(f"{key} 缺少本机归属记录")
    if desired is None:
        return "detach-required" if receipt else "unselected"
    if plugin is None:
        return "missing" if receipt else "install"
    if receipt is None:
        return "adopt"
    if plugin["registry"] or plugin["ref"] not in {receipt["accepted"]["ref"], desired["ref"]}:
        return "foreign"
    if matches(plugin, desired) and receipt["accepted"] == desired:
        return "current"
    if receipt["accepted"]["ref"] != desired["ref"]:
        return "update"
    accepted = receipt["accepted"]
    actual = values(plugin)
    if (plugin["enabled"] not in {accepted["enabled"], desired["enabled"]}
            or any(actual[block] != accepted["settings"][block] and actual[block] != expected
                   for block, expected in desired["settings"].items())):
        return "drift"
    return "configure"


def _satisfies(action, state, data, previous):
    plugin = state["plugin"]
    old = previous["plugin"]
    if action == "remove":
        return plugin is None and not state.get("residue")
    if not plugin or plugin["registry"]:
        return False
    if action in {"install", "update"}:
        return (plugin["ref"] == data["ref"] and plugin["status"] in {"running", "disabled"}
                and (action == "install" or (values(plugin) == values(old) and plugin["enabled"] == old["enabled"])))
    if plugin["path"] != old["path"] or plugin["ref"] != old["ref"]:
        return False
    if action in {"enable", "disable"}:
        expected = action == "enable"
        return (plugin["enabled"] == expected and plugin["status"] == ("running" if expected else "disabled")
                and all(value is None or value == values(plugin)[key] for key, value in values(old).items()))
    expected_values = {**values(old), data["block"]: data["values"]}
    return values(plugin) == expected_values and plugin["enabled"] == old["enabled"]


def execute(args, key, action, target, native, verify, write_lock, start):
    step_id = f"{key}:paseo-plugins"
    active = apply_journal.read()
    if active is None:
        active = start(args, "paseo-plugins", key, [
            {"id": step_id, "component": key, "action": action, "target": target}])
    step = apply_journal.step(active, step_id)
    target = deepcopy(step["target"])
    if target["fingerprint"] != args.plugin_profile_digest or step["action"] != action:
        raise PluginError("共享声明与未完成事务不同")
    desired = target["desired"]
    before = target["before"]
    state = native(key, server=before["server"])

    def checkpoint():
        nonlocal active
        active = apply_journal.set_target(active, step_id, target)

    def mutate(operation, **data):
        nonlocal state
        verify(args)
        target["pending"] = {"operation": operation, "data": data, "before": state}
        checkpoint()
        result = native(key, operation, state, **data)
        if not _satisfies(operation, result, data, state):
            raise PluginError(f"{key} 原生 {operation} 后置条件未满足")
        state = result
        target["confirmed"] = state
        target["pending"] = None
        checkpoint()

    pending = target.get("pending")
    if pending:
        if state == pending["before"]:
            mutate(pending["operation"], **pending["data"])
        elif _satisfies(pending["operation"], state, pending["data"], pending["before"]):
            target.update(pending=None, confirmed=state)
            checkpoint()
        else:
            raise PluginError(f"{key} 中断现场与操作前后状态均不符，保留事务")
    elif state != target.get("confirmed", before):
        raise PluginError(f"{key} 实际状态自计划后发生变化")

    active = apply_journal.started(active, step_id)
    plugin = state["plugin"]
    if action == "uninstall" and plugin:
        mutate("remove")
    elif action not in {"detach", "uninstall", "reconcile"}:
        if plugin and plugin["registry"]:
            mutate("remove")
            plugin = None
        if plugin is None:
            mutate("install", ref=desired["ref"])
        elif plugin["ref"] != desired["ref"]:
            mutate("update", ref=desired["ref"])
        plugin = state["plugin"]
        if not plugin["enabled"] and (desired["enabled"] or values(plugin) != desired["settings"]):
            mutate("enable")
        for block, expected in desired["settings"].items():
            current = state["plugin"]["settings"][block]
            if (current["values"] != expected
                    or (not desired["enabled"] and current["revision"] == "missing")):
                mutate("settings", block=block, values=expected)
        if not desired["enabled"] and state["plugin"]["enabled"]:
            mutate("disable")
    if action in {"detach", "uninstall"}:
        if action == "uninstall" and state["plugin"] is not None:
            raise PluginError(f"{key} 卸载尚未核验")
        receipt, lock = None, None
    else:
        if not matches(state["plugin"], desired):
            raise PluginError(f"{key} 最终状态和共享声明不同")
        receipt = _receipt(desired, state["server"], native.home)
        lock = _lock_entry(args, key, desired)
    verify(args)
    target["confirmed"] = state
    checkpoint()
    receipt_path = _receipt_path(key)
    previous = owned_files.read(receipt_path, private=True)
    output = owned_files.json_bytes(receipt) if receipt else None
    if previous != output:
        if profile_digest(previous) != target["previous_receipt"]:
            raise PluginError(f"{key} 的接管回执被外部修改")
        owned_files.replace(receipt_path, output, previous)
    old_lock = args.component_lock["components"].get(key)
    if old_lock != lock:
        args.component_lock = _set_lock(args, key, lock)
        write_lock(args)
    active = apply_journal.read()
    apply_journal.verified(active, step_id, {"state": "verified"})
    apply_journal.clear()


def profile_digest(raw):
    return hashlib.sha256(raw).hexdigest() if raw is not None else None


READ_ACTIONS = {"plan", "status", "check", "capture"}
MUTATION_STATES = {
    "apply": {"install", "configure"},
    "adopt": {"adopt"},
    "update": {"update"},
    "repair": {"missing", "drift", "configure"},
    "reconcile": {"update"},
    "detach": {"detach-required"},
    "uninstall": {"detach-required"},
}


def add_parser(subcommands, handler):
    plugins = subcommands.add_parser("paseo-plugins", help="限定范围管理 Paseo 的受管 Git 插件")
    commands = plugins.add_subparsers(dest="plugin_action", required=True)
    for action in sorted(READ_ACTIONS | MUTATION_STATES.keys() | {"recover"}):
        child = commands.add_parser(action)
        child.add_argument("--config")
        child.add_argument("--json", action="store_true")
        if action in MUTATION_STATES and action != "apply":
            child.add_argument("component", choices=["paseo-chat-polish", "phone-notify"])
        if action not in READ_ACTIONS:
            child.add_argument("--confirm", help="预览返回的摘要，或 recover 的事务 ID")
        child.set_defaults(function=handler)


def command(args, load, verify, write_lock, start, emit):
    kconf, _, catalog = load(args)
    registered = catalog["paseo_plugins"]
    action = args.plugin_action
    home = Path(config_model.config_value(kconf, "PENNIX_PASEO_HOME"))
    native = Native(home, registered)
    if action == "capture":
        profile = {}
        for key in registered:
            plugin = native(key)["plugin"]
            if not plugin or plugin["status"] != "running":
                raise PluginError(f"{key} 未运行，无法提取基线")
            profile[key] = {"ref": plugin["ref"], "enabled": plugin["enabled"], "settings": values(plugin)}
        emit({"schema": 1, "plugins": profile}, True)
        return 0

    path = Path(config_model.config_value(kconf, "PENNIX_PASEO_PLUGIN_PROFILE")).expanduser()
    if not path.is_absolute():
        raise PluginError("PENNIX_PASEO_PLUGIN_PROFILE 须指向共享声明的绝对路径")
    raw, profile = load_profile(path, registered)
    args.plugin_profile_path = path
    args.plugin_profile_digest = profile_digest(raw)
    active = apply_journal.read()
    if action == "recover":
        if not active or active["command"] != "paseo-plugins" or active["transaction"] != args.confirm:
            raise PluginError("恢复须用 --confirm 指定当前 Paseo 插件事务 ID")
        if active["config_digest"] != apply_journal.digest(args.config_path):
            raise PluginError("workflow.config 与待恢复事务不同")
        key = active["component"]
        step = apply_journal.step(active, f"{key}:paseo-plugins")
        if step["target"]["fingerprint"] != args.plugin_profile_digest:
            raise PluginError("共享声明与待恢复事务不同")
        if active["lock_digest"] != apply_journal.digest(args.component_lock_path):
            recover_lock(args, active, step)
        execute(args, key, step["action"], step["target"], native, verify, write_lock, start)
        emit({"operation": action, "component": key, "state": "verified"}, args.json)
        return 0

    targets = {}
    preview = {}
    component = getattr(args, "component", None)
    for key in [component] if component else registered:
        lock = args.component_lock["components"].get(key)
        desired = profile.get(key)
        receipt_path = _receipt_path(key)
        receipt_digest = apply_journal.digest(receipt_path)
        receipt = owned_files.read_json(receipt_path)
        if receipt is None and lock is None and desired is None:
            preview[key] = {"state": "unselected"}
            continue
        before = native(key)
        state = inspect(key, desired, before, receipt, lock, registered[key], home)
        plugin = before["plugin"]
        targets[key] = {"fingerprint": args.plugin_profile_digest, "desired": desired, "before": before,
                        "previous_lock": lock, "previous_receipt": receipt_digest}
        preview[key] = {
            "state": state, "installed_ref": plugin["ref"] if plugin else None,
            "selected_ref": desired["ref"] if desired else None, "enabled": plugin["enabled"] if plugin else None,
            "target_enabled": desired["enabled"] if desired else None,
            "source_switch": bool(plugin and plugin["registry"] and state == "adopt"),
            "settings_drift": changed_fields(values(plugin), desired["settings"]) if desired and plugin else [],
        }
    fingerprint = profile_digest(owned_files.json_bytes(
        {"action": action, "config": apply_journal.digest(args.config_path), "targets": targets}))
    if action in READ_ACTIONS or not args.confirm:
        emit({"operation": action, "plugins": preview, "profile_sha256": args.plugin_profile_digest,
              "transaction": active["transaction"] if active and active["command"] == "paseo-plugins" else None,
              **({"confirm": fingerprint} if action in MUTATION_STATES else {})}, args.json)
        return int(action == "check" and (bool(active) or any(
            item["state"] not in {"current", "unselected"} for item in preview.values())))
    if active:
        raise PluginError("存在未完成事务，先核验 recover")
    if args.confirm != fingerprint:
        raise PluginError("插件操作确认摘要不同，重新预览")
    for key, item in preview.items():
        if item["state"] in {"current", "unselected"} and action == "apply":
            targets.pop(key, None)
            continue
        if item["state"] not in MUTATION_STATES[action]:
            raise PluginError(f"{key} 状态 {item['state']} 无法执行 {action}")
        target = targets[key]
        plugin, desired = target["before"]["plugin"], target["desired"]
        if action == "adopt" and (plugin["ref"] != desired["ref"] or plugin["status"] != "running"):
            raise PluginError(f"{key} 接管需从同一提交的运行中插件开始")
        if action == "reconcile" and not matches(plugin, desired):
            raise PluginError(f"{key} 外部状态尚未达到共享声明")
        if action == "uninstall" and plugin and (
                plugin["ref"] != target["previous_lock"]["ref"] or plugin["registry"]):
            raise PluginError(f"{key} 归属不明，停止卸载")
        if action not in {"detach", "uninstall", "reconcile"} and (
                not plugin or plugin["registry"] or plugin["ref"] != desired["ref"]):
            preflight_install(registered[key], desired["ref"])
            if plugin and plugin["ref"] != desired["ref"]:
                preflight_install(registered[key], plugin["ref"])
    for key, target in targets.items():
        execute(args, key, action, target, native, verify, write_lock, start)
    emit({"operation": action, "plugins": dict.fromkeys(targets, "verified"),
          "profile_sha256": args.plugin_profile_digest}, args.json)
    return 0
