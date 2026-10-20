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
import tempfile
from pathlib import Path
from typing import Any
import tomllib

import _engine as owner_engine
import apply_journal
import codex_config
import component_lock
import candidates
import connection_owners
import config_model
import credential_import
import path_env
import paseo_service
import relay_owner
import release_owner
import skill_collections
import owned_files
import secret_store
import native_integrations


ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "references" / "catalog.json"
APPLY_ORDER = [
    "codex-cli", "trellis-cli", "pnpm", "context-mode", "codegraph-cli", "paseo-cli",
    "ponytail-plugin", "cch-status", "tmux", "tmux-config", "aoe", "uv", "tavily-cli",
    "tavily-hikari", "peon-ping", "codex-config", "codex-agents", "pennix-skills-extra",
    "pennix-skills",
]


class CliError(RuntimeError):
    """A user-facing lifecycle error."""


def _args_for_engine(args: argparse.Namespace, command: str) -> argparse.Namespace:
    args.command = command
    args.codex_home = Path(os.path.expanduser(args.codex_home))
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
    for key, component in catalog["components"].items():
        component["id"] = key
    try:
        candidates.validate_catalog(catalog)
    except candidates.CandidateError as error:
        raise CliError(f"候选解析器目录不完整: {error}") from error
    sources = {key: ((value["lock_source"], *value["lock_source_aliases"])
                     if value.get("lock_source_aliases") else value["lock_source"])
               for key, value in catalog["components"].items()
               if value.get("delivery") in {"package", "plugin", "client", "collection"}}
    try:
        lock_path = component_lock.path_for(path)
        args.component_lock = component_lock.read(lock_path, sources)
        args.component_lock_path = lock_path
        args.component_lock_sources = sources
    except component_lock.LockError as error:
        raise CliError(str(error)) from error
    return kconf, path, catalog


def _component_status(args: argparse.Namespace, component_id: str, component: dict[str, Any]) -> dict[str, Any]:
    if component_id == "codex-config":
        values = config_model.values(args.kconf)
        status = codex_config.inspect(args.codex_home / "config.toml", values)
        if (component_id in config_model.selected_components(args.kconf)
                and values.get("PENNIX_CODEX_AUTH_API_KEY") == "y"):
            path = Path(config_model.config_value(args.kconf, "PENNIX_SECRETS_FILE"))
            try:
                secret_store.get(path, "codex", "api_key")
            except secret_store.SecretError:
                status.update(state="blocked", detail="Codex API Key 缺失或秘密文件不安全")
        return status
    native = component.get("native_config")
    if native:
        try:
            # Inspect the real native state even when the Kconfig selection is off;
            # disabling a component must not orphan an external or drifted MCP entry.
            return native_integrations.inspect(native["owner"], args.codex_home, enabled=True)
        except (native_integrations.NativeIntegrationError, native_integrations.hook_integrations.HookError) as error:
            return {"state": "blocked", "owned": False, "detail": str(error)}
    try:
        state, detail = owner_engine.probe_component(
            component, args.codex_home, home_directory=Path.home()
        )
    except Exception as error:
        return {"state": "blocked", "detail": str(error)}
    result = {"state": state, "detail": detail}
    if component.get("delivery") == "client" and state in {"external", "drifted"}:
        result["review_sha256"] = owner_engine.local_clients.fingerprint(component)
    if component.get("delivery") == "package" and isinstance(component.get("package"), dict):
        try:
            result["ownership"] = "lifecycle" if owner_engine.package_owned(
                component_id, component, detail if state in {"match", "outdated"} else None, args.codex_home
            ) else "external"
        except owner_engine.BootstrapError as error:
            result.update(state="blocked", detail=str(error), ownership="unknown")
    return result


def _relay_plan(args: argparse.Namespace, kconf: config_model.StrictKconfig) -> dict[str, Any] | None:
    paseo_home = Path(config_model.config_value(kconf, "PENNIX_PASEO_HOME"))
    selected = config_model.config_value(kconf, "PENNIX_PASEO_RELAY") == "y"
    register = config_model.config_value(kconf, "PENNIX_PASEO_RELAY_REGISTER") == "y"
    nas_ssh = config_model.config_value(kconf, "PENNIX_RELAY_NAS_SSH")
    router_ssh = config_model.config_value(kconf, "PENNIX_RELAY_ROUTER_SSH")
    if not selected:
        if not relay_owner.local_owned():
            return None
        return {
            "selected": False,
            "action": "uninstall",
            "state": "owned",
            "register": True,
            "detail": "本工具登记项待撤回",
        }
    endpoint = config_model.config_value(kconf, "PENNIX_PASEO_RELAY_ENDPOINT")
    public_endpoint = config_model.config_value(kconf, "PENNIX_PASEO_RELAY_PUBLIC_ENDPOINT")
    try:
        relay_owner.validate_config(endpoint, public_endpoint)
        if not register:
            paseo = Path(shutil.which("paseo") or Path.home() / ".local/bin/paseo")
            state = relay_owner.inspect_daemon(
                paseo, paseo_home, endpoint, public_endpoint,
                config_model.config_value(kconf, "PENNIX_PASEO_RELAY_TLS") == "y",
                config_model.config_value(kconf, "PENNIX_PASEO_RELAY_PUBLIC_TLS") == "y",
            )
            return {
                "selected": True,
                "register": False,
                "action": "keep" if state == "current" else "configure",
                "state": state,
                "detail": "本地 daemon relay 已核验；不访问远端登记表" if state == "current"
                else "仅配置本地 daemon relay，不访问远端登记表",
            }
        state = relay_owner.inspect(paseo_home, nas_ssh=nas_ssh, router_ssh=router_ssh)
        return {
            "selected": True,
            "register": True,
            "action": "keep" if state == "present" else "install",
            "state": state,
            "detail": "既有登记项" if state == "present" else "等待登记",
        }
    except relay_owner.RelayError as error:
        return {"selected": True, "action": "blocked", "state": "blocked", "detail": str(error)}


