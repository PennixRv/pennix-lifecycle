#!/usr/bin/env python3
"""Pennix lifecycle command line interface."""

from __future__ import annotations

import argparse
import copy
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import _engine as owner_engine
import codex_config
import config_model
import credential_import
import path_env
import paseo_service
import relay_owner
import secret_store


ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "references" / "catalog.json"


class CliError(RuntimeError):
    """A user-facing lifecycle error."""


def _expand_path(value: str) -> Path:
    return Path(os.path.expanduser(os.path.expandvars(value)))


def _args_for_engine(args: argparse.Namespace, command: str) -> argparse.Namespace:
    args.command = command
    args.codex_home = _expand_path(args.codex_home)
    args.home_directory = Path.home()
    args.destination = None
    args.yes = True
    return args


def _load(args: argparse.Namespace) -> tuple[config_model.StrictKconfig, Path, dict[str, Any]]:
    kconf, path = config_model.load(args.config)
    try:
        catalog = owner_engine.load_catalog(CATALOG)
    except Exception as error:
        raise CliError(f"组件目录无效: {error}") from error
    catalog = copy.deepcopy(catalog)
    npm_prefix = config_model.config_value(kconf, "PENNIX_NPM_PREFIX")
    for component in catalog["components"].values():
        package = component.get("package")
        if isinstance(package, dict) and package.get("source") == "npm":
            package["prefix"] = npm_prefix
    return kconf, path, catalog


def _component_status(args: argparse.Namespace, component_id: str, component: dict[str, Any]) -> dict[str, Any]:
    if component_id == "codex-config":
        values = config_model.values(args.kconf)
        return codex_config.inspect(args.codex_home / "config.toml", values)
    try:
        state, detail = owner_engine.probe_component(
            component, args.codex_home, home_directory=Path.home()
        )
    except Exception as error:
        return {"state": "blocked", "detail": str(error)}
    return {"state": state, "detail": detail}


def _planned_action(component: dict[str, Any], action: str) -> tuple[str, str | None]:
    if action not in {"install", "uninstall"}:
        return action, None
    owner = component.get("actions", {}).get(action)
    if owner == "native-owner":
        return "native-owner", action
    if owner != "managed":
        return "keep", None
    return action, None


def _valid_endpoint(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.hostname) and parsed.username is None and parsed.password is None


def _connection_plans(kconf: config_model.StrictKconfig) -> dict[str, dict[str, Any]]:
    try:
        configured = secret_store.load()
        secret_error = None
    except secret_store.SecretError as error:
        configured = {}
        secret_error = str(error)
    definitions = (
        ("siyuan", "PENNIX_TARGET_SIYUAN", "PENNIX_SIYUAN_URL", "siyuan.api_token", None),
        ("cch", "PENNIX_TARGET_CCH", "PENNIX_CCH_URL", "cch.admin_token", "cch-status"),
        ("hikari", "PENNIX_TARGET_HIKARI", "PENNIX_HIKARI_URL", "hikari.api_token", "tavily-hikari"),
        ("grok", "PENNIX_TARGET_GROK", "PENNIX_GROK_URL", "grok.api_key", "pennix-skills"),
        ("grok-tavily", "PENNIX_TARGET_GROK_TAVILY", "PENNIX_GROK_TAVILY_URL", "grok.tavily_api_key", "pennix-skills"),
        ("windsurf", "PENNIX_TARGET_WINDSURF", None, "windsurf.api_key", "pennix-skills"),
    )
    result: dict[str, dict[str, Any]] = {}
    for name, flag, endpoint_key, secret, component in definitions:
        selected = bool(kconf.syms[flag].tri_value)
        item: dict[str, Any] = {
            "selected": selected,
            "action": "ignored" if not selected else "blocked",
            "state": "disabled" if not selected else "blocked",
        }
        if not selected:
            result[f"integration-{name}"] = item
            continue
        missing: list[str] = []
        if component:
            component_symbol = next(
                (symbol for symbol, component_id in config_model.COMPONENT_IDS.items() if component_id == component),
                None,
            )
            if component_symbol is None or not bool(kconf.syms[component_symbol].tri_value):
                missing.append(f"component:{component}")
        endpoint = config_model.config_value(kconf, endpoint_key) if endpoint_key else ""
        if endpoint_key and not _valid_endpoint(endpoint):
            missing.append(f"endpoint:{endpoint_key}")
        if secret_error:
            missing.append("secrets.toml")
        elif secret not in configured:
            missing.append(f"secret:{secret}")
        if missing:
            item["detail"] = ", ".join(missing)
        else:
            item["action"] = "native-owner"
            item["state"] = "native-owner-required"
            item["detail"] = "目标连接由对应组件原生入口完成"
        result[f"integration-{name}"] = item
    return result


