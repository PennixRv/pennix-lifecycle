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
import tomllib

import _engine as owner_engine
import codex_config
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
        tier = config_model.choice_value(args.kconf, "PENNIX_FASTCTX_TIER_")
        try:
            # Inspect the real native state even when the Kconfig selection is off;
            # disabling a component must not orphan an external or drifted MCP entry.
            return native_integrations.inspect(native["owner"], args.codex_home, enabled=True, tier=tier)
        except native_integrations.NativeIntegrationError as error:
            return {"state": "blocked", "owned": False, "detail": str(error)}
    try:
        state, detail = owner_engine.probe_component(
            component, args.codex_home, home_directory=Path.home()
        )
    except Exception as error:
        return {"state": "blocked", "detail": str(error)}
    result = {"state": state, "detail": detail}
    if component.get("delivery") == "package" and isinstance(component.get("package"), dict):
        result["ownership"] = "lifecycle" if owner_engine.package_owned(
            component_id, component, detail if state == "match" else None, args.codex_home
        ) else "external"
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
            return {
                "selected": True,
                "register": False,
                "action": "configure",
                "state": "unverified",
                "detail": "仅配置 daemon relay，不访问远端登记表",
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
        status = _component_status(args, component_id, component)
        if component.get("native_config"):
            package_state, package_observed = owner_engine.probe_component(component, args.codex_home)
            package_owned = owner_engine.package_owned(component_id, component, package_observed, args.codex_home)
            status["package_state"] = package_state
            status["package_ownership"] = "lifecycle" if package_owned else "external"
            if component_id in selected:
                if package_state == "missing":
                    action = "install"
                elif package_state == "match" and not package_owned:
                    action = "adopt" if config_model.config_value(kconf, "PENNIX_ADOPT_EXISTING_PACKAGES") == "y" else "blocked"
                elif package_state in {"upgrade-required", "drifted", "blocked"}:
                    action = "repair"
                else:
                    action = "keep"
                native_enabled = config_model.config_value(kconf, component["native_config"]["select"]) == "y"
                native_action = "keep" if not native_enabled or status["state"] == "current" else (
                    "repair" if status["state"] == "drifted" else "configure"
                )
                if native_enabled and status["state"] in {"blocked", "external"}:
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
            if (component.get("delivery") == "package" and status["state"] == "match"
                    and status.get("ownership") != "lifecycle"):
                action = "adopt" if config_model.config_value(kconf, "PENNIX_ADOPT_EXISTING_PACKAGES") == "y" else "blocked"
                if action == "blocked":
                    status["detail"] = "已安装但非生命周期归属；需显式开启接管"
            else:
                action = "keep" if status["state"] in {"match", "current", "present"} else "install"
            if status["state"] in {"upgrade-required", "drifted", "blocked"}:
                action = "repair"
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
                else:
                    action = "uninstall" if status["state"] in {"match", "current", "present"} else "keep"
            if action == "uninstall" and component.get("delivery") == "collection" and not owner_engine.collection_owned(component, args.codex_home):
                action = "keep"
                status["ownership"] = "external"
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
    components["paseo-autostart"] = {
        "selected": mode != "off" or run,
        "mode": mode,
        "run": run,
        "listen": listen,
        "action": service_action,
        "state": service_state,
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
        )
    except connection_owners.ConnectionError as error:
        raise CliError(str(error)) from error
    return {"config": str(path), "codex_home": str(args.codex_home), "components": components, "connections": connections}


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


def _ensure_connection_dependencies(selected: set[str], connections: dict[str, Any], catalog: dict[str, Any]) -> None:
    for target, item in connections.items():
        if item["selected"]:
            component = catalog["configuration_targets"][target]["component"]
            if component not in selected:
                raise CliError(f"连接 {target} 依赖未选择: {component}")


def _run_component(args: argparse.Namespace, catalog: dict[str, Any], component_id: str, operation: str) -> str:
    component = catalog["components"][component_id]
    native = component.get("native_config")
    if native and operation in {"configure", "unconfigure"}:
        tier = config_model.choice_value(args.kconf, "PENNIX_FASTCTX_TIER_")
        if operation == "unconfigure":
            return native_integrations.unconfigure(native["owner"], args.codex_home, tier=tier)
        return native_integrations.apply(native["owner"], args.codex_home, tier=tier)
    if component_id == "codex-config":
        if operation == "uninstall":
            return codex_config.uninstall(args.codex_home / "config.toml")
        values = config_model.values(args.kconf)
        return codex_config.apply(args.codex_home / "config.toml", values)
    engine_args = _args_for_engine(args, operation)
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
    _emit(value, args.json)
    bad = []
    for item in value["components"].values():
        if not item["selected"]:
            continue
        if item.get("native_action") is not None:
            if (item["action"] != "keep" or item.get("package_state") != "match"
                    or item.get("package_ownership") != "lifecycle" or item["native_action"] != "keep"):
                bad.append(item)
        elif item["state"] not in {"match", "current", "present"}:
            bad.append(item)
    bad.extend(item for item in value["connections"].values() if item["selected"] and item["action"] == "blocked")
    service = value["components"].get("paseo-autostart")
    if service and service["action"] == "blocked":
        bad.append(service)
    return 1 if bad else 0


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
    blocked = [key for key in selected if value["components"].get(key, {}).get("action") == "blocked"]
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
    relay_home = Path(config_model.config_value(args.kconf, "PENNIX_PASEO_HOME"))
    relay_result = None
    if relay and relay["action"] == "uninstall":
        try:
            relay_result = relay_owner.remove(
                relay_home,
                nas_ssh=config_model.config_value(args.kconf, "PENNIX_RELAY_NAS_SSH"),
                router_ssh=config_model.config_value(args.kconf, "PENNIX_RELAY_ROUTER_SSH"),
            )
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
        if item["selected"] and item["action"] in {"install", "adopt"}:
            results[component_id] = _run_component(args, catalog, component_id, "install")
        elif item["selected"] and item["state"] in {"match", "current"} and config_model.config_value(args.kconf, "PENNIX_UPDATE_COMPONENTS") == "y":
            results[component_id] = _run_component(args, catalog, component_id, "upgrade")
    for component_id in order:
        item = value["components"][component_id]
        if item["selected"] and item.get("native_action") == "configure":
            results[component_id] = _run_component(args, catalog, component_id, "configure")
    for target, item in value["connections"].items():
        if item["action"] not in {"configure", "unconfigure"}:
            continue
        try:
            results[f"connection:{target}"] = connection_owners.apply_target(
                target,
                catalog["configuration_targets"][target],
                args.kconf,
                args.codex_home,
                Path(config_model.config_value(args.kconf, "PENNIX_SECRETS_FILE")),
                item["action"],
            )
        except (connection_owners.ConnectionError, secret_store.SecretError) as error:
            raise CliError(f"连接 {target} {item['action']} 失败: {error}") from error
    for component_id in order:
        item = value["components"][component_id]
        if not item["selected"] and item.get("native_action") == "unconfigure":
            results[component_id] = _run_component(args, catalog, component_id, "unconfigure")
    for component_id in order:
        item = value["components"][component_id]
        if not item["selected"] and item["action"] == "uninstall":
            results[component_id] = _run_component(args, catalog, component_id, "uninstall")
    prefix = Path.home() / ".local"
    results["zshrc-user-bin-path"] = path_env.apply(prefix)
    mode = value["components"]["paseo-autostart"]["mode"]
    paseo = prefix / "bin" / "paseo"
    if not paseo.is_file():
        paseo = Path(shutil.which("paseo") or paseo)
    if config_model.config_value(args.kconf, "PENNIX_SYSTEM_UPGRADE") == "y":
        result = subprocess.run(["sudo", "pacman", "-Syu", "--noconfirm"], check=False)
        if result.returncode:
            raise CliError(f"完整系统升级失败: {result.returncode}")
        results["system-upgrade"] = "changed"
    if relay and relay["selected"]:
        try:
            if relay.get("register") and relay["action"] == "install":
                relay_owner.ensure(
                    relay_home,
                    nas_ssh=config_model.config_value(args.kconf, "PENNIX_RELAY_NAS_SSH"),
                    router_ssh=config_model.config_value(args.kconf, "PENNIX_RELAY_ROUTER_SSH"),
                )
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
    try:
        results["paseo-autostart"] = paseo_service.apply(
            mode,
            paseo,
            Path(config_model.config_value(args.kconf, "PENNIX_PASEO_HOME")),
            value["components"]["paseo-autostart"]["listen"],
            value["components"]["paseo-autostart"]["run"],
            restart_daemon=bool(relay and relay["selected"]),
        )
    except paseo_service.PaseoServiceError as error:
        raise CliError(str(error)) from error
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
    if component.get("native_config"):
        planned = _plan(args)["components"][args.component]
        if planned.get("native_action") == "repair":
            operation = "configure"
    result = _run_component(args, catalog, args.component, operation)
    _emit({"operation": "repair", "component": args.component, "status": result}, args.json)
    return 0


def _reverse_uninstall_order(catalog: dict[str, Any]) -> list[str]:
    order = [
        "pennix-skills", "pennix-skills-extra", "codex-agents", "codex-config", "tavily-hikari",
        "aoe", "tmux-config", "tmux", "cch-status", "ponytail-plugin", "paseo-cli",
        "codegraph-cli", "fastctx", "pnpm", "trellis-cli", "codex-cli",
    ]
    return [key for key in order if key in catalog["components"] and catalog["components"][key].get("delivery") != "native"]


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
        relay = _plan(args)["components"].get("paseo-relay")
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
        targets = _reverse_uninstall_order(catalog)
    else:
        if not args.component or args.confirm != args.component:
            raise CliError("单组件卸载必须使用 --confirm <component-id>")
        targets = [args.component]
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
    repair.set_defaults(function=cmd_repair)

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
            args.command in {"apply", "repair", "uninstall", "secrets", "tool"}
            or (args.command == "config" and args.config_command in {"init", "edit"})
            or (args.command == "cache" and args.cache_command == "clean" and args.yes)
        )
        if mutating:
            with owned_files.locked():
                return args.function(args)
        return args.function(args)
    except (CliError, connection_owners.ConnectionError, config_model.ConfigError, codex_config.CodexConfigError, path_env.PathEnvError, paseo_service.PaseoServiceError, relay_owner.RelayError, release_owner.ReleaseError, skill_collections.CollectionError, secret_store.SecretError, owned_files.OwnershipError, OSError) as error:
        print(f"pennix: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