def _plan(args: argparse.Namespace) -> dict[str, Any]:
    kconf, path, catalog = _load(args)
    args.kconf = kconf
    args.codex_home = Path(os.path.expanduser(config_model.config_value(kconf, "PENNIX_CODEX_HOME")))
    selected = config_model.selected_components(kconf)
    components: dict[str, Any] = {}
    for component_id, component in catalog["components"].items():
        if component.get("delivery") == "state":
            components[component_id] = {"selected": False, "action": "ignored", "state": "runtime-state"}
            continue
        lock_target = args.component_lock["components"].get(component_id)
        lock_migration = None
        plugin_adoption = None
        if component_id in selected and lock_target is None:
            try:
                if component.get("delivery") == "package":
                    lock_migration = owner_engine.package_lock_migration(component_id, component)
                elif component.get("delivery") == "client":
                    lock_migration = owner_engine.local_clients.lock_migration(component_id, component)
                elif component.get("delivery") == "collection":
                    lock_migration = skill_collections.lock_migration(component, args.codex_home)
                elif component.get("delivery") == "plugin":
                    migrated = owner_engine.plugin_lock_migration(component_id, component, args.codex_home)
                    if migrated:
                        if owner_engine._plugin_receipt(component_id, component) is None:
                            plugin_adoption = migrated
                        else:
                            lock_migration = migrated
                        lock_target = migrated
            except (owner_engine.BootstrapError, owner_engine.local_clients.ClientError,
                    skill_collections.CollectionError, OSError):
                lock_migration = None
                plugin_adoption = None
            if lock_migration:
                lock_target = lock_migration
        if lock_target:
            component["_lock_target"] = lock_target
        status = _component_status(args, component_id, component)
        if component.get("native_config"):
            try:
                package_state, package_observed = owner_engine.probe_component(component, args.codex_home)
                package_owned = owner_engine.package_owned(component_id, component, package_observed, args.codex_home)
            except owner_engine.BootstrapError as error:
                package_state, package_owned = "blocked", False
                status["detail"] = str(error)
            status["package_state"] = package_state
            status["package_ownership"] = "lifecycle" if package_owned else "external"
            if component_id in selected:
                if package_state == "missing":
                    action = "install"
                    if (component.get("package", {}).get("source") in {"official", "aur"}
                            and config_model.config_value(kconf, "PENNIX_SYSTEM_UPGRADE") != "y"):
                        action = "blocked"
                        status["detail"] = "Arch 包缺失时必须由同次确认的完整系统升级提供安全安装边界"
                elif package_state in {"match", "external"} and not package_owned:
                    action = "adopt" if config_model.config_value(kconf, "PENNIX_ADOPT_EXISTING_PACKAGES") == "y" else "blocked"
                elif package_state in {"upgrade-required", "drifted", "blocked"}:
                    action = "repair"
                else:
                    action = "keep"
                if package_state == "outdated":
                    if (component.get("package", {}).get("source") in {"official", "aur"}
                            and package_owned
                            and config_model.config_value(kconf, "PENNIX_SYSTEM_UPGRADE") == "y"):
                        action = "system-refresh"
                    else:
                        action = "blocked"
                native_enabled = config_model.config_value(kconf, component["native_config"]["select"]) == "y"
                native_action = ("unconfigure" if status.get("owned") else "keep") if not native_enabled else "keep" if status["state"] == "current" else (
                    "repair" if status["state"] == "drifted" else "configure"
                )
                if native_enabled and status["state"] in {"blocked", "external"}:
                    if (status["state"] == "external" and config_model.config_value(kconf, "PENNIX_ADOPT_EXISTING_PACKAGES") == "y"
                            and native_integrations.can_adopt(component["native_config"]["owner"], args.codex_home)):
                        native_action = "configure"
                    else:
                        action = "blocked"
                        status["detail"] = status.get("detail", "native owner 无法安全接管")
                        native_action = "blocked"
                status["native_action"] = native_action
            else:
                if status["state"] == "external":
                    action = "keep"
                    status["native_action"] = "keep"
                elif status["state"] in {"drifted", "blocked"}:
                    action = "blocked"
                    status["native_action"] = "blocked"
                else:
                    action = "uninstall" if package_state == "match" and package_owned else "keep"
                    status["native_action"] = "unconfigure" if status.get("owned") else "keep"
        elif component.get("delivery") == "native":
            action = "keep" if not (component_id in selected) or status["state"] in {"match", "current", "present"} else "blocked"
            if action == "blocked":
                status["detail"] = component.get("native_owner_reason", "需要由组件原生 owner 完成安装和配置")
        elif component_id in selected:
            if component.get("delivery") == "client" and status["state"] == "external":
                status["state"] = "adoption-required"
            if (component.get("delivery") == "package" and status["state"] == "match"
                    and status.get("ownership") != "lifecycle"):
                action = "adopt" if config_model.config_value(kconf, "PENNIX_ADOPT_EXISTING_PACKAGES") == "y" else "blocked"
                if action == "blocked":
                    status["detail"] = "已安装但非生命周期归属；需显式开启接管"
            else:
                action = "keep" if status["state"] in {"match", "current", "present"} else "install"
            if status["state"] == "upgrade-required":
                action = "blocked"
            if status["state"] in {"drifted", "blocked"}:
                action = "repair"
            if status["state"] == "adoption-required":
                action = "adopt" if config_model.config_value(kconf, "PENNIX_ADOPT_EXISTING_PACKAGES") == "y" else "blocked"
            if status["state"] == "outdated":
                if (component.get("package", {}).get("source") in {"official", "aur"}
                        and status.get("ownership") == "lifecycle"
                        and config_model.config_value(kconf, "PENNIX_SYSTEM_UPGRADE") == "y"):
                    action = "system-refresh"
                else:
                    action = "blocked"
        else:
            if component_id == "codex-config":
                try:
                    owned = codex_config.is_owned(args.codex_home / "config.toml")
                except codex_config.CodexConfigError as error:
                    status.update(state="blocked", detail=str(error))
                    owned = False
                action = "uninstall" if owned else "keep"
                if owned:
                    status["state"] = "present"
            else:
                if component.get("delivery") == "package":
                    action = "uninstall" if (
                        status["state"] == "match" and status.get("ownership") == "lifecycle"
                    ) else "keep"
                    if action == "keep" and status["state"] == "match":
                        status["ownership"] = "external"
                elif component.get("delivery") == "collection":
                    ownership = skill_collections.receipt_owned(component, args.codex_home)
                    if ownership == "owned":
                        action = "uninstall"
                        status.update(state="owned-legacy", ownership="lifecycle",
                                      detail="当前 catalog source 已变化；仍由本 Lifecycle receipt 归属")
                    elif ownership.startswith("blocked:"):
                        action = "blocked"
                        status.update(detail=ownership, ownership="unknown")
                    else:
                        action = "keep"
                else:
                    action = "uninstall" if status["state"] in {"match", "current", "present"} else "keep"
            if action == "uninstall" and component.get("delivery") == "collection" and not owner_engine.collection_owned(component, args.codex_home):
                if status.get("ownership") != "lifecycle":
                    action = "keep"
                    status["ownership"] = "external"
        if lock_migration:
            status["lock_migration"] = lock_migration
        if plugin_adoption:
            status.update(state="adoption-required", action="adopt-ponytail-plugin",
                          plugin_adoption=plugin_adoption,
                          detail="精确 Marketplace 来源、版本和干净完整提交匹配；等待专用接管确认")
        installed_state = package_state if component.get("native_config") else status["state"]
        if (component_id in selected and component.get("delivery") in {"package", "plugin", "client", "collection"}
                and lock_target is None and installed_state != "missing"):
            status.update(state="lock-migration-required", detail="已有安装必须先由可信 owner receipt 初始化 components.lock.json")
            action = "blocked"
        components[component_id] = {"selected": component_id in selected, "action": action, **status}
    prefix = Path.home() / ".local"
    paseo = prefix / "bin" / "paseo"
    if not paseo.is_file():
        paseo = Path(shutil.which("paseo") or paseo)
    mode = config_model.choice_value(kconf, "PENNIX_PASEO_AUTOSTART_")
    run = config_model.config_value(kconf, "PENNIX_PASEO_RUN") == "y"
    listen = config_model.config_value(kconf, "PENNIX_PASEO_LISTEN")
    paseo_home = Path(config_model.config_value(kconf, "PENNIX_PASEO_HOME"))
    service_state = paseo_service.inspect(paseo, paseo_home)
    runtime = paseo_service.runtime(paseo, paseo_home, mode, run)
    if mode != "off" and not run:
        service_action = "blocked"
        service_detail = "Paseo 自启动需要同时启用保持运行"
    elif service_state in {"blocked", "drifted", "external"}:
        service_action = "blocked"
        service_detail = f"Paseo unit 状态为 {service_state}，需要显式处理归属或漂移"
    elif mode == "off" and run and service_state == "absent":
        service_action = "start"
        service_detail = "等待启动 daemon"
    else:
        service_action = "keep" if (mode == "off" and service_state == "absent") or (mode != "off" and service_state == "current") else (
            "uninstall" if mode == "off" else "install"
        )
        service_detail = None
    if service_action == "keep" and runtime["state"] != "current":
        service_action = "start"
        service_detail = "daemon、unit enable/active 或 linger 尚未达到期望"
    components["paseo-autostart"] = {
        "selected": mode != "off" or run,
        "mode": mode,
        "run": run,
        "listen": listen,
        "action": service_action,
        "state": ("current" if service_action == "keep" and runtime["state"] == "current" else service_state),
        "unit_state": service_state,
        "runtime": runtime,
        **({"detail": service_detail} if service_detail else {}),
    }
    relay = _relay_plan(args, kconf)
    if relay is not None:
        components["paseo-relay"] = relay
    try:
        connections = connection_owners.plan(
            kconf,
            catalog,
            args.codex_home,
            Path(config_model.config_value(kconf, "PENNIX_SECRETS_FILE")),
            installing={key for key, item in components.items() if item["selected"] and item["action"] in {"install", "upgrade", "adopt-upgrade"}},
        )
    except connection_owners.ConnectionError as error:
        raise CliError(str(error)) from error
    arch_components = [key for key, item in components.items()
                       if item.get("selected") and catalog["components"].get(key, {}).get("package", {}).get("source") in {"official", "aur"}]
    return {"config": str(path), "codex_home": str(args.codex_home), "components": components, "connections": connections,
            "functional_checks_requested": config_model.config_value(kconf, "PENNIX_FUNCTIONAL_CHECKS") == "y",
            "side_effects": {
                "path": {"path": str(Path.home() / ".zshrc"), "prefix": str(Path.home() / ".local"), "action": "apply"},
                "system_upgrade": config_model.config_value(kconf, "PENNIX_SYSTEM_UPGRADE") == "y",
                "system_upgrade_command": "sudo pacman -Syu --noconfirm",
                "system_upgrade_components": arch_components,
            }}