def _relay_plan(args: argparse.Namespace, kconf: config_model.StrictKconfig) -> dict[str, Any] | None:
    paseo_home = _expand_path(config_model.config_value(kconf, "PENNIX_PASEO_HOME"))
    selected = bool(kconf.syms["PENNIX_PASEO_RELAY"].tri_value)
    if not selected:
        if not relay_owner.local_owned():
            return None
        return {"selected": False, "action": "uninstall", "state": "owned", "detail": "本工具登记项待撤回"}
    endpoint = config_model.config_value(kconf, "PENNIX_PASEO_RELAY_ENDPOINT")
    public_endpoint = config_model.config_value(kconf, "PENNIX_PASEO_RELAY_PUBLIC_ENDPOINT")
    try:
        relay_owner.validate_config(endpoint, public_endpoint)
        state = relay_owner.inspect(paseo_home)
        return {
            "selected": True,
            "action": "keep" if state == "present" else "install",
            "state": state,
            "detail": "既有登记项" if state == "present" else "等待登记",
        }
    except relay_owner.RelayError as error:
        return {"selected": True, "action": "blocked", "state": "blocked", "detail": str(error)}


def _plan(args: argparse.Namespace) -> dict[str, Any]:
    kconf, path, catalog = _load(args)
    args.kconf = kconf
    args.codex_home = _expand_path(config_model.config_value(kconf, "PENNIX_CODEX_HOME"))
    args.paseo_home = _expand_path(config_model.config_value(kconf, "PENNIX_PASEO_HOME"))
    selected = config_model.selected_components(kconf)
    components: dict[str, Any] = {}
    for component_id, component in catalog["components"].items():
        if component.get("delivery") == "state":
            components[component_id] = {"selected": False, "action": "ignored", "state": "runtime-state"}
            continue
        status = _component_status(args, component_id, component)
        if component_id in selected:
            action = "keep" if status["state"] in {"match", "current", "present"} else "install"
        else:
            action = "uninstall" if status["state"] in {"match", "current", "present"} else "keep"
            if action == "uninstall" and component.get("delivery") == "collection" and not owner_engine.collection_owned(component, args.codex_home):
                action = "keep"
                status["ownership"] = "external"
        action, owner_operation = _planned_action(component, action)
        if owner_operation:
            status["owner_action"] = owner_operation
        components[component_id] = {"selected": component_id in selected, "action": action, **status}
    prefix = _expand_path(config_model.config_value(kconf, "PENNIX_NPM_PREFIX"))
    paseo = prefix / "bin" / "paseo"
    if not paseo.is_file():
        paseo = Path(shutil.which("paseo") or paseo)
    mode = "linger" if kconf.syms["PENNIX_PASEO_AUTOSTART_LINGER"].tri_value else (
        "login" if kconf.syms["PENNIX_PASEO_AUTOSTART_LOGIN"].tri_value else "off"
    )
    service_state = paseo_service.inspect(paseo, args.paseo_home)
    service_action = "keep" if (mode == "off" and service_state == "absent") or (mode != "off" and service_state == "current") else (
        "uninstall" if mode == "off" else "install"
    )
    components["paseo-autostart"] = {
        "selected": mode != "off", "mode": mode, "action": service_action, "state": service_state,
    }
    components.update(_connection_plans(kconf))
    relay = _relay_plan(args, kconf)
    if relay is not None:
        components["paseo-relay"] = relay
    return {"config": str(path), "codex_home": str(args.codex_home), "components": components}


def _emit(value: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))
        return
    if isinstance(value, dict) and "components" in value:
        print(f"配置: {value['config']}")
        print(f"Codex: {value['codex_home']}")
        for key, item in value["components"].items():
            print(f"{key}: {item['action']} ({item['state']})")
        return
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _ensure_dependencies(selected: set[str], component_id: str) -> None:
    dependencies = {
        "tmux-config": {"tmux"},
        "ponytail-plugin": {"codex-cli"},
        "codex-config": {"codex-cli"},
        "codex-agents": {"codex-cli"},
    }
    missing = dependencies.get(component_id, set()) - selected
    if missing:
        raise CliError(f"{component_id} 依赖未选择: {', '.join(sorted(missing))}")