def _emit(value: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))
        return
    if isinstance(value, dict) and "components" in value:
        print(f"配置: {value['config']}")
        print(f"Codex: {value['codex_home']}")
        for key, item in value["components"].items():
            print(f"{key}: {item['action']} ({item['state']})")
        for key, item in value.get("connections", {}).items():
            print(f"connection:{key}: {item['action']} ({item['state']})")
        side_effects = value.get("side_effects", {})
        if side_effects.get("path"):
            print(f"PATH: {side_effects['path']['action']} {side_effects['path']['path']} <- {side_effects['path']['prefix']}/bin")
        if side_effects.get("system_upgrade"):
            print(f"system-upgrade: confirm {side_effects['system_upgrade_command']} (system-wide; lifecycle Arch components: {', '.join(side_effects['system_upgrade_components']) or 'none'})")
        return
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _ensure_dependencies(selected: set[str], component_id: str) -> None:
    dependencies = {
        "tmux-config": {"tmux"},
        "ponytail-plugin": {"codex-cli"},
        "codex-config": {"codex-cli"},
        "codex-agents": {"codex-cli"},
        "tavily-hikari": {"tavily-cli", "pennix-skills"},
        "tavily-cli": {"uv"},
    }
    missing = dependencies.get(component_id, set()) - selected
    if missing:
        raise CliError(f"{component_id} 依赖未选择: {', '.join(sorted(missing))}")


def _ensure_connection_dependencies(selected: set[str], connections: dict[str, Any], catalog: dict[str, Any]) -> None:
    for target, item in connections.items():
        if item["selected"]:
            component = catalog["configuration_targets"][target]["component"]
            if component not in selected:
                raise CliError(f"连接 {target} 依赖未选择: {component}")


def _run_component(args: argparse.Namespace, catalog: dict[str, Any], component_id: str, operation: str, *, repair: bool = False,
                   candidate: dict[str, Any] | None = None) -> str:
    component = catalog["components"][component_id]
    native = component.get("native_config")
    if native and operation in {"configure", "unconfigure"}:
        if operation == "unconfigure":
            return native_integrations.unconfigure(native["owner"], args.codex_home)
        return native_integrations.apply(native["owner"], args.codex_home,
            adopt=config_model.config_value(args.kconf, "PENNIX_ADOPT_EXISTING_PACKAGES") == "y",
            repair=repair)
    if native and operation == "uninstall":
        native_integrations.unconfigure(native["owner"], args.codex_home)
    if component_id == "codex-config":
        if operation == "uninstall":
            return codex_config.uninstall(args.codex_home / "config.toml")
        values = config_model.values(args.kconf)
        return codex_config.apply(args.codex_home / "config.toml", values)
    engine_args = _args_for_engine(args, operation)
    if candidate:
        engine_args.candidate = candidate
        engine_args.explicit_update = operation == "upgrade"
    try:
        if component.get("delivery") in {"static", "collection"}:
            return owner_engine.static_operation(engine_args, component_id, component, operation, catalog)
        return owner_engine.component_operation(engine_args, catalog, component_id, component, operation)
    except Exception as error:
        raise CliError(f"{component_id} {operation} 失败: {error}") from error


def cmd_config_init(args: argparse.Namespace) -> int:
    path = config_model.init(args.config, force=args.force, defconfig=args.defconfig)
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
    bad = []
    for item in value["components"].values():
        if not item["selected"]:
            if item.get("action") != "uninstall":
                continue
            bad.append(item)
            continue
        if item.get("native_action") is not None:
            if (item["action"] != "keep" or item.get("package_state") != "match"
                    or item.get("package_ownership") != "lifecycle" or item["native_action"] != "keep"):
                bad.append(item)
        elif item["action"] != "keep" or item["state"] not in {"match", "current", "present"}:
            bad.append(item)
    bad.extend(item for item in value["connections"].values() if item["selected"] and (
        item["action"] != "keep" or item["state"] != "current"))
    service = value["components"].get("paseo-autostart")
    if service and service["action"] == "blocked":
        bad.append(service)
    if value.get("functional_checks_requested"):
        _, _, catalog = _load(args)
        probes = connection_owners.functional_checks(catalog, value["connections"], args.codex_home)
        value["functional_checks"] = probes
        bad.extend(probe for probe in probes.values() if probe["state"] != "passed")
    else:
        value["functional_checks"] = {"state": "not_requested"}
    _emit(value, args.json)
    return 1 if bad else 0


def _apply_steps(value: dict[str, Any], args: argparse.Namespace, catalog: dict[str, Any]) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    side_effects = value.get("side_effects", {})
    if side_effects.get("system_upgrade"):
        steps.append({"id": "system-upgrade", "component": "system", "action": "upgrade", "target": {}})
    if any(value["components"].get(key, {}).get("action") in {"install", "adopt", "upgrade", "adopt-upgrade"}
           and value["components"].get(key, {}).get("selected")
           and catalog["components"].get(key, {}).get("package", {}).get("source") == "aur"
           for key in APPLY_ORDER):
        steps.append({"id": "aur-helper", "component": "aur-helper", "action": "prepare", "target": {}})
    for key, item in value["components"].items():
        if item.get("selected") and item.get("lock_migration"):
            steps.append({"id": f"{key}:lock-migration", "component": key,
                          "action": "lock-migration", "target": item["lock_migration"]})
    for key in APPLY_ORDER:
        item = value["components"].get(key)
        if not item:
            continue
        action = item.get("action")
        if item.get("selected") and action in {"install", "adopt", "upgrade", "adopt-upgrade"}:
            steps.append({"id": f"{key}:{action}", "component": key, "action": action,
                          "target": args.component_lock["components"].get(key, {})})
        if item.get("selected") and item.get("plugin_adoption"):
            steps.append({"id": f"{key}:adopt-plugin", "component": key,
                          "action": "adopt-plugin", "target": item["plugin_adoption"]})
    for key in APPLY_ORDER:
        item = value["components"].get(key)
        if item and item.get("selected") and item.get("native_action") == "configure":
            steps.append({"id": f"{key}:configure", "component": key, "action": "configure", "target": {}})
    for target, item in value.get("connections", {}).items():
        if item.get("action") in {"configure", "unconfigure"}:
            steps.append({"id": f"connection:{target}:{item['action']}", "component": f"connection:{target}",
                          "action": item["action"], "target": {}})
    for key in APPLY_ORDER:
        item = value["components"].get(key)
        if item and item.get("native_action") == "unconfigure":
            steps.append({"id": f"{key}:unconfigure", "component": key, "action": "unconfigure", "target": {}})
    for key in reversed(APPLY_ORDER):
        item = value["components"].get(key)
        if item and not item.get("selected") and item.get("action") == "uninstall":
            steps.append({"id": f"{key}:uninstall", "component": key, "action": "uninstall", "target":
                          args.component_lock["components"].get(key, {})})
    relay = value["components"].get("paseo-relay")
    if relay and relay.get("action") in {"install", "uninstall"}:
        steps.append({"id": f"paseo-relay:{relay['action']}", "component": "paseo-relay",
                      "action": relay["action"], "target": {}})
    steps.append({"id": "path-env:apply", "component": "path-env", "action": "apply", "target": {}})
    steps.append({"id": f"paseo-autostart:{value['components']['paseo-autostart']['action']}",
                  "component": "paseo-autostart", "action": "service", "target": {}})
    return steps


def _journal_for_apply(args: argparse.Namespace, value: dict[str, Any], steps: list[dict[str, Any]], *, create: bool = True) -> tuple[dict | None, bool]:
    try:
        active = apply_journal.read()
    except apply_journal.JournalError as error:
        raise CliError(f"未完成 lifecycle journal 无法读取，保留现场：{error}") from error
    config_digest = apply_journal.digest(Path(value["config"]))
    lock_digest = apply_journal.digest(args.component_lock_path)
    if config_digest is None:
        raise CliError("workflow.config 在 apply 前消失")
    if active is not None:
        if active["command"] != "apply" or active["component"] is not None:
            raise CliError("存在未完成的单组件更新 journal；请使用相同的 update 入口恢复")
        if active["config_digest"] != config_digest:
            raise CliError("workflow.config 已改变；未完成 apply 保留现场，不能猜测续跑")
        if apply_journal.digest(args.component_lock_path) != active["lock_digest"]:
            expected = [step for step in active["steps"] if step["status"] == "verified" and step["target"]]
            if not expected and not any(step["id"] == "system-upgrade" and step["status"] == "verified"
                                        for step in active["steps"]):
                raise CliError("components.lock.json 在未完成 apply 期间发生变化；保留现场，不能猜测续跑")
        if apply_journal.complete(active):
            apply_journal.clear()
            active = None
        else:
            return active, True
    if not create:
        return None, False
    return apply_journal.start("apply", None, config_digest, lock_digest, steps), False


def _journal_mutate(value: dict, step_id: str, action):
    step = apply_journal.step(value, step_id)
    if step["status"] == "verified":
        return value, "resumed"
    if step["status"] in {"started", "unknown"}:
        raise CliError(f"步骤 {step_id} 的副作用状态未知；保留 journal，需人工核验")
    value = apply_journal.started(value, step_id)
    try:
        result = action()
    except BaseException:
        try:
            apply_journal.unknown(value, step_id)
        except BaseException:
            pass
        raise
    return apply_journal.verified(value, step_id), result


def _retry_unknown_update(active: dict, step_id: str, state: str) -> dict:
    if apply_journal.step(active, step_id)["status"] != "unknown":
        return active
    if state != "match":
        raise CliError("update owner 未保持旧锁定目标；保留 journal，需人工核验")
    return apply_journal.retry(active, step_id)


def _refresh_arch_receipts(args: argparse.Namespace, catalog: dict[str, Any]) -> None:
    updates = []
    selected = config_model.selected_components(args.kconf)
    for component_id in selected:
        component = catalog["components"].get(component_id)
        package = component.get("package") if component else None
        if not isinstance(package, dict) or package.get("source") not in {"official", "aur"}:
            continue
        if owner_engine._package_receipt(component_id, component) is None:
            continue
        identity = owner_engine.package_identity(component)
        updates.append((component_id, component, identity["version"]))
    for component_id, component, version in updates:
        owner_engine._write_package_receipt(component_id, component, version)
        args.component_lock = component_lock.set_entry(
            args.component_lock, component_id,
            {"source": component["lock_source"], "version": version},
            args.component_lock_sources,
        )
    if updates:
        component_lock.write(args.component_lock_path, args.component_lock, args.component_lock_sources)