def _run_component(args: argparse.Namespace, catalog: dict[str, Any], component_id: str, operation: str) -> str:
    component = catalog["components"][component_id]
    if component_id == "codex-config":
        if operation == "uninstall":
            return codex_config.uninstall(args.codex_home / "config.toml")
        values = config_model.values(args.kconf)
        return codex_config.apply(args.codex_home / "config.toml", values)
    if component.get("actions", {}).get(operation) != "managed":
        raise CliError(f"{component_id} {operation} 不归 pennix-lifecycle 管理")
    engine_args = _args_for_engine(args, operation)
    try:
        if component.get("delivery") in {"static", "collection"}:
            return owner_engine.static_operation(engine_args, component_id, component, operation, catalog)
        return owner_engine.component_operation(engine_args, catalog, component_id, component, operation)
    except Exception as error:
        raise CliError(f"{component_id} {operation} 失败: {error}") from error


def cmd_config_init(args: argparse.Namespace) -> int:
    path = (
        config_model.init_from_defconfig(args.defconfig, args.config, force=args.force)
        if args.defconfig
        else config_model.init(args.config, force=args.force)
    )
    print(path)
    return 0


def cmd_config_check(args: argparse.Namespace) -> int:
    kconf, path = config_model.load(args.config)
    _emit({"config": str(path), "values": config_model.values(kconf)}, args.json)
    return 0


def cmd_config_edit(args: argparse.Namespace) -> int:
    print(config_model.edit(args.config))
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    value = _plan(args)
    _emit(value, args.json)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    value = _plan(args)
    _emit(value, args.json)
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    value = _plan(args)
    _emit(value, args.json)
    bad = [item for item in value["components"].values() if item["selected"] and item["state"] not in {"match", "current", "present"}]
    return 1 if bad else 0