def cmd_apply(args: argparse.Namespace) -> int:
    value = _plan(args)
    _, _, catalog = _load(args)
    selected = {key for key, item in value["components"].items() if item["selected"]}
    repair = [key for key, item in value["components"].items()
              if item["selected"] and (item["action"] == "repair" or item.get("native_action") == "repair")]
    if repair:
        raise CliError(f"先显式 repair 漂移组件，未执行任何动作：{', '.join(sorted(repair))}")
    relay = value["components"].get("paseo-relay")
    if relay and relay["selected"] and relay["state"] == "blocked":
        raise CliError(f"Paseo relay 预检失败：{relay.get('detail', '未知错误')}；未执行任何动作")
    for component_id in selected:
        if component_id in catalog["components"]:
            _ensure_dependencies(selected, component_id)
    _ensure_connection_dependencies(selected, value["connections"], catalog)
    blocked_connections = [
        target for target, item in value["connections"].items()
        if item["selected"] and item["action"] == "blocked"
    ]
    if blocked_connections:
        details = "; ".join(
            f"{target}: {value['connections'][target].get('detail', 'owner 预检失败')}"
            for target in sorted(blocked_connections)
        )
        raise CliError(f"连接预检失败，未执行任何动作：{details}")
    service = value["components"]["paseo-autostart"]
    if service["action"] == "blocked":
        raise CliError(f"Paseo 服务预检失败，未执行任何动作：{service.get('detail', '配置不一致')}")
    blocked = [key for key, item in value["components"].items()
               if item.get("action") == "blocked" and (key in selected or item.get("native_action") == "blocked")]
    if blocked:
        details = "; ".join(f"{key}: {value['components'][key].get('detail', 'owner 预检失败')}" for key in sorted(blocked))
        raise CliError(f"组件预检失败，未执行任何动作：{details}")
    if ("codex-config" in selected and args.kconf.syms["PENNIX_CODEX_AUTH_API_KEY"].tri_value):
        if args.kconf.syms["PENNIX_CODEX_PROVIDER_CUSTOM"].tri_value == 0:
            raise CliError("Codex API Key 认证需要选择自定义 provider")
        try:
            secret_store.get(Path(config_model.config_value(args.kconf, "PENNIX_SECRETS_FILE")), "codex", "api_key")
        except secret_store.SecretError as error:
            raise CliError(f"Codex API Key 预检失败：{error}") from error
    if value["components"]["paseo-autostart"]["mode"] != "off" and "paseo-cli" not in selected:
        raise CliError("Paseo 自启动需要选择 paseo-cli")
    steps = _apply_steps(value, args, catalog)
    active, resumed = _journal_for_apply(args, value, steps, create=False)
    plugin_adoptions = {key: item["plugin_adoption"] for key, item in value["components"].items()
                        if item.get("selected") and item.get("plugin_adoption")}
    if active is None and value.get("side_effects", {}).get("system_upgrade"):
        _emit(value, args.json)
        if (not sys.stdin.isatty() or not sys.stdout.isatty()
                or input("确认完整系统升级及本次 apply？输入 yes 继续: ").strip() != "yes"):
            return 1
    if active is None and plugin_adoptions:
        _emit(value, args.json)
        for key, target in plugin_adoptions.items():
            print(f"将仅为现有 Codex plugin {key} 写入 Lifecycle receipt 和 components.lock.json；不会重装插件。精确身份: {target}")
        if (not sys.stdin.isatty() or not sys.stdout.isatty()
                or input("确认迁移 Ponytail plugin 所有权？输入 yes 继续: ").strip() != "yes"):
            return 1
    if active is None:
        active, _ = _journal_for_apply(args, value, steps, create=True)
    relay_home = Path(config_model.config_value(args.kconf, "PENNIX_PASEO_HOME"))
    results: dict[str, str] = {}
    system_step = next((item for item in active["steps"] if item["id"] == "system-upgrade"), None)
    if system_step is not None and system_step["status"] == "started":
        raise CliError("pacman -Syu 的副作用状态未知；保留 journal，不能自动重试")
    if system_step is not None and system_step["status"] != "verified":
        def system_upgrade():
            result = subprocess.run(["sudo", "pacman", "-Syu", "--noconfirm"], check=False)
            if result.returncode:
                raise CliError(f"完整系统升级失败: {result.returncode}")
            _refresh_arch_receipts(args, catalog)
            return "changed"
        active, results["system-upgrade"] = _journal_mutate(active, "system-upgrade", system_upgrade)
    if system_step is not None:
        value = _plan(args)
        _, _, catalog = _load(args)
        relay = value["components"].get("paseo-relay")
    for step in active["steps"]:
        step_id, component_id, action = step["id"], step["component"], step["action"]
        if step["status"] == "verified" or step_id == "system-upgrade":
            continue
        if action == "prepare" and component_id == "aur-helper":
            active, results["aur-helper"] = _journal_mutate(
                active, step_id, lambda: owner_engine.host.ensure_aur_helper(catalog["aur_helper"]))
            continue
        if action == "lock-migration":
            def migrate(step=step, component_id=component_id):
                args.component_lock = component_lock.set_entry(args.component_lock, component_id, step["target"], args.component_lock_sources)
                catalog["components"][component_id]["_lock_target"] = step["target"]
                component_lock.write(args.component_lock_path, args.component_lock, args.component_lock_sources)
                return "migrated"
            active, results[component_id] = _journal_mutate(active, step_id, migrate)
            continue
        if action in {"install", "adopt", "upgrade", "adopt-upgrade"}:
            component = catalog["components"].get(component_id)
            if component is None:
                raise CliError(f"journal 引用未知组件: {component_id}")
            operation = "upgrade" if action in {"upgrade", "adopt-upgrade"} else "install"
            target = step["target"] or args.component_lock["components"].get(component_id)
            component["_lock_target"] = target if target else component.get("_lock_target")
            if component.get("delivery") in {"package", "plugin", "client", "collection"}:
                with tempfile.TemporaryDirectory(prefix="pennix-candidate-") as directory:
                    candidate = candidates.resolve(component, Path(directory), target)
                    candidate_target = candidates.lock_entry(candidate)
                    if target and candidate_target != target:
                        raise CliError(f"{component_id} 候选与 journal 精确目标不一致")
                    if not step["target"]:
                        active = apply_journal.set_target(active, step_id, candidate_target)
                        target = candidate_target
                    component["_candidate_target"] = candidate_target
                    def install(candidate=candidate, component_id=component_id, component=component, target=target):
                        result = _run_component(args, catalog, component_id, operation, candidate=candidate)
                        args.component_lock = component_lock.set_entry(args.component_lock, component_id, target, args.component_lock_sources)
                        component_lock.write(args.component_lock_path, args.component_lock, args.component_lock_sources)
                        return result
                    active, results[component_id] = _journal_mutate(active, step_id, install)
            else:
                active, results[component_id] = _journal_mutate(
                    active, step_id, lambda component_id=component_id: _run_component(args, catalog, component_id, operation))
            continue
        if action == "adopt-plugin":
            component = catalog["components"][component_id]
            def adopt_plugin(component=component, component_id=component_id, target=step["target"]):
                current = owner_engine.plugin_lock_migration(component_id, component, args.codex_home)
                if current != target:
                    raise CliError("Ponytail plugin 在恢复前身份发生变化；未接管")
                owner_engine._write_plugin_receipt(component_id, component, current)
                try:
                    args.component_lock = component_lock.set_entry(args.component_lock, component_id, current, args.component_lock_sources)
                    component_lock.write(args.component_lock_path, args.component_lock, args.component_lock_sources)
                except BaseException:
                    owner_engine._remove_plugin_receipt(component_id)
                    raise
                return "adopted"
            active, results[component_id] = _journal_mutate(active, step_id, adopt_plugin)
            continue
        if action in {"configure", "unconfigure"} and component_id in catalog["components"]:
            active, results[component_id] = _journal_mutate(
                active, step_id, lambda component_id=component_id, action=action: _run_component(args, catalog, component_id, action))
            continue
        if component_id.startswith("connection:"):
            target_name = component_id.split(":", 1)[1]
            item = value["connections"][target_name]
            def apply_connection(target_name=target_name, item=item):
                try:
                    if item.get("state") == "pending-owner":
                        fresh = connection_owners.plan(args.kconf, catalog, args.codex_home,
                            Path(config_model.config_value(args.kconf, "PENNIX_SECRETS_FILE")))[target_name]
                        if fresh["action"] == "blocked":
                            raise connection_owners.ConnectionError(fresh.get("detail", "安装后 owner 仍不可用"))
                    return connection_owners.apply_target(target_name, catalog["configuration_targets"][target_name], args.kconf,
                        args.codex_home, Path(config_model.config_value(args.kconf, "PENNIX_SECRETS_FILE")), action)
                except (connection_owners.ConnectionError, secret_store.SecretError) as error:
                    raise CliError(f"连接 {target_name} {action} 失败: {error}") from error
            active, results[f"connection:{target_name}"] = _journal_mutate(active, step_id, apply_connection)
            continue
        if action == "uninstall" and component_id in catalog["components"]:
            def uninstall(component_id=component_id):
                result = _run_component(args, catalog, component_id, "uninstall")
                if component_id in args.component_lock["components"]:
                    args.component_lock = component_lock.remove_entry(args.component_lock, component_id)
                    component_lock.write(args.component_lock_path, args.component_lock, args.component_lock_sources)
                return result
            active, results[component_id] = _journal_mutate(active, step_id, uninstall)
            continue
        if component_id == "paseo-relay":
            def apply_relay():
                if action == "uninstall":
                    return relay_owner.remove(relay_home,
                        nas_ssh=config_model.config_value(args.kconf, "PENNIX_RELAY_NAS_SSH"),
                        router_ssh=config_model.config_value(args.kconf, "PENNIX_RELAY_ROUTER_SSH"))
                prefix = Path.home() / ".local"
                paseo = prefix / "bin" / "paseo"
                if not paseo.is_file():
                    paseo = Path(shutil.which("paseo") or paseo)
                relay_spec = value["components"]["paseo-relay"]
                if relay_spec.get("register"):
                    relay_owner.ensure(relay_home,
                        nas_ssh=config_model.config_value(args.kconf, "PENNIX_RELAY_NAS_SSH"),
                        router_ssh=config_model.config_value(args.kconf, "PENNIX_RELAY_ROUTER_SSH"))
                return relay_owner.configure_daemon(
                    paseo, relay_home,
                    config_model.config_value(args.kconf, "PENNIX_PASEO_RELAY_ENDPOINT"),
                    config_model.config_value(args.kconf, "PENNIX_PASEO_RELAY_PUBLIC_ENDPOINT"),
                    bool(args.kconf.syms["PENNIX_PASEO_RELAY_TLS"].tri_value),
                    bool(args.kconf.syms["PENNIX_PASEO_RELAY_PUBLIC_TLS"].tri_value))
            active, results["paseo-relay"] = _journal_mutate(active, step_id, apply_relay)
            continue
        if component_id == "path-env":
            active, results["zshrc-user-bin-path"] = _journal_mutate(
                active, step_id, lambda: path_env.apply(Path.home() / ".local"))
            continue
        if component_id == "paseo-autostart":
            mode = value["components"]["paseo-autostart"]["mode"]
            prefix = Path.home() / ".local"
            paseo = prefix / "bin" / "paseo"
            if not paseo.is_file():
                paseo = Path(shutil.which("paseo") or paseo)
            def apply_service():
                result = paseo_service.apply(mode, paseo, Path(config_model.config_value(args.kconf, "PENNIX_PASEO_HOME")),
                    value["components"]["paseo-autostart"]["listen"], value["components"]["paseo-autostart"]["run"],
                    restart_daemon=bool(relay and relay["selected"]))
                if paseo_service.runtime(paseo, Path(config_model.config_value(args.kconf, "PENNIX_PASEO_HOME")), mode,
                                         value["components"]["paseo-autostart"]["run"])["state"] != "current":
                    raise paseo_service.PaseoServiceError("Paseo 实际 daemon/enable/active/linger 后置条件未满足")
                return result
            active, results["paseo-autostart"] = _journal_mutate(active, step_id, apply_service)
            continue
        raise CliError(f"journal 步骤没有有效执行路由: {step_id}")
    apply_journal.clear()
    _emit({"operation": "apply", "results": results}, args.json)
    return 0


def cmd_repair(args: argparse.Namespace) -> int:
    kconf, _, catalog = _load(args)
    args.kconf = kconf
    args.codex_home = Path(os.path.expanduser(config_model.config_value(kconf, "PENNIX_CODEX_HOME")))
    if args.component not in catalog["components"]:
        raise CliError(f"未知组件: {args.component}")
    operation = "upgrade"
    component = catalog["components"][args.component]
    if args.expected_sha256 is not None and args.component != "codex-agents" and component.get("delivery") != "client":
        raise CliError("--expected-sha256 只用于审阅后的 codex-agents 或固定本地客户端升级")
    if component.get("delivery") in {"package", "plugin", "client", "collection"} and not component.get("native_config"):
        if args.component not in config_model.selected_components(kconf):
            raise CliError(f"组件未在 workflow.config 中选择: {args.component}")
        entry = args.component_lock["components"].get(args.component)
        missing_lock = entry is None
        if entry is None:
            if component.get("delivery") == "package":
                entry = owner_engine.package_lock_migration(args.component, component)
            elif component.get("delivery") == "client":
                entry = owner_engine.local_clients.lock_migration(args.component, component)
            elif component.get("delivery") == "collection":
                entry = skill_collections.lock_migration(component, args.codex_home)
            elif component.get("delivery") == "plugin":
                entry = owner_engine.plugin_lock_migration(args.component, component, args.codex_home)
        if entry is None:
            raise CliError("repair 需要可信 receipt 和可映射到当前来源的 lock target")
        component["_lock_target"] = entry
        status = _component_status(args, args.component, component)
        if status["state"] not in {"match", "outdated"}:
            raise CliError(f"组件状态不允许锁定目标修复: {status['state']} ({status.get('detail', '')})")
        if component.get("delivery") == "package" and status.get("ownership") != "lifecycle":
            raise CliError("repair 需要可信 Lifecycle package receipt")
        if component.get("delivery") == "plugin" and not owner_engine._plugin_owned(args.component, component, args.codex_home):
            raise CliError("repair 需要可信 Lifecycle plugin receipt")
        if component.get("delivery") == "client" and owner_engine.local_clients.receipt(args.component, component) is None:
            raise CliError("repair 需要可信 Lifecycle client receipt")
        if component.get("delivery") == "collection" and not owner_engine.collection_owned(component, args.codex_home):
            raise CliError("repair 需要可信 Lifecycle collection receipt")
        with tempfile.TemporaryDirectory(prefix="pennix-repair-") as directory:
            try:
                candidate = candidates.resolve(component, Path(directory), entry)
            except candidates.CandidateError as error:
                raise CliError(f"无法取得锁定修复候选: {error}") from error
            candidate_target = candidates.lock_entry(candidate)
            component["_candidate_target"] = candidate_target
            result = _run_component(args, catalog, args.component, operation, candidate=candidate)
        if missing_lock:
            args.component_lock = component_lock.set_entry(args.component_lock, args.component, candidate_target, args.component_lock_sources)
            component_lock.write(args.component_lock_path, args.component_lock, args.component_lock_sources)
    else:
        result = _run_component(args, catalog, args.component, operation)
    native = component.get("native_config")
    if native and config_model.config_value(kconf, native["select"]) == "y":
        configured = _run_component(args, catalog, args.component, "configure", repair=True)
        result = f"{result}; native {configured}"
    _emit({"operation": "repair", "component": args.component, "status": result}, args.json)
    return 0


def cmd_update(args: argparse.Namespace) -> int:
    kconf, config_path, catalog = _load(args)
    args.kconf = kconf
    args.codex_home = Path(os.path.expanduser(config_model.config_value(kconf, "PENNIX_CODEX_HOME")))
    component = catalog["components"].get(args.component)
    if component is None or component.get("delivery") not in {"package", "plugin", "client", "collection"}:
        raise CliError(f"不支持更新的组件: {args.component}")
    if args.component not in config_model.selected_components(kconf):
        raise CliError(f"组件未在 workflow.config 中选择: {args.component}")
    try:
        active = apply_journal.read()
    except apply_journal.JournalError as error:
        raise CliError(f"未完成 lifecycle journal 无法读取：{error}") from error
    if active is not None:
        if active["command"] != "update" or active["component"] != args.component:
            raise CliError("存在其他未完成 lifecycle 事务；请从原入口恢复")
        if active["config_digest"] != apply_journal.digest(config_path):
            raise CliError("workflow.config 已改变；未完成 update 保留现场，不能猜测续跑")
        if apply_journal.complete(active):
            apply_journal.clear()
            active = None
        elif apply_journal.digest(args.component_lock_path) != active["lock_digest"]:
            raise CliError("components.lock.json 已改变；未完成 update 保留现场，不能猜测续跑")
        elif any(item["status"] == "started" for item in active["steps"]):
            raise CliError("update 步骤仍处于 started；保留 journal，不能自动重试")
    entry = args.component_lock["components"].get(args.component)
    if entry is None:
        raise CliError(f"组件尚无可信 lock；先通过 apply 初始化或完成 receipt 迁移: {args.component}")
    component["_lock_target"] = entry
    status = _component_status(args, args.component, component)
    if component.get("native_config"):
        try:
            package_state, package_observed = owner_engine.probe_component(component, args.codex_home)
            package_owned = owner_engine.package_owned(args.component, component, package_observed, args.codex_home)
        except owner_engine.BootstrapError as error:
            raise CliError(f"组件状态不允许更新: {error}") from error
        if package_state not in {"match", "outdated"}:
            raise CliError(f"组件状态不允许更新: {package_state}")
        if not package_owned:
            raise CliError("组件没有可信 Lifecycle package receipt")
    else:
        if status["state"] not in {"match", "outdated"}:
            raise CliError(f"组件状态不允许更新: {status['state']} ({status.get('detail', '')})")
        if component.get("delivery") == "package" and status.get("ownership") != "lifecycle":
            raise CliError("组件没有可信 Lifecycle package receipt")
    if component.get("delivery") == "client" and owner_engine.local_clients.receipt(args.component, component) is None:
        raise CliError("组件没有可信 Lifecycle client receipt")
    if component.get("delivery") == "collection" and not owner_engine.collection_owned(component, args.codex_home):
        raise CliError("组件没有可信 Lifecycle collection receipt")
    if component.get("delivery") == "plugin" and not owner_engine._plugin_owned(args.component, component, args.codex_home):
        raise CliError("组件没有可信 Lifecycle plugin receipt")
    if active is not None:
        step_id = f"{args.component}:upgrade"
        active = _retry_unknown_update(active, step_id, status["state"])
    if active is None and (not sys.stdin.isatty() or not sys.stdout.isatty()):
        raise CliError("pennix update 需要交互式终端")
    with tempfile.TemporaryDirectory(prefix="pennix-update-") as directory:
        try:
            candidate = candidates.resolve(component, Path(directory), active["steps"][0]["target"] if active else None)
        except candidates.CandidateError as error:
            raise CliError(str(error)) from error
        target = candidates.lock_entry(candidate)
        if active and target != active["steps"][0]["target"]:
            raise CliError("恢复候选与 journal 中已确认目标不一致")
        if all(entry.get(key) == target.get(key) for key in ("version", "ref", "sha256")):
            _emit({"operation": "update", "component": args.component, "status": "no-op", "target": entry}, args.json)
            return 0
        component["_candidate_target"] = target
        _emit({"operation": "update", "component": args.component, "current": entry,
               "candidate": target, "impact": candidate.get("impact", "仅替换该组件；owner 安装器影响需按计划核验")}, args.json)
        if active is None:
            if input("确认仅更新该组件？输入 yes 继续: ").strip() != "yes":
                return 1
            config_digest = apply_journal.digest(config_path)
            lock_digest = apply_journal.digest(args.component_lock_path)
            active = apply_journal.start("update", args.component, config_digest, lock_digest,
                                         [{"id": f"{args.component}:upgrade", "component": args.component,
                                           "action": "upgrade", "target": target}])
        step_id = f"{args.component}:upgrade"
        def update_component():
            result = _run_component(args, catalog, args.component, "upgrade", candidate=candidate)
            args.component_lock = component_lock.set_entry(args.component_lock, args.component, target, args.component_lock_sources)
            component_lock.write(args.component_lock_path, args.component_lock, args.component_lock_sources)
            return result
        active, result = _journal_mutate(active, step_id, update_component)
    apply_journal.clear()
    _emit({"operation": "update", "component": args.component, "status": result, "target": target}, args.json)
    return 0