def cmd_apply(args: argparse.Namespace) -> int:
    value = _plan(args)
    _, _, catalog = _load(args)
    selected = {key for key, item in value["components"].items() if item["selected"]}
    blocked_connections = [
        key for key, item in value["components"].items()
        if key.startswith("integration-") and item["selected"]
    ]
    if blocked_connections:
        details = "; ".join(
            f"{key}: {value['components'][key].get('detail', value['components'][key]['state'])}"
            for key in blocked_connections
        )
        raise CliError(f"连接目标不能由当前生命周期入口直接应用，已停止：{details}")
    if "codex-config" in selected:
        try:
            if "codex.api_key" not in secret_store.load():
                raise CliError("Codex 配置需要 secrets.toml 中的 codex.api_key")
        except secret_store.SecretError as error:
            raise CliError(f"Codex 秘密文件预检失败: {error}") from error
    relay = value["components"].get("paseo-relay")
    if relay and relay["selected"] and relay["state"] == "blocked":
        raise CliError(f"Paseo relay 预检失败：{relay.get('detail', '未知错误')}；未执行任何动作")
    for component_id in selected:
        if component_id in catalog["components"]:
            _ensure_dependencies(selected, component_id)
    if value["components"]["paseo-autostart"]["mode"] != "off" and "paseo-cli" not in selected:
        raise CliError("Paseo 自启动需要选择 paseo-cli")
    relay_home = args.paseo_home
    relay_result = None
    if relay and relay["action"] == "uninstall":
        try:
            relay_result = relay_owner.remove(relay_home)
        except relay_owner.RelayError as error:
            raise CliError(f"Paseo relay 撤回失败，未执行本地收敛：{error}") from error
    order = [
        "codex-cli", "trellis-cli", "pnpm", "fastctx", "codegraph-cli", "paseo-cli",
        "ponytail-plugin", "cch-status", "tmux", "tmux-config", "aoe", "tavily-hikari",
        "codex-config", "codex-agents", "pennix-skills-extra", "pennix-skills",
    ]
    results: dict[str, str] = {}
    for component_id in order:
        item = value["components"][component_id]
        if item["action"] == "native-owner":
            results[component_id] = f"requires-native-owner:{item['owner_action']}"
        elif item["selected"] and item["action"] == "install":
            results[component_id] = _run_component(args, catalog, component_id, "install")
        elif item["selected"] and item["state"] in {"match", "current"} and config_model.config_value(args.kconf, "PENNIX_UPDATE_COMPONENTS") == "y":
            owner = catalog["components"][component_id].get("actions", {}).get("upgrade")
            if owner == "managed":
                results[component_id] = _run_component(args, catalog, component_id, "upgrade")
            elif owner == "native-owner":
                results[component_id] = "requires-native-owner:upgrade"
        elif not item["selected"] and item["action"] == "uninstall":
            results[component_id] = _run_component(args, catalog, component_id, "uninstall")
    npm_selected = any(
        item["selected"]
        and isinstance(catalog["components"].get(key, {}).get("package"), dict)
        and catalog["components"][key]["package"].get("source") == "npm"
        for key, item in value["components"].items()
        if key in catalog["components"]
    )
    prefix = _expand_path(config_model.config_value(args.kconf, "PENNIX_NPM_PREFIX"))
    results["zshrc-npm-path"] = path_env.apply(prefix) if npm_selected else path_env.remove()
    mode = value["components"]["paseo-autostart"]["mode"]
    paseo = prefix / "bin" / "paseo"
    if not paseo.is_file():
        paseo = Path(shutil.which("paseo") or paseo)
    try:
        results["paseo-autostart"] = paseo_service.apply(mode, paseo, args.paseo_home)
    except paseo_service.PaseoServiceError as error:
        raise CliError(str(error)) from error
    if config_model.config_value(args.kconf, "PENNIX_SYSTEM_UPGRADE") == "y":
        result = subprocess.run(["sudo", "pacman", "-Syu", "--noconfirm"], check=False)
        if result.returncode:
            raise CliError(f"完整系统升级失败: {result.returncode}")
        results["system-upgrade"] = "changed"
    if relay and relay["selected"]:
        try:
            if relay["action"] == "install":
                relay_owner.ensure(relay_home)
            endpoint = config_model.config_value(args.kconf, "PENNIX_PASEO_RELAY_ENDPOINT")
            public_endpoint = config_model.config_value(args.kconf, "PENNIX_PASEO_RELAY_PUBLIC_ENDPOINT")
            results["paseo-relay"] = relay_owner.configure_daemon(
                paseo, relay_home, endpoint, public_endpoint,
                bool(args.kconf.syms["PENNIX_PASEO_RELAY_TLS"].tri_value),
                bool(args.kconf.syms["PENNIX_PASEO_RELAY_PUBLIC_TLS"].tri_value),
            )
        except relay_owner.RelayError as error:
            raise CliError(f"Paseo relay 应用失败：{error}") from error
    elif relay_result is not None:
        results["paseo-relay"] = relay_result
    _emit({"operation": "apply", "results": results}, args.json)
    return 0


def cmd_repair(args: argparse.Namespace) -> int:
    kconf, _, catalog = _load(args)
    args.kconf = kconf
    args.codex_home = _expand_path(config_model.config_value(kconf, "PENNIX_CODEX_HOME"))
    args.paseo_home = _expand_path(config_model.config_value(kconf, "PENNIX_PASEO_HOME"))
    if args.component not in catalog["components"]:
        raise CliError(f"未知组件: {args.component}")
    if catalog["components"][args.component].get("actions", {}).get("upgrade") != "managed":
        raise CliError(f"{args.component} upgrade 不归 pennix-lifecycle 管理")
    result = _run_component(args, catalog, args.component, "upgrade")
    _emit({"operation": "repair", "component": args.component, "status": result}, args.json)
    return 0


def _reverse_uninstall_order(catalog: dict[str, Any]) -> list[str]:
    order = [
        "pennix-skills", "pennix-skills-extra", "codex-agents", "codex-config", "tavily-hikari",
        "aoe", "tmux-config", "tmux", "cch-status", "ponytail-plugin", "paseo-cli",
        "codegraph-cli", "fastctx", "pnpm", "trellis-cli", "codex-cli",
    ]
    return [key for key in order if key in catalog["components"] and catalog["components"][key].get("actions", {}).get("uninstall") == "managed"]


def cmd_uninstall(args: argparse.Namespace) -> int:
    kconf, _, catalog = _load(args)
    args.kconf = kconf
    args.codex_home = _expand_path(config_model.config_value(kconf, "PENNIX_CODEX_HOME"))
    args.paseo_home = _expand_path(config_model.config_value(kconf, "PENNIX_PASEO_HOME"))
    if args.component == "paseo-relay":
        if args.confirm != args.component:
            raise CliError("relay 卸载必须使用 --confirm paseo-relay")
        try:
            result = relay_owner.remove(args.paseo_home)
        except relay_owner.RelayError as error:
            raise CliError(f"Paseo relay 撤回失败：{error}") from error
        _emit({"operation": "uninstall", "config_unchanged": True, "results": {"paseo-relay": result}}, args.json)
        return 0
    if args.all:
        if args.confirm_all != "ALL":
            raise CliError("全量卸载必须使用 --confirm-all ALL")
        relay = _plan(args)["components"].get("paseo-relay")
        relay_result = None
        if relay_owner.local_owned():
            try:
                relay_result = relay_owner.remove(args.paseo_home)
            except relay_owner.RelayError as error:
                raise CliError(f"relay 远端撤回预检失败，未执行任何本地卸载：{error}") from error
        elif relay and relay["selected"] and relay["state"] == "blocked":
            raise CliError(f"relay 远端撤回预检失败，未执行任何本地卸载：{relay.get('detail', '未知错误')}")
        targets = _reverse_uninstall_order(catalog)
    else:
        if not args.component or args.confirm != args.component:
            raise CliError("单组件卸载必须使用 --confirm <component-id>")
        targets = [args.component]
        if args.component not in catalog["components"]:
            raise CliError(f"未知组件: {args.component}")
        if catalog["components"][args.component].get("actions", {}).get("uninstall") != "managed":
            raise CliError(f"{args.component} uninstall 不归 pennix-lifecycle 管理")
        selected = config_model.selected_components(kconf)
        dependents = {
            "codex-cli": {"ponytail-plugin", "codex-config", "codex-agents"},
            "tmux": {"tmux-config"},
        }
        if dependents.get(args.component, set()) & selected:
            raise CliError(f"卸载 {args.component} 会破坏已选择依赖；请先修改配置并 apply")
    results: dict[str, str] = {}
    if args.all and relay_result is not None:
        results["paseo-relay"] = relay_result
    for component_id in targets:
        try:
            results[component_id] = _run_component(args, catalog, component_id, "uninstall")
        except CliError:
            if args.all:
                raise
            raise
    _emit({"operation": "uninstall", "config_unchanged": True, "results": results}, args.json)
    return 0


def cmd_secrets_edit(args: argparse.Namespace) -> int:
    path = _secrets_path(args.secrets_file)
    template = (ROOT / "templates" / "secrets.example.toml").read_text(encoding="utf-8")
    secret_store.ensure(path, template)
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
    result = subprocess.run([*shlex.split(editor), str(path)], check=False)
    if result.returncode:
        raise CliError(f"编辑器退出码: {result.returncode}")
    print(path)
    return 0


def _secrets_path(value: str | None) -> Path:
    path = _expand_path(value) if value else secret_store.default_path()
    if not path.is_absolute():
        raise CliError("secrets.toml 路径必须是绝对路径")
    return path


def cmd_secrets_set(args: argparse.Namespace) -> int:
    value = input(f"{args.field}: ")
    try:
        changed = secret_store.update(_secrets_path(args.secrets_file), {args.field: value})
    except secret_store.SecretError as error:
        raise CliError(str(error)) from error
    print("changed" if changed else "no-op")
    return 0


def cmd_secrets_get(args: argparse.Namespace) -> int:
    try:
        print(secret_store.get(args.field, _secrets_path(args.secrets_file)))
    except secret_store.SecretError as error:
        raise CliError(str(error)) from error
    return 0


def cmd_secrets_check(args: argparse.Namespace) -> int:
    try:
        values = secret_store.load(_secrets_path(args.secrets_file))
    except secret_store.SecretError as error:
        raise CliError(str(error)) from error
    _emit({"configured": sorted(values)}, args.json)
    return 0


def cmd_secrets_import_ssh(args: argparse.Namespace) -> int:
    try:
        changed = credential_import.import_fields(
            args.alias, args.fields, _secrets_path(args.secrets_file)
        )
    except credential_import.CredentialImportError as error:
        raise CliError(str(error)) from error
    _emit({"changed": sorted(changed), "source": args.alias}, args.json)
    return 0