def _reverse_uninstall_order(catalog: dict[str, Any]) -> list[str]:
    return [key for key in reversed(APPLY_ORDER)
            if key in catalog["components"] and catalog["components"][key].get("delivery") != "native"]


def cmd_uninstall(args: argparse.Namespace) -> int:
    kconf, _, catalog = _load(args)
    args.kconf = kconf
    args.codex_home = Path(os.path.expanduser(config_model.config_value(kconf, "PENNIX_CODEX_HOME")))
    if args.component == "paseo-relay":
        if args.confirm != args.component:
            raise CliError("relay 卸载必须使用 --confirm paseo-relay")
        try:
            result = relay_owner.remove(
                Path(config_model.config_value(kconf, "PENNIX_PASEO_HOME")),
                nas_ssh=config_model.config_value(kconf, "PENNIX_RELAY_NAS_SSH"),
                router_ssh=config_model.config_value(kconf, "PENNIX_RELAY_ROUTER_SSH"),
            )
        except relay_owner.RelayError as error:
            raise CliError(f"Paseo relay 撤回失败：{error}") from error
        _emit({"operation": "uninstall", "config_unchanged": True, "results": {"paseo-relay": result}}, args.json)
        return 0
    if args.all:
        if args.confirm_all != "ALL":
            raise CliError("全量卸载必须使用 --confirm-all ALL")
        snapshot = _plan(args)
        targets = []
        for key in _reverse_uninstall_order(catalog):
            component = catalog["components"][key]
            status = snapshot["components"][key]
            delivery = component["delivery"]
            if delivery == "package":
                if owner_engine._package_receipt(key, component) is None:
                    continue
                if not owner_engine.package_owned(key, component, codex_home=args.codex_home):
                    raise CliError(f"全量卸载预检发现包归属漂移: {key}")
            elif delivery == "client":
                if owner_engine.local_clients.receipt(key, component) is None:
                    continue
                if owner_engine.local_clients.inspect(key, component)[0] not in {"match", "outdated"}:
                    raise CliError(f"全量卸载预检发现客户端漂移: {key}")
            elif delivery == "collection" and not owner_engine.collection_owned(component, args.codex_home):
                if status["state"] in {"drifted", "blocked"}:
                    raise CliError(f"全量卸载预检发现集合漂移: {key}")
                continue
            elif delivery == "plugin":
                if owner_engine._plugin_receipt(key, component) is None:
                    continue
                if not owner_engine._plugin_owned(key, component, args.codex_home):
                    raise CliError(f"全量卸载预检发现 plugin 归属漂移: {key}")
            if status["state"] in {"drifted", "blocked"}:
                raise CliError(f"全量卸载预检发现配置漂移: {key}")
            targets.append(key)
        owned_connections = []
        for target, spec in catalog["configuration_targets"].items():
            status = connection_owners.inspect_target(target, spec, args.codex_home)
            if status.get("owned"):
                if status["state"] != "current":
                    raise CliError(f"全量卸载预检发现连接漂移: {target}")
                owned_connections.append(target)
        relay = snapshot["components"].get("paseo-relay")
        relay_result = None
        if relay_owner.local_owned():
            try:
                relay_result = relay_owner.remove(
                    Path(config_model.config_value(kconf, "PENNIX_PASEO_HOME")),
                    nas_ssh=config_model.config_value(kconf, "PENNIX_RELAY_NAS_SSH"),
                    router_ssh=config_model.config_value(kconf, "PENNIX_RELAY_ROUTER_SSH"),
                )
            except relay_owner.RelayError as error:
                raise CliError(f"relay 远端撤回预检失败，未执行任何本地卸载：{error}") from error
        elif relay and relay["selected"] and relay["state"] == "blocked":
            raise CliError(f"relay 远端撤回预检失败，未执行任何本地卸载：{relay.get('detail', '未知错误')}")
    else:
        if not args.component or args.confirm != args.component:
            raise CliError("单组件卸载必须使用 --confirm <component-id>")
        targets = [args.component]
        selected = config_model.selected_components(kconf)
        dependents = {
            "codex-cli": {"ponytail-plugin", "codex-config", "codex-agents", "context-mode"},
            "tmux": {"tmux-config"},
            "uv": {"tavily-cli"},
            "tavily-cli": {"tavily-hikari"},
            "pennix-skills": {"tavily-hikari"},
        }
        if dependents.get(args.component, set()) & selected:
            raise CliError(f"卸载 {args.component} 会破坏已选择依赖；请先修改配置并 apply")
    results: dict[str, str] = {}
    if args.all and relay_result is not None:
        results["paseo-relay"] = relay_result
    if args.all:
        for target in owned_connections:
            results[f"connection:{target}"] = connection_owners.apply_target(
                target, catalog["configuration_targets"][target], kconf, args.codex_home,
                Path(config_model.config_value(kconf, "PENNIX_SECRETS_FILE")), "unconfigure")
        paseo = Path(shutil.which("paseo") or Path.home() / ".local/bin/paseo")
        paseo_home = Path(config_model.config_value(kconf, "PENNIX_PASEO_HOME"))
        if paseo_service.inspect(paseo, paseo_home) == "current":
            results["paseo-autostart"] = paseo_service.apply("off", paseo, paseo_home, "127.0.0.1:1", False)
    for component_id in targets:
        try:
            results[component_id] = _run_component(args, catalog, component_id, "uninstall")
            if component_id in args.component_lock["components"]:
                args.component_lock = component_lock.remove_entry(args.component_lock, component_id)
                component_lock.write(args.component_lock_path, args.component_lock, args.component_lock_sources)
        except CliError:
            if args.all:
                raise
            raise
    _emit({"operation": "uninstall", "config_unchanged": True, "results": results}, args.json)
    return 0