def cmd_cache(args: argparse.Namespace) -> int:
    cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")).expanduser() / "pennix-lifecycle"
    if args.cache_command == "list":
        _emit({"path": str(cache), "entries": sorted(item.name for item in cache.iterdir()) if cache.is_dir() else []}, args.json)
        return 0
    if args.cache_command == "clean":
        if cache.is_dir():
            for item in cache.iterdir():
                if item.is_dir() and not item.is_symlink():
                    shutil.rmtree(item)
                else:
                    item.unlink(missing_ok=True)
        print(cache)
        return 0
    raise CliError("未知 cache 操作")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pennix", description="Pennix 工作流生命周期工具")
    sub = p.add_subparsers(dest="command", required=True)

    config = sub.add_parser("config", help="管理 workflow.config")
    config_sub = config.add_subparsers(dest="config_command", required=True)
    for name, function in (("init", cmd_config_init), ("edit", cmd_config_edit), ("check", cmd_config_check)):
        child = config_sub.add_parser(name)
        child.add_argument("--config")
        child.add_argument("--json", action="store_true")
        if name == "init":
            child.add_argument("--force", action="store_true")
            child.add_argument("--defconfig")
        child.set_defaults(function=function)

    for name, function in (("plan", cmd_plan), ("status", cmd_status), ("check", cmd_check), ("apply", cmd_apply)):
        child = sub.add_parser(name)
        child.add_argument("--config")
        child.add_argument("--json", action="store_true")
        child.set_defaults(function=function)

    repair = sub.add_parser("repair")
    repair.add_argument("component")
    repair.add_argument("--config")
    repair.add_argument("--json", action="store_true")
    repair.set_defaults(function=cmd_repair)

    uninstall = sub.add_parser("uninstall")
    uninstall.add_argument("component", nargs="?")
    uninstall.add_argument("--all", action="store_true")
    uninstall.add_argument("--confirm")
    uninstall.add_argument("--confirm-all")
    uninstall.add_argument("--config")
    uninstall.add_argument("--json", action="store_true")
    uninstall.set_defaults(function=cmd_uninstall)

    secrets = sub.add_parser("secrets", help="管理私有明文 secrets.toml")
    secrets_sub = secrets.add_subparsers(dest="secrets_command", required=True)
    secret_edit = secrets_sub.add_parser("edit")
    secret_edit.add_argument("--secrets-file")
    secret_edit.set_defaults(function=cmd_secrets_edit)
    secret_set = secrets_sub.add_parser("set")
    secret_set.add_argument("field", choices=sorted(secret_store.FIELDS))
    secret_set.add_argument("--secrets-file")
    secret_set.set_defaults(function=cmd_secrets_set)
    secret_get = secrets_sub.add_parser("get")
    secret_get.add_argument("field", choices=sorted(secret_store.FIELDS))
    secret_get.add_argument("--secrets-file")
    secret_get.set_defaults(function=cmd_secrets_get)
    secret_check = secrets_sub.add_parser("check")
    secret_check.add_argument("--secrets-file")
    secret_check.add_argument("--json", action="store_true")
    secret_check.set_defaults(function=cmd_secrets_check)
    secret_import = secrets_sub.add_parser("import-ssh")
    secret_import.add_argument("alias")
    secret_import.add_argument("fields", nargs="+", choices=sorted(credential_import.SOURCES))
    secret_import.add_argument("--secrets-file")
    secret_import.add_argument("--json", action="store_true")
    secret_import.set_defaults(function=cmd_secrets_import_ssh)

    cache = sub.add_parser("cache", help="管理工具自有缓存")
    cache_sub = cache.add_subparsers(dest="cache_command", required=True)
    for name in ("list", "clean"):
        child = cache_sub.add_parser(name)
        child.add_argument("--json", action="store_true")
        child.set_defaults(function=cmd_cache)
    return p


def main(argv: list[str] | None = None) -> int:
    try:
        args = parser().parse_args(argv)
        return args.function(args)
    except (CliError, config_model.ConfigError, codex_config.CodexConfigError, path_env.PathEnvError, paseo_service.PaseoServiceError, relay_owner.RelayError, secret_store.SecretError) as error:
        print(f"pennix: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