def cmd_secrets_edit(args: argparse.Namespace) -> int:
    kconf, _ = config_model.load(args.config)
    configured = config_model.config_value(kconf, "PENNIX_SECRETS_FILE")
    path = Path(configured)
    if not path.is_absolute():
        raise CliError("secrets.toml 路径必须是绝对路径")
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise CliError("secrets edit 需要交互式终端")
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    owned_files.safe_path(path)
    if path.parent.stat().st_uid != os.getuid() or path.parent.stat().st_mode & 0o077:
        raise CliError("secrets.toml 父目录必须归当前用户且权限为 0700")
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file():
            raise CliError("secrets.toml 不是安全的普通文件")
        if path.stat().st_uid != os.getuid() or (path.stat().st_mode & 0o777) != 0o600:
            raise CliError("secrets.toml 必须归当前用户且权限为 0600")
    else:
        example = ROOT / "templates" / "secrets.example.toml"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(example.read_text(encoding="utf-8"))
    editor = shlex.split(os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi")
    if not editor:
        raise CliError("VISUAL/EDITOR 为空")
    result = subprocess.run([*editor, str(path)], check=False)
    if result.returncode:
        raise CliError(f"编辑器退出码: {result.returncode}")
    try:
        secret_store.load(path)
    except secret_store.SecretError as error:
        raise CliError(str(error)) from error
    print(path)
    return 0


def cmd_secrets_codex_token(args: argparse.Namespace) -> int:
    try:
        value = secret_store.get(Path(args.secrets_file).expanduser(), "codex", "api_key")
    except secret_store.SecretError as error:
        raise CliError(str(error)) from error
    sys.stdout.write(value + "\n")
    return 0


def cmd_secrets_import_ssh(args: argparse.Namespace) -> int:
    kconf, _ = config_model.load(args.config)
    path = Path(config_model.config_value(kconf, "PENNIX_SECRETS_FILE"))
    try:
        updated = credential_import.import_from_ssh(path, args.alias, args.service)
    except credential_import.CredentialImportError as error:
        raise CliError(str(error)) from error
    _emit({"operation": "secrets-import-ssh", "updated": sorted(updated)}, args.json)
    return 0


def cmd_cache(args: argparse.Namespace) -> int:
    cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")).expanduser() / "pennix-lifecycle"
    if args.cache_command == "list":
        owned_files.safe_path(cache)
        _emit({"path": str(cache), "entries": sorted(item.name for item in cache.iterdir()) if cache.is_dir() else []}, args.json)
        return 0
    if args.cache_command == "clean":
        owned_files.safe_path(cache)
        if cache.exists() and (not cache.is_dir() or cache.stat().st_uid != os.getuid()):
            raise CliError("生命周期缓存目录类型或归属不安全")
        entries = sorted(cache.iterdir(), key=lambda item: item.name) if cache.is_dir() else []
        if not args.yes:
            _emit({"path": str(cache), "would_remove": [item.name for item in entries], "removed": False}, args.json)
            return 0
        for item in entries:
            info = item.lstat()
            if info.st_uid != os.getuid():
                raise CliError(f"缓存项不属于当前用户，停止清理: {item.name}")
        for item in entries:
            if item.is_dir() and not item.is_symlink():
                shutil.rmtree(item)
            else:
                item.unlink()
        _emit({"path": str(cache), "removed": True}, args.json)
        return 0
    raise CliError("未知 cache 操作")


def cmd_tool(args: argparse.Namespace) -> int:
    if args.tool_command == "install":
        result = release_owner._install(ROOT)
    else:
        if not args.yes:
            raise CliError("工具自身卸载需要显式 --yes")
        path_env.remove()
        result = release_owner._uninstall()
    _emit({"tool": result}, args.json)
    return 0


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
            child.add_argument("--defconfig", help="从严格校验的 defconfig 生成 workflow.config")
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
    repair.add_argument("--expected-sha256", help="仅当 AGENTS.md 与已审阅的完整摘要相符时升级")
    repair.set_defaults(function=cmd_repair)

    update = sub.add_parser("update", help="明确升级一个已选择且受管的组件")
    update.add_argument("component")
    update.add_argument("--config")
    update.add_argument("--json", action="store_true")
    update.set_defaults(function=cmd_update)

    tool = sub.add_parser("tool", help="管理生命周期工具自身")
    tool_sub = tool.add_subparsers(dest="tool_command", required=True)
    install_tool = tool_sub.add_parser("install", help="安装当前已核验发行包")
    install_tool.add_argument("--json", action="store_true")
    install_tool.set_defaults(function=cmd_tool)
    uninstall_tool = tool_sub.add_parser("uninstall", help="卸载生命周期工具自身")
    uninstall_tool.add_argument("--yes", action="store_true")
    uninstall_tool.add_argument("--json", action="store_true")
    uninstall_tool.set_defaults(function=cmd_tool)

    uninstall = sub.add_parser("uninstall")
    uninstall.add_argument("component", nargs="?")
    uninstall.add_argument("--all", action="store_true")
    uninstall.add_argument("--confirm")
    uninstall.add_argument("--confirm-all")
    uninstall.add_argument("--config")
    uninstall.add_argument("--json", action="store_true")
    uninstall.set_defaults(function=cmd_uninstall)

    secrets = sub.add_parser("secrets", help="编辑私有明文 secrets.toml")
    secrets_sub = secrets.add_subparsers(dest="secrets_command", required=True)
    secret_edit = secrets_sub.add_parser("edit")
    secret_edit.add_argument("--config")
    secret_edit.set_defaults(function=cmd_secrets_edit)
    secret_token = secrets_sub.add_parser("codex-token", help="供 Codex auth command 安全读取静态 API Key")
    secret_token.add_argument("--secrets-file", required=True)
    secret_token.set_defaults(function=cmd_secrets_codex_token)
    secret_import = secrets_sub.add_parser("import-ssh", help="从既有 SSH 别名读取已登记凭据文件")
    secret_import.add_argument("--alias", required=True)
    secret_import.add_argument("--service", action="append", required=True, choices=sorted(credential_import.SOURCES))
    secret_import.add_argument("--config")
    secret_import.add_argument("--json", action="store_true")
    secret_import.set_defaults(function=cmd_secrets_import_ssh)

    cache = sub.add_parser("cache", help="管理工具自有缓存")
    cache_sub = cache.add_subparsers(dest="cache_command", required=True)
    for name in ("list", "clean"):
        child = cache_sub.add_parser(name)
        child.add_argument("--json", action="store_true")
        if name == "clean":
            child.add_argument("--yes", action="store_true", help="执行预览中的精确清理")
        child.set_defaults(function=cmd_cache)
    return p


def main(argv: list[str] | None = None) -> int:
    try:
        args = parser().parse_args(argv)
        mutating = (
            args.command in {"apply", "repair", "update", "uninstall", "secrets", "tool"}
            or (args.command == "config" and args.config_command in {"init", "edit"})
            or (args.command == "cache" and args.cache_command == "clean" and args.yes)
        )
        if mutating:
            with owned_files.locked():
                return args.function(args)
        return args.function(args)
    except (CliError, apply_journal.JournalError, connection_owners.ConnectionError, config_model.ConfigError, codex_config.CodexConfigError, path_env.PathEnvError, paseo_service.PaseoServiceError, relay_owner.RelayError, release_owner.ReleaseError, skill_collections.CollectionError, owner_engine.local_clients.ClientError, secret_store.SecretError, owned_files.OwnershipError, native_integrations.NativeIntegrationError, native_integrations.hook_integrations.HookError, OSError) as error:
        print(f"pennix: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
