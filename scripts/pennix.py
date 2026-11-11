#!/usr/bin/env python3
"""Pennix lifecycle command line interface."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
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
    "codex-cli", "trellis-cli", "pnpm", "context-mode", "codegraph-cli", "paseo-cli", "trellis-paseo-adapter",
    "ponytail-plugin", "cch-status", "tmux", "tmux-config", "aoe", "uv", "tavily-cli",
    "markitdown", "pandoc", "libreoffice", "poppler", "tesseract", "tesseract-eng", "tesseract-chi-sim", "noto-fonts-cjk",
    "tavily-hikari", "peon-ping", "codex-config", "codex-agents", "pennix-skills-extra",
    "pennix-skills",
]
DEPENDENCIES = {
    "tmux-config": {"tmux"}, "ponytail-plugin": {"codex-cli"},
    "codex-config": {"codex-cli"}, "codex-agents": {"codex-cli"},
    "context-mode": {"codex-cli"}, "tavily-hikari": {"tavily-cli", "pennix-skills"},
    "tavily-cli": {"uv"}, "markitdown": {"uv"},
    "trellis-paseo-adapter": {"trellis-cli", "paseo-cli"},
}


class CliError(RuntimeError):
    """A user-facing lifecycle error."""


def _args_for_engine(args: argparse.Namespace, command: str) -> argparse.Namespace:
    args = copy.copy(args)
    args.command = command
    args.codex_home = Path(os.path.expanduser(args.codex_home))
    args.home_directory = Path.home()
    args.destination = None
    args.yes = True
    return args


def _load(args: argparse.Namespace) -> tuple[config_model.StrictKconfig, Path, dict[str, Any]]:
    path_env.activate(Path.home() / ".local")
    kconf, path = config_model.load(args.config)
    args.codex_home = Path(config_model.config_value(kconf, "PENNIX_CODEX_HOME"))
    owned_files.bind_target(args.codex_home)
    os.environ["CODEX_HOME"] = str(args.codex_home)
    active = apply_journal.read()
    if active is not None and getattr(args, "command", None) in {"apply", "update", "reconcile", "repair", "uninstall"}:
        expected = {"home": str(Path.home()), "codex_home": str(args.codex_home), "state_root": str(owned_files.state_root())}
        if active.get("identity") != expected:
            raise CliError("事务目标身份缺失或不匹配；保留旧 journal，不自动重放")
    snapshot = getattr(args, "config_snapshot", kconf.snapshot)
    if snapshot != kconf.snapshot:
        raise CliError("workflow.config 在计划期间发生变化；未沿用旧计划")
    args.config_snapshot = snapshot
    args.config_path = path
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
        lock_before = owned_files.read(lock_path, private=True)
        if getattr(args, "component_lock_snapshot", lock_before) != lock_before:
            raise CliError("components.lock.json 在计划期间发生变化；未沿用旧计划")
        args.component_lock = component_lock.read(lock_path, sources)
        if owned_files.read(lock_path, private=True) != lock_before:
            raise CliError("components.lock.json 在读取期间发生变化")
        args.component_lock_snapshot = lock_before
        args.component_lock_path = lock_path
        args.component_lock_sources = sources
    except component_lock.LockError as error:
        raise CliError(str(error)) from error
    return kconf, path, catalog


def _verify_inputs(args: argparse.Namespace) -> None:
    if hasattr(args, "config_snapshot") and owned_files.read(args.config_path) != args.config_snapshot:
        raise CliError("确认后 workflow.config 已变化；未执行旧计划")
    if hasattr(args, "component_lock_snapshot") and owned_files.read(args.component_lock_path, private=True) != args.component_lock_snapshot:
        raise CliError("确认后 components.lock.json 已变化；未执行旧计划")


def _write_lock(args: argparse.Namespace) -> None:
    _verify_inputs(args)
    previous = getattr(args, "component_lock_snapshot", owned_files.read(args.component_lock_path, private=True))
    component_lock.validate(args.component_lock, args.component_lock_sources)
    output = owned_files.json_bytes(args.component_lock)
    owned_files.replace(args.component_lock_path, output, previous)
    args.component_lock_snapshot = output
    apply_journal.checkpoint_lock(hashlib.sha256(output).hexdigest())


def _start_transaction(args, command, component, steps):
    _verify_inputs(args)
    owned_files.bind_target(args.codex_home, register=True)
    os.environ["CODEX_HOME"] = str(args.codex_home)
    return apply_journal.start(command, component, apply_journal.digest(args.config_path),
                               apply_journal.digest(args.component_lock_path), steps)


def _open_transaction(args, command, component, steps):
    active = apply_journal.read()
    if active is None:
        return _start_transaction(args, command, component, steps)
    identity = {"home": str(Path.home()), "codex_home": str(args.codex_home),
                "state_root": str(owned_files.state_root())}
    if (active.get("identity") != identity or active["command"] != command or active["component"] != component
            or active["config_digest"] != apply_journal.digest(args.config_path)
            or active["lock_digest"] != apply_journal.digest(args.component_lock_path)):
        raise CliError("存在其他目标或输入已变化的生命周期事务；保留现场")
    return active


def _package_diagnostics(component_id: str, component: dict[str, Any], state: str) -> dict[str, Any]:
    receipt = owner_engine._package_receipt(component_id, component)
    result = {"managed_record": receipt is not None}
    if receipt is not None:
        result["receipt_version"] = receipt["version"]
    if component.get("_lock_target"):
        result["locked_identity"] = component["_lock_target"]
    if state == "outdated":
        result["mismatch_reason"] = "observed_identity_differs_from_lock"
    if component["package"].get("source") in {"official", "aur"} and state != "missing":
        result["observed_identity"] = owner_engine.package_identity(component)
    return result


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
    if component.get("_lock_target"):
        result["locked_identity"] = component["_lock_target"]
    if state == "outdated":
        result["mismatch_reason"] = "observed_identity_differs_from_lock"
    if component.get("delivery") == "client" and state in {"external", "drifted"}:
        result["review_sha256"] = owner_engine.local_clients.fingerprint(component)
    if component.get("delivery") == "package" and isinstance(component.get("package"), dict):
        try:
            result.update(_package_diagnostics(component_id, component, state))
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
        remote_owned = relay_owner.local_owned()
        local_owned = relay_owner.daemon_owned(paseo_home)
        if not remote_owned and not local_owned:
            return None
        return {
            "selected": False,
            "action": "uninstall",
            "state": "owned",
            "register": remote_owned,
            "local_config_owned": local_owned,
            "detail": "本地 relay 配置及远端登记分别按归属撤回",
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
                if component.get("delivery") == "package":
                    status.update(_package_diagnostics(component_id, component, package_state))
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
        elif component_id in selected:
            if component.get("delivery") == "client" and status["state"] == "external":
                status["state"] = "adoption-required"
            if (component.get("delivery") == "package" and status["state"] == "match"
                    and status.get("ownership") != "lifecycle"):
                action = ("keep" if component.get("package", {}).get("external_satisfies") else
                          "adopt" if config_model.config_value(kconf, "PENNIX_ADOPT_EXISTING_PACKAGES") == "y" else "blocked")
                if action == "blocked":
                    status["detail"] = "已安装但非生命周期归属；需显式开启接管"
            else:
                action = "keep" if status["state"] in {"match", "current", "present"} else "install"
            if status["state"] == "upgrade-required":
                action = "blocked"
            if status["state"] == "migration-required":
                action = "repair"
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
                        action = "keep"
                        status.update(state="owned-legacy", ownership="lifecycle",
                                      detail="共享用户集合保留；取消此配置选择不自动卸载。显式卸载影响全部宿主配置")
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
            action = "adopt-ponytail-plugin"
            status.update(state="adoption-required", action="adopt-ponytail-plugin",
                          plugin_adoption=plugin_adoption,
                          detail="精确 Marketplace 来源、版本和干净完整提交匹配；等待专用接管确认")
        installed_state = package_state if component.get("native_config") else status["state"]
        if (component_id in selected and component.get("delivery") in {"package", "plugin", "client", "collection"}
                and lock_target is None and installed_state != "missing"
                and not (component.get("package", {}).get("external_satisfies")
                         and status.get("ownership") == "external" and status["state"] == "match")):
            status.update(state="lock-migration-required", detail="已有安装必须先由可信 owner receipt 初始化 components.lock.json")
            action = "blocked"
        if component_id not in selected:
            if component.get("delivery") == "package":
                receipt = owner_engine._package_receipt(component_id, component)
                if receipt is not None and owner_engine.package_absent(component):
                    action = "uninstall"
                    status.update(state="orphaned", ownership="lifecycle", detail="包已被原生 owner 证明缺失；仅撤回受管记录")
                elif status.get("ownership", status.get("package_ownership")) == "lifecycle":
                    action = "uninstall" if status.get("package_state", status["state"]) in {"match", "outdated"} else "blocked"
            elif component.get("delivery") == "plugin":
                receipt = owner_engine._plugin_receipt(component_id, component)
                if receipt is not None:
                    action = "uninstall" if (owner_engine._plugin_owned(component_id, component, args.codex_home)
                        or owner_engine.plugin_absent(component_id, component, args.codex_home)) else "blocked"
            elif component.get("delivery") == "client":
                if owner_engine.local_clients.receipt(component_id, component) is not None:
                    action = "uninstall" if status["state"] in {"match", "outdated"} else "blocked"
            elif component_id == "codex-agents":
                target_path = owner_engine._static_target(component_id, args.codex_home)
                record = owned_files.read_json(owner_engine._agents_receipt(target_path))
                content = owned_files.read(target_path)
                if record is not None:
                    action = "uninstall" if (content is not None and record == owner_engine._agents_record(target_path, content)
                        or content is None and record.get("path") == str(target_path) and record.get("schema") == 1) else "blocked"
        elif component.get("delivery") == "plugin" and status["state"] == "match" and not plugin_adoption:
            if not owner_engine._plugin_owned(component_id, component, args.codex_home):
                action = "blocked"
                status["detail"] = "lock 不代表归属；plugin 需要独立可信 receipt 和专用接管确认"
        components[component_id] = {**status, "selected": component_id in selected, "action": action}
    prefix = Path.home() / ".local"
    paseo = prefix / "bin" / "paseo"
    if not paseo.is_file():
        paseo = Path(shutil.which("paseo") or paseo)
    mode = config_model.choice_value(kconf, "PENNIX_PASEO_AUTOSTART_")
    run = config_model.config_value(kconf, "PENNIX_PASEO_RUN") == "y"
    listen = config_model.config_value(kconf, "PENNIX_PASEO_LISTEN")
    paseo_home = Path(config_model.config_value(kconf, "PENNIX_PASEO_HOME"))
    service_selected = mode != "off" or run
    service_state = paseo_service.inspect(paseo, paseo_home)
    runtime = ({"state": "missing", "running": False, "healthy": False, "enabled": False,
                "linger": False, "daemon_version": None} if service_selected and components.get("paseo-cli", {}).get("state") == "missing" else paseo_service.runtime(paseo, paseo_home, mode, run)
               if service_selected or service_state in {"current", "migration-required"}
               or config_model.config_value(kconf, "PENNIX_PASEO_RELAY") == "y"
               else {"state": "current", "running": False, "enabled": False,
                     "linger": False, "daemon_version": None})
    cli_lock = args.component_lock["components"].get("paseo-cli", {})
    paseo_config = (paseo_service.observe(paseo, paseo_home, listen, cli_lock.get("version"), runtime)
                    if service_selected else None)
    if (mode != "off" and service_state == "absent"
            and (runtime.get("enabled") or runtime.get("active"))):
        service_action = "blocked"
        service_detail = "存在未由 Lifecycle 管理的同名 Paseo user unit"
    elif runtime["state"] == "unknown" and (service_selected or service_state in {"current", "migration-required"}):
        service_action = "blocked"
        service_detail = "Paseo daemon 或 user manager 不可核验"
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
    if (service_action != "blocked" and service_selected and paseo_config
            and paseo_config["skills_sync"]["state"] == "not-installed"):
        service_action = "configure"
        service_detail = (
            "通过 Paseo 官方 Skills API 完成首次安装；会触及 .agents/skills、.claude/skills，"
            "以及仅原生归属的旧 .codex/skills；停止态 daemon 将临时启动并按实例停止"
        )
    if service_action == "keep" and service_selected and runtime["state"] != "current":
        service_action = ("blocked" if runtime.get("unit_failed") or runtime.get("running") and not runtime.get("healthy")
                          else "start" if run and not runtime.get("running") else "install")
        service_detail = "Paseo 显式启动请求、自启 enablement 或实际运行健康尚未满足"
    if service_action == "keep" and service_selected and paseo_config["state"] != "current":
        service_action = "configure"
        service_detail = "Paseo 目标配置、Skills 同步或 CLI/daemon 版本尚未一致"
    components["paseo-autostart"] = {
        "selected": service_selected,
        "mode": mode,
        "run": run,
        "listen": listen,
        "action": service_action,
        "state": ("current" if service_action == "keep" and runtime["state"] == "current"
                  and (paseo_config is None or paseo_config["state"] == "current") else service_state),
        "unit_state": service_state,
        "runtime": runtime,
        **({"paseo_config": paseo_config} if paseo_config is not None else {}),
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
                "path": {"path": str(Path.home() / ".zshrc"), "prefix": str(Path.home() / ".local"), "action": path_env.inspect(str(Path.home() / ".local"))},
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
            if item.get("detail"):
                print(f"  {item['detail']}")
        for key, item in value.get("connections", {}).items():
            print(f"connection:{key}: {item['action']} ({item['state']})")
            if item.get("detail"):
                print(f"  {item['detail']}")
        functional = value.get("functional_checks", {})
        if "state" in functional:
            print(f"functional: {functional['state']}")
        else:
            for key, item in functional.items():
                print(f"functional:{key}: {item['state']}" + (f" ({item['detail']})" if item.get("detail") else ""))
        side_effects = value.get("side_effects", {})
        if side_effects.get("path"):
            print(f"PATH: {side_effects['path']['action']} {side_effects['path']['path']} <- {side_effects['path']['prefix']}/bin")
        if side_effects.get("system_upgrade"):
            print(f"system-upgrade: confirm {side_effects['system_upgrade_command']} (system-wide; lifecycle Arch components: {', '.join(side_effects['system_upgrade_components']) or 'none'})")
        return
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _ensure_dependencies(selected: set[str], component_id: str) -> None:
    missing = DEPENDENCIES.get(component_id, set()) - selected
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
    _verify_inputs(args)
    if operation in {"install", "upgrade", "uninstall"}:
        _delegation_preflight(args, component_id, operation)
    component = catalog["components"][component_id]
    owned_files.bind_target(args.codex_home, register=True)
    native = component.get("native_config")
    if native and operation in {"configure", "unconfigure"}:
        if operation == "unconfigure":
            return native_integrations.unconfigure(native["owner"], args.codex_home)
        return native_integrations.apply(native["owner"], args.codex_home,
            adopt=config_model.config_value(args.kconf, "PENNIX_ADOPT_EXISTING_PACKAGES") == "y",
            repair=repair)
    if native and operation == "uninstall":
        if not (owner_engine.package_absent(component) or
                owner_engine.package_owned(component_id, component, codex_home=args.codex_home)):
            raise CliError("包归属未通过预检；未撤回 native 配置")
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


def _delegation_preflight(args: argparse.Namespace, component_id: str, operation: str) -> None:
    if component_id == "trellis-paseo-adapter" and operation != "uninstall":
        try:
            version = subprocess.run(["node", "--version"], check=True, capture_output=True, text=True, timeout=15).stdout.strip()
            if int(version.lstrip("v").split(".")[0]) < 22:
                raise ValueError("unsupported Node")
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            raise CliError("trellis-paseo-adapter 需要 Node >=22；基础 Trellis 的 Node 要求不变") from error
    if component_id not in {"codex-cli", "trellis-cli", "trellis-paseo-adapter", "codegraph-cli", "paseo-cli"}:
        return
    home = Path(config_model.config_value(args.kconf, "PENNIX_PASEO_HOME"))
    executable = shutil.which("paseo")
    if not executable or not home.is_dir():
        return
    try:
        status = subprocess.run([executable, "daemon", "status", "--home", str(home), "--json"], capture_output=True, text=True, timeout=30, check=False)
        value = json.loads(status.stdout)
        if status.returncode or not isinstance(value, dict):
            raise ValueError("unknown daemon")
        if value.get("pid") is None:
            return
        result = subprocess.run([executable, "ls", "-g", "--background", "--json", "--home", str(home)], capture_output=True, text=True, timeout=30, check=False)
        agents = json.loads(result.stdout)
        if result.returncode or not isinstance(agents, list) or any(not isinstance(item, dict) or not isinstance(item.get("status"), str) for item in agents):
            raise ValueError("unknown agents")
        if agents and any(not isinstance(item.get("labels"), dict) for item in agents):
            # Paseo's list renderer omits labels; absence is not an unlabelled agent.
            adapter = shutil.which("trellis-paseo-adapter")
            if not adapter:
                current = os.environ.get("PASEO_AGENT_ID")
                if current and all(item.get("id") == current for item in agents):
                    return
                raise ValueError("native labels unavailable without adapter")
            request = {"version": 1, "operation": "inspect", "daemon_selector": {"home": str(home)}, "payload": {}}
            inspected = subprocess.run([adapter], input=json.dumps(request), capture_output=True, text=True, timeout=30, check=False)
            if inspected.returncode or len(inspected.stdout) > 256 * 1024:
                raise ValueError("unknown adapter inspection")
            receipt = json.loads(inspected.stdout)
            if not isinstance(receipt, dict) or receipt.get("version") != 1 or receipt.get("operation") != "inspect" or receipt.get("ok") is not True:
                raise ValueError("unverified adapter receipt")
            observed = receipt.get("observations", {})
            if not isinstance(observed, dict):
                raise ValueError("invalid adapter observations")
            identity = observed.get("identity", {})
            if not isinstance(identity, dict) or identity.get("pid") != value["pid"] or identity.get("home") != str(home.resolve()) or (value.get("serverId") and identity.get("server_id") != value["serverId"]):
                raise ValueError("daemon identity mismatch")
            full_agents = observed.get("agents")
            if not isinstance(full_agents, list) or any(not isinstance(item, dict) or not isinstance(item.get("id"), str) or not isinstance(item.get("status"), str) or not isinstance(item.get("labels"), dict) for item in full_agents):
                raise ValueError("unverified native agent labels")
            if not {item.get("id") for item in agents}.issubset({item["id"] for item in full_agents}):
                raise ValueError("native inspection omitted listed agents")
            agents = full_agents
        if any((item.get("labels", {}).get("trellis.attempt") or item.get("labels", {}).get("trellis.task_id")) and (item["status"] != "closed" or item.get("activeTurn")) for item in agents):
            raise CliError("存在未关闭的 Trellis 工作树委派；先由 controller 保全、接受或取消并核销，再关闭原生 agent，未替换共享组件")
    except (OSError, ValueError, TypeError, subprocess.TimeoutExpired) as error:
        raise CliError("Paseo 委派运行态不可核验；保留现场，未替换共享组件") from error


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
            if item.get("unit_state") == "external":
                continue
            if item.get("action") not in {"uninstall", "blocked"} and item.get("native_action") not in {"unconfigure", "blocked"}:
                continue
            bad.append(item)
            continue
        if item.get("native_action") is not None:
            if (item["action"] != "keep" or item.get("package_state") != "match"
                    or item.get("package_ownership") != "lifecycle" or item["native_action"] != "keep"):
                bad.append(item)
        elif item["action"] != "keep" or item["state"] not in {"match", "current", "present"}:
            bad.append(item)
    bad.extend(item for item in value["connections"].values() if (
        item["selected"] and (item["action"] != "keep" or item["state"] != "current")
        or not item["selected"] and item["action"] in {"unconfigure", "blocked"}))
    service = value["components"].get("paseo-autostart")
    if service and (service["action"] == "blocked" and service.get("unit_state") != "external" or service["selected"] and (
            service["action"] != "keep" or service.get("state") != "current")):
        bad.append(service)
    if value.get("functional_checks_requested"):
        _, _, catalog = _load(args)
        probes = connection_owners.functional_checks(catalog, value["connections"], args.codex_home)
        value["functional_checks"] = probes
        bad.extend(probe for probe in probes.values() if probe["state"] != "passed")
    else:
        value["functional_checks"] = {"state": "not_requested"}
    if apply_journal.read() is not None:
        value["transaction"] = {"state": "pending"}
        bad.append(value["transaction"])
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
                          "target": item.get("candidate_target") or args.component_lock["components"].get(key, {})})
        if item.get("selected") and item.get("plugin_adoption"):
            steps.append({"id": f"{key}:adopt-plugin", "component": key,
                          "action": "adopt-plugin", "target": item["plugin_adoption"]})
        if key == "paseo-cli" and value["components"]["paseo-autostart"].get("selected"):
            steps.append({"id": "paseo-config:configure", "component": "paseo-config",
                          "action": "configure", "target": {
                              "listen": value["components"]["paseo-autostart"]["listen"],
                              "skills_selection": {"mode": "all"},
                          }})
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
    removals = []
    for key in reversed(APPLY_ORDER):
        item = value["components"].get(key)
        if item and not item.get("selected") and item.get("action") == "uninstall":
            removals.append({"id": f"{key}:uninstall", "component": key, "action": "uninstall", "target":
                          args.component_lock["components"].get(key, {})})
    relay = value["components"].get("paseo-relay")
    if relay and relay.get("action") in {"install", "configure", "uninstall"}:
        steps.append({"id": f"paseo-relay:{relay['action']}", "component": "paseo-relay",
                      "action": relay["action"], "target": {}})
    steps.append({"id": "path-env:apply", "component": "path-env", "action": "apply", "target": {}})
    service = value["components"]["paseo-autostart"]
    paseo_action = value["components"].get("paseo-cli", {}).get("action")
    restart_daemon = bool(service["runtime"].get("running") and (
        service.get("paseo_config", {}).get("restart_required")
        or paseo_action in {"install", "upgrade", "adopt-upgrade"}
        or relay and relay.get("action") in {"configure", "install"}
    ))
    if (service.get("selected") or service.get("unit_state") in {"current", "migration-required"}
            or relay and relay.get("action") in {"install", "configure", "uninstall"}):
        steps.append({"id": f"paseo-autostart:{service['action']}",
                      "component": "paseo-autostart", "action": "service",
                      "target": {"restart_daemon": restart_daemon, "service_contract": "autostart-v1"}})
    return steps + removals


def _journal_for_apply(args: argparse.Namespace, value: dict[str, Any], steps: list[dict[str, Any]], *, create: bool = True) -> tuple[dict | None, bool]:
    _verify_inputs(args)
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
            raise CliError("components.lock.json 在未完成 apply 期间发生变化；保留现场，不能猜测续跑")
        if apply_journal.complete(active):
            apply_journal.clear()
            active = None
        else:
            return active, True
    if not create:
        return None, False
    return _start_transaction(args, "apply", None, steps), False


def _journal_mutate(value: dict, step_id: str, action):
    step = apply_journal.step(value, step_id)
    if step["status"] == "verified":
        return value, "resumed"
    if step["status"] in {"started", "unknown"}:
        raise CliError(f"步骤 {step_id} 的副作用状态未知；保留 journal，需人工核验")
    value = apply_journal.started(value, step_id)
    try:
        result = action()
    except BaseException as error:
        try:
            cause = error
            while not hasattr(cause, "effect") and cause.__cause__ is not None:
                cause = cause.__cause__
            apply_journal.rejected(apply_journal.read() or value, step_id, getattr(cause, "effect", "unknown"))
        except BaseException:
            pass
        raise
    return apply_journal.verified(apply_journal.read() or value, step_id), result


def _retry_unknown_update(active: dict, step_id: str, state: str) -> dict:
    if apply_journal.step(active, step_id)["status"] != "unknown":
        return active
    raise CliError("update 的副作用未知；版本相同不能证明完整恢复，需使用 recover 核验后置条件")


def _refresh_arch_receipts(args: argparse.Namespace, catalog: dict[str, Any]) -> None:
    updates = []
    for component_id, component in catalog["components"].items():
        package = component.get("package") if component else None
        if not isinstance(package, dict) or package.get("source") != "official":
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
        _write_lock(args)


def _finish_observed_paseo_apply(args: argparse.Namespace, value: dict[str, Any]) -> bool:
    service = value["components"].get("paseo-autostart", {})
    if (not service.get("selected") or service.get("action") != "keep"
            or service.get("unit_state") != "current"
            or service.get("runtime", {}).get("state") != "current"
            or service.get("paseo_config", {}).get("state") != "current"):
        return False
    active = apply_journal.read()
    if active is None or active["command"] != "apply":
        return False
    remaining = [step for step in active["steps"] if step["status"] != "verified"]
    if (len(remaining) != 1 or remaining[0]["component"] != "paseo-autostart"
            or remaining[0]["action"] != "service"
            or remaining[0]["status"] not in {"started", "unknown"}
            or not any(step["id"] == "paseo-config:configure" and step["status"] == "verified"
                       for step in active["steps"])):
        return False
    if (active["config_digest"] != apply_journal.digest(Path(value["config"]))
            or active["lock_digest"] != apply_journal.digest(args.component_lock_path)):
        raise CliError("配置或锁已改变；不能结束未完成的 Paseo apply，保留 journal")
    # Only acknowledge observed postconditions; never replay an unknown service action.
    apply_journal.verified(active, remaining[0]["id"], {
        "runtime": service["runtime"], "paseo_config": service["paseo_config"],
    })
    apply_journal.clear()
    return True


def _preflight_pending_owners(args: argparse.Namespace, value: dict, catalog: dict, steps: list[dict]) -> None:
    pending = {name: catalog["configuration_targets"][name] for name, item in value["connections"].items()
               if item.get("state") == "pending-owner"}
    for provider in {"cch-status" if name == "cch" else "pennix-skills" for name in pending}:
        step = next((item for item in steps if item["component"] == provider
                     and item["action"] in {"install", "upgrade", "adopt", "adopt-upgrade"}), None)
        if step is None:
            raise CliError(f"连接 owner {provider} 缺少可核验的安装候选")
        with tempfile.TemporaryDirectory(prefix="pennix-owner-preflight-") as directory:
            workspace = Path(directory)
            environment = {"HOME": directory, "PATH": os.environ["PATH"], "LANG": "C.UTF-8",
                           "CODEX_HOME": str(workspace / "codex"), "XDG_CONFIG_HOME": str(workspace / "config"),
                           "XDG_STATE_HOME": str(workspace / "state"), "XDG_CACHE_HOME": str(workspace / "cache"),
                           "XDG_DATA_HOME": str(workspace / "data"), "NPM_CONFIG_USERCONFIG": "/dev/null"}
            component = catalog["components"][provider]
            candidate = candidates.resolve(component, workspace, step["target"] or None)
            if provider == "pennix-skills":
                staged_component = {**component, "_candidate_target": candidates.lock_entry(candidate)}
                staging, manifest = skill_collections._stage(staged_component, workspace, candidate, environment=environment)
            else:
                result = subprocess.run(["npm", "install", "--global", "--prefix", str(workspace / "prefix"),
                                         "--ignore-scripts", "--no-audit", "--no-fund", candidate["path"]],
                                        capture_output=True, timeout=240, env=environment)
                if result.returncode:
                    raise CliError("CCH owner 候选暂存失败；未执行组件变更")
            for name, spec in pending.items():
                if ("cch-status" if name == "cch" else "pennix-skills") != provider:
                    continue
                if provider == "cch-status":
                    command = [str(workspace / "prefix/bin" / spec["command"][0])]
                elif spec["command"][0] in manifest["commands"]:
                    command = [str(staging / manifest["commands"][spec["command"][0]])]
                else:
                    command = [item.replace("$HOME/.agents/skills/pennix-skills", str(staging)) for item in spec["command"]]
                payload = connection_owners._payload(name, spec, args.kconf,
                    Path(config_model.config_value(args.kconf, "PENNIX_SECRETS_FILE")), "configure")
                connection_owners._run(command, "validate", payload, codex_home=args.codex_home)
            step["target"] = candidates.lock_entry(candidate)


def cmd_apply(args: argparse.Namespace) -> int:
    value = _plan(args)
    if _finish_observed_paseo_apply(args, value):
        _emit({"operation": "apply", "results": {"paseo-autostart": "verified-current"}}, args.json)
        return 0
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
        if item["action"] == "blocked"
    ]
    if blocked_connections:
        details = "; ".join(
            f"{target}: {value['connections'][target].get('detail', 'owner 预检失败')}"
            for target in sorted(blocked_connections)
        )
        raise CliError(f"连接预检失败，未执行任何动作：{details}")
    service = value["components"]["paseo-autostart"]
    paseo_needed = service["selected"] or service["unit_state"] in {"current", "migration-required"} or bool(relay and relay["selected"])
    if service["action"] == "blocked" and paseo_needed:
        raise CliError(f"Paseo 服务预检失败，未执行任何动作：{service.get('detail', '配置不一致')}")
    blocked = [key for key, item in value["components"].items()
               if item.get("action") == "blocked" and (key in selected or key in catalog["components"] or item.get("native_action") == "blocked")]
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
    if value["components"]["paseo-autostart"]["run"] and "paseo-cli" not in selected:
        raise CliError("Paseo daemon 运行需要选择 paseo-cli")
    steps = _apply_steps(value, args, catalog)
    active, resumed = _journal_for_apply(args, value, steps, create=False)
    if active is None:
        _preflight_pending_owners(args, value, catalog, steps)
    service = value["components"]["paseo-autostart"]
    if (active and service["selected"] and service["run"]
            and not any(item["id"] == "paseo-config:configure" for item in active["steps"])):
        raise CliError("未完成 apply journal 缺少 Paseo 配置步骤；保留现场，不能猜测续跑")
    if active and any(item.get("component") == "paseo-autostart" and item["status"] != "verified"
                      and item.get("target", {}).get("service_contract") != "autostart-v1" for item in active["steps"]):
        raise CliError("旧 Paseo journal 不符合仅自启合同；保留现场，不重放旧服务操作")
    paseo_home = Path(config_model.config_value(args.kconf, "PENNIX_PASEO_HOME"))
    prefix = Path.home() / ".local"
    paseo = prefix / "bin" / "paseo"
    if not paseo.is_file():
        paseo = Path(shutil.which("paseo") or paseo)
    if paseo_needed:
        try:
            if paseo.is_file():
                paseo_service.skills(paseo, paseo_home, "probe")
            paseo_service.preflight(
                paseo_home, service["listen"], service["mode"], service["run"],
                needs_systemd=service["mode"] != "off" or service["unit_state"] in {"current", "migration-required"},
            )
            if paseo.is_file() and service["runtime"]["state"] == "unknown":
                raise paseo_service.PaseoServiceError("Paseo daemon 运行态不可核验")
            restart_needed = bool(service["runtime"].get("running") and (
                service.get("paseo_config", {}).get("restart_required")
                or service["unit_state"] == "migration-required" and service["runtime"].get("active")
                or value["components"].get("paseo-cli", {}).get("action") in {"install", "upgrade", "adopt-upgrade"}
                or relay and relay.get("action") in {"configure", "install"}
            ))
            if restart_needed:
                paseo_service.assert_no_active_work(paseo, paseo_home)
        except paseo_service.PaseoServiceError as error:
            raise CliError(f"Paseo 只读预检失败；未执行组件变更：{error}") from error
    plan_steps = active["steps"] if active else steps
    paseo_step = next((item for item in plan_steps if item["component"] == "paseo-cli"
                       and item["action"] in {"install", "adopt", "upgrade", "adopt-upgrade"}
                       and (active is None or item["status"] != "verified")), None)
    if paseo_step is not None:
        component = catalog["components"]["paseo-cli"]
        target = paseo_step["target"] or None
        try:
            with tempfile.TemporaryDirectory(prefix="pennix-paseo-preflight-") as directory:
                candidate = candidates.resolve(component, Path(directory), target)
                candidate_target = candidates.lock_entry(candidate)
                paseo_service.preflight_candidate(candidate)
        except candidates.CandidateError as error:
            raise CliError(f"Paseo CLI 来源或摘要预检失败；未执行组件变更：{error}") from error
        if target and not candidates.matches(candidate_target, target):
            raise CliError("Paseo CLI 候选与 journal 精确目标不一致；未执行组件变更")
        if active:
            active = apply_journal.set_target(active, paseo_step["id"], candidate_target)
        else:
            paseo_step["target"] = candidate_target
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
        _verify_inputs(args)
        if hasattr(args, "config_snapshot") and _plan(args) != value:
            raise CliError("确认期间资源或归属已变化；请重新计划")
        active, _ = _journal_for_apply(args, value, steps, create=True)
    relay_home = paseo_home
    results: dict[str, str] = {}
    system_step = next((item for item in active["steps"] if item["id"] == "system-upgrade"), None)
    if system_step is not None and system_step["status"] == "started":
        raise CliError("pacman -Syu 的副作用状态未知；保留 journal，不能自动重试")
    if system_step is not None and system_step["status"] != "verified":
        def system_upgrade():
            result = subprocess.run(["sudo", "pacman", "-Syu", "--noconfirm"], check=False)
            if result.returncode:
                raise CliError(f"完整系统升级失败: {result.returncode}")
            owner_engine._candidate.cache_clear()
            _refresh_arch_receipts(args, catalog)
            return "changed"
        active, results["system-upgrade"] = _journal_mutate(active, "system-upgrade", system_upgrade)
    if system_step is not None:
        value = _plan(args)
        _, _, catalog = _load(args)
        relay = value["components"].get("paseo-relay")
        if any(item.get("action") in {"blocked", "repair"} or item.get("native_action") in {"blocked", "repair"}
               for item in value["components"].values()) or any(
                   item.get("action") == "blocked" for item in value["connections"].values()):
            raise CliError("系统升级后重新计划发现阻塞；保留已完成步骤，不执行旧计划")
        active = apply_journal.replan(active, _apply_steps(value, args, catalog))
    for step in active["steps"]:
        _verify_inputs(args)
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
                _write_lock(args)
                return "migrated"
            active, results[component_id] = _journal_mutate(active, step_id, migrate)
            continue
        if component_id == "paseo-config":
            def configure_paseo(step=step):
                return paseo_service.configure(
                    paseo, paseo_home, step["target"]["listen"]
                )
            active, results[component_id] = _journal_mutate(active, step_id, configure_paseo)
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
                    if target and not candidates.matches(candidate_target, target):
                        raise CliError(f"{component_id} 候选与 journal 精确目标不一致")
                    if step["target"] != candidate_target:
                        active = apply_journal.set_target(active, step_id, candidate_target)
                        target = candidate_target
                    component["_candidate_target"] = candidate_target
                    def install(candidate=candidate, component_id=component_id, component=component, target=target):
                        result = _run_component(args, catalog, component_id, operation, candidate=candidate)
                        args.component_lock = component_lock.set_entry(args.component_lock, component_id, target, args.component_lock_sources)
                        _write_lock(args)
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
                    _write_lock(args)
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
                        args.codex_home, Path(config_model.config_value(args.kconf, "PENNIX_SECRETS_FILE")), action,
                        operation_id=f"{active.get('transaction', 'legacy')}:{step_id}")
                except connection_owners.ConnectionError:
                    raise
                except secret_store.SecretError as error:
                    raise connection_owners.ConnectionError(f"连接 {target_name} 输入不可用", effect="none") from error
            active, results[f"connection:{target_name}"] = _journal_mutate(active, step_id, apply_connection)
            continue
        if action == "uninstall" and component_id in catalog["components"]:
            def uninstall(component_id=component_id):
                result = _run_component(args, catalog, component_id, "uninstall")
                if component_id in args.component_lock["components"]:
                    args.component_lock = component_lock.remove_entry(args.component_lock, component_id)
                    _write_lock(args)
                return result
            active, results[component_id] = _journal_mutate(active, step_id, uninstall)
            continue
        if component_id == "paseo-relay":
            def apply_relay():
                prefix = Path.home() / ".local"
                paseo = prefix / "bin" / "paseo"
                if not paseo.is_file():
                    paseo = Path(shutil.which("paseo") or paseo)
                if action == "uninstall":
                    if relay_owner.local_owned():
                        relay_owner.remove(relay_home,
                            nas_ssh=config_model.config_value(args.kconf, "PENNIX_RELAY_NAS_SSH"),
                            router_ssh=config_model.config_value(args.kconf, "PENNIX_RELAY_ROUTER_SSH"))
                    return relay_owner.unconfigure_daemon(paseo, relay_home)
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
            def apply_service():
                result = paseo_service.apply(mode, paseo, paseo_home,
                    value["components"]["paseo-autostart"]["listen"], value["components"]["paseo-autostart"]["run"],
                    restart_daemon=bool(step["target"].get("restart_daemon")))
                runtime_state = paseo_service.runtime(
                    paseo, paseo_home, mode, value["components"]["paseo-autostart"]["run"])
                if runtime_state["state"] != "current":
                    raise paseo_service.PaseoServiceError("Paseo 实际 daemon/enable/active/linger 后置条件未满足")
                if value["components"]["paseo-autostart"]["selected"]:
                    cli_version = args.component_lock["components"].get("paseo-cli", {}).get("version")
                    observed = paseo_service.observe(
                        paseo, paseo_home, value["components"]["paseo-autostart"]["listen"],
                        cli_version, runtime_state)
                    if observed["state"] != "current":
                        raise paseo_service.PaseoServiceError("Paseo 配置、Skills 同步或 CLI/daemon 版本后置条件未满足")
                return result
            active, results["paseo-autostart"] = _journal_mutate(active, step_id, apply_service)
            continue
        raise CliError(f"journal 步骤没有有效执行路由: {step_id}")
    apply_journal.clear()
    _emit({"operation": "apply", "results": results}, args.json)
    return 0


def _copilot_steps(args, catalog):
    return [{"id": f"connection:{name}:configure", "component": f"connection:{name}",
             "action": "configure", "target": {}}
            for name, spec in catalog.get("configuration_targets", {}).items()
            if spec.get("host") == "copilot-cli" and spec["component"] == args.component
            and config_model.config_value(args.kconf, spec["select"]) == "y"]


def _configure_copilot(args, catalog, active, *, repair=False):
    for step in _copilot_steps(args, catalog):
        name = step["component"].split(":", 1)[1]
        active, _ = _journal_mutate(active, step["id"], lambda name=name:
            connection_owners.apply_target(name, catalog["configuration_targets"][name], args.kconf,
                args.codex_home, Path(config_model.config_value(args.kconf, "PENNIX_SECRETS_FILE")), "configure", repair=repair,
                operation_id=f"{active['transaction']}:{step['id']}"))
    return active


def cmd_repair(args: argparse.Namespace) -> int:
    kconf, _, catalog = _load(args)
    args.kconf = kconf
    args.codex_home = Path(os.path.expanduser(config_model.config_value(kconf, "PENNIX_CODEX_HOME")))
    if args.component not in catalog["components"]:
        raise CliError(f"未知组件: {args.component}")
    operation = "upgrade"
    component = catalog["components"][args.component]
    if args.component not in config_model.selected_components(kconf):
        raise CliError(f"组件未在 workflow.config 中选择: {args.component}")
    active = apply_journal.read()
    if active is not None and (active["command"] != "repair" or active["component"] != args.component):
        raise CliError("存在其他未完成 lifecycle 事务；先从原入口或 recover 核验恢复")
    if args.expected_sha256 is not None and args.component != "codex-agents" and component.get("delivery") != "client":
        raise CliError("--expected-sha256 只用于审阅后的 codex-agents 或固定本地客户端升级")
    if component.get("delivery") in {"package", "plugin", "client", "collection"}:
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
        if component.get("native_config"):
            state, observed = owner_engine.probe_component(component, args.codex_home)
            status = {"state": state, "ownership": "lifecycle" if owner_engine.package_owned(args.component, component, observed, args.codex_home) else "external"}
        missing_owned = (status["state"] == "missing" and component.get("delivery") == "package"
                         and owner_engine._package_receipt(args.component, component) is not None
                         and owner_engine.package_absent(component))
        if status["state"] not in {"match", "outdated", "migration-required"} and not missing_owned:
            raise CliError(f"组件状态不允许锁定目标修复: {status['state']} ({status.get('detail', '')})")
        if component.get("delivery") == "package" and status.get("ownership") != "lifecycle" and not missing_owned:
            raise CliError("repair 需要可信 Lifecycle package receipt")
        if component.get("delivery") == "plugin" and not owner_engine._plugin_owned(args.component, component, args.codex_home):
            raise CliError("repair 需要可信 Lifecycle plugin receipt")
        if component.get("delivery") == "client" and owner_engine.local_clients.receipt(args.component, component) is None:
            raise CliError("repair 需要可信 Lifecycle client receipt")
        if component.get("delivery") == "collection" and not owner_engine.collection_owned(component, args.codex_home):
            raise CliError("repair 需要可信 Lifecycle collection receipt")
        if component.get("delivery") == "collection" and status["state"] == "migration-required":
            steps = [{"id": f"{args.component}:repair", "component": args.component,
                      "action": "upgrade", "target": entry}]
            active = _open_transaction(args, "repair", args.component, steps)
            def migrate_collection():
                result = skill_collections.migrate(component, args.codex_home)
                args.component_lock = component_lock.set_entry(args.component_lock, args.component, entry, args.component_lock_sources)
                _write_lock(args)
                return result
            active, result = _journal_mutate(active, steps[0]["id"], migrate_collection)
            apply_journal.clear()
            _emit({"operation": "repair", "component": args.component, "status": result}, args.json)
            return 0
        with tempfile.TemporaryDirectory(prefix="pennix-repair-") as directory:
            try:
                candidate = candidates.resolve(component, Path(directory), entry)
            except candidates.CandidateError as error:
                raise CliError(f"无法取得锁定修复候选: {error}") from error
            candidate_target = candidates.lock_entry(candidate)
            component["_candidate_target"] = candidate_target
            steps = [{"id": f"{args.component}:repair", "component": args.component,
                      "action": "upgrade", "target": candidate_target}]
            native = component.get("native_config")
            if native and config_model.config_value(kconf, native["select"]) == "y":
                steps.append({"id": f"{args.component}:configure", "component": args.component,
                              "action": "configure", "target": {}})
            steps.extend(_copilot_steps(args, catalog))
            active = _open_transaction(args, "repair", args.component, steps)
            if apply_journal.step(active, steps[0]["id"])["target"] != candidate_target:
                raise CliError("修复候选与已记录的精确目标不同")
            def repair_component():
                result = _run_component(args, catalog, args.component, operation, candidate=candidate)
                args.component_lock = component_lock.set_entry(args.component_lock, args.component, candidate_target, args.component_lock_sources)
                _write_lock(args)
                return result
            active, result = _journal_mutate(active, steps[0]["id"], repair_component)
    else:
        active = _open_transaction(args, "repair", args.component,
            [{"id": f"{args.component}:repair", "component": args.component, "action": "upgrade", "target": {}}])
        active, result = _journal_mutate(active, f"{args.component}:repair",
            lambda: _run_component(args, catalog, args.component, operation))
    native = component.get("native_config")
    if native and config_model.config_value(kconf, native["select"]) == "y":
        active, configured = _journal_mutate(active, f"{args.component}:configure",
            lambda: _run_component(args, catalog, args.component, "configure", repair=True))
        result = f"{result}; native {configured}"
    active = _configure_copilot(args, catalog, active, repair=True)
    apply_journal.clear()
    _emit({"operation": "repair", "component": args.component, "status": result}, args.json)
    return 0


def cmd_reconcile(args: argparse.Namespace) -> int:
    kconf, config_path, catalog = _load(args)
    args.kconf = kconf
    args.codex_home = Path(os.path.expanduser(config_model.config_value(kconf, "PENNIX_CODEX_HOME")))
    component = catalog["components"].get(args.component)
    if component is None or args.component not in config_model.selected_components(kconf):
        raise CliError("reconcile 仅接受已选择的登记组件")
    plugin = component.get("delivery") == "plugin"
    if not plugin and (component.get("delivery") != "package" or component.get("package", {}).get("source") not in {"official", "aur"}):
        raise CliError("reconcile 仅支持 Arch/AUR 包和 Codex plugin")
    receipt_path = (owner_engine._plugin_receipt_path(args.component) if plugin
                    else owner_engine._package_receipt_path(args.component))
    active = apply_journal.read()
    config_digest = apply_journal.digest(config_path)
    lock_digest = apply_journal.digest(args.component_lock_path)
    step_id = f"{args.component}:reconcile"
    if active:
        if (active["command"] != "reconcile" or active["component"] != args.component
                or active["config_digest"] != config_digest or len(active["steps"]) != 1
                or active["steps"][0]["id"] != step_id):
            raise CliError("存在其他或输入已改变的 lifecycle journal；保留现场")
        saved = active["steps"][0]["target"]
    else:
        old = args.component_lock["components"].get(args.component)
        if old is None:
            raise CliError("reconcile 需要既有可信 lock，不能接管未知安装")
        try:
            target, identity, receipt = owner_engine.reconcile_candidate(args.component, component, args.codex_home, old)
        except owner_engine.BootstrapError as error:
            raise CliError(str(error)) from error
        after_receipt = ({"schema": 1, "component": args.component, "id": component["plugin"]["id"],
                          "target": owned_files.target_identity(), **target}
                         if plugin else {**receipt, "version": identity["probe_version"]})
        next_lock = component_lock.set_entry(args.component_lock, args.component, target, args.component_lock_sources)
        saved = {"entry": target, "identity": identity,
                 "receipt_before": apply_journal.digest(receipt_path), "receipt_after": after_receipt,
                 "lock_after": hashlib.sha256(owned_files.json_bytes(next_lock)).hexdigest()}
        _emit({"operation": "reconcile", "component": args.component, "old_lock": old,
               "old_receipt": receipt, "candidate": target, "identity": identity,
               "action": "align-native-marketplace-and-record" if plugin else "accept-installed-identity-only"}, args.json)
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise CliError("pennix reconcile 需要交互式终端以确认精确候选")
        if input("确认收敛此精确已安装身份？输入 yes 继续: ").strip() != "yes":
            return 1
        if (apply_journal.digest(config_path) != config_digest
                or apply_journal.digest(args.component_lock_path) != lock_digest
                or apply_journal.digest(receipt_path) != saved["receipt_before"]):
            raise CliError("确认期间配置、锁或 receipt 已变化；未执行收敛")
        try:
            fresh, fresh_identity, _ = owner_engine.reconcile_candidate(args.component, component, args.codex_home, old)
        except owner_engine.BootstrapError as error:
            raise CliError(str(error)) from error
        if fresh != target or fresh_identity != identity:
            raise CliError("确认期间实际候选已变化；未执行收敛")
        active = _start_transaction(args, "reconcile", args.component,
                                     [{"id": step_id, "component": args.component, "action": "reconcile", "target": saved}])
    target = saved["entry"]
    after_bytes = owned_files.json_bytes(saved["receipt_after"])
    after_digest = hashlib.sha256(after_bytes).hexdigest()
    if (apply_journal.digest(args.component_lock_path) not in {active["lock_digest"], saved["lock_after"]}
            or apply_journal.digest(receipt_path) not in {saved["receipt_before"], after_digest}):
        raise CliError("reconcile journal 的锁或 receipt 已漂移；保留现场")

    def verify_identity():
        try:
            if plugin:
                actual = owner_engine._plugin_snapshot(component, args.codex_home, installed_only=True)
            else:
                actual = owner_engine.package_identity(component)
                actual["probe_version"] = owner_engine._version(component, actual["command"])
        except owner_engine.BootstrapError as error:
            raise CliError(str(error)) from error
        if actual != saved["identity"]:
            raise CliError("reconcile 实际身份与已确认候选不符；保留 journal")

    verify_identity()
    step = apply_journal.step(active, step_id)
    if step["status"] in {"started", "unknown"}:
        if plugin and owner_engine._plugin_snapshot(component, args.codex_home) != target:
            raise CliError("原生 plugin 收敛副作用未知；先核验 Marketplace，再从相同入口恢复")
        if step["status"] == "started":
            active = apply_journal.unknown(active, step_id)
        active = apply_journal.retry(active, step_id)
    if step["status"] == "verified":
        if apply_journal.digest(args.component_lock_path) != saved["lock_after"] or apply_journal.digest(receipt_path) != after_digest:
            raise CliError("reconcile 已验证步骤的后置条件改变；保留 journal")
    else:
        def accept_identity():
            verify_identity()
            if apply_journal.digest(config_path) != config_digest:
                raise CliError("reconcile 配置已变化；保留 journal")
            if plugin:
                owner_engine.align_plugin(component, args.codex_home, target)
            verify_identity()
            receipt_raw = owned_files.read(receipt_path, private=True)
            lock_raw = owned_files.read(args.component_lock_path, private=True)
            if (receipt_raw is None or lock_raw is None
                    or hashlib.sha256(receipt_raw).hexdigest() not in {saved["receipt_before"], after_digest}
                    or hashlib.sha256(lock_raw).hexdigest() not in {active["lock_digest"], saved["lock_after"]}
                    or apply_journal.digest(config_path) != config_digest):
                raise CliError("reconcile 提交前输入已变化；保留 journal")
            owned_files.replace(receipt_path, after_bytes, receipt_raw)
            updated = component_lock.set_entry(args.component_lock, args.component, target, args.component_lock_sources)
            if hashlib.sha256(owned_files.json_bytes(updated)).hexdigest() != saved["lock_after"]:
                raise CliError("reconcile lock 后置内容不符；保留 journal")
            owned_files.replace(args.component_lock_path, owned_files.json_bytes(updated), lock_raw)
            apply_journal.checkpoint_lock(saved["lock_after"])
            verify_identity()
            return "reconciled"
        try:
            active, _ = _journal_mutate(active, step_id, accept_identity)
        except owner_engine.BootstrapError as error:
            raise CliError(str(error)) from error
    apply_journal.clear()
    _emit({"operation": "reconcile", "component": args.component, "status": "reconciled", "target": target}, args.json)
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
    version = getattr(args, "version", None)
    if version is not None:
        if component.get("package", {}).get("source") != "npm":
            raise CliError("--version 仅支持 npm 组件")
        if not re.fullmatch(
                r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
                r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
                r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?", version):
            raise CliError("--version 必须是精确 npm 版本，不能使用 tag 或范围")
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
            upgrade = apply_journal.step(active, f"{args.component}:upgrade")
            if (upgrade["status"] != "verified"
                    or args.component_lock["components"].get(args.component) != upgrade["target"]):
                raise CliError("components.lock.json 已改变；未完成 update 保留现场，不能猜测续跑")
        elif any(item["status"] == "started" and item["id"] != "paseo-daemon:restart"
                 for item in active["steps"]):
            raise CliError("update 步骤仍处于 started；保留 journal，不能自动重试")
    if active and version is not None and active["steps"][0]["target"].get("version") != version:
        raise CliError("--version 与未完成 update 已确认目标不一致；保留 journal")
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
        if apply_journal.step(active, step_id)["status"] != "verified":
            active = _retry_unknown_update(active, step_id, status["state"])
    if active is None and (not sys.stdin.isatty() or not sys.stdout.isatty()):
        raise CliError("pennix update 需要交互式终端")
    with tempfile.TemporaryDirectory(prefix="pennix-update-") as directory:
        try:
            candidate = candidates.resolve(component, Path(directory),
                                           active["steps"][0]["target"] if active else
                                           ({"version": version} if version is not None else None))
        except candidates.CandidateError as error:
            raise CliError(str(error)) from error
        target = candidates.lock_entry(candidate)
        if args.component == "paseo-cli":
            paseo_service.preflight_candidate(candidate)
        if active and target != active["steps"][0]["target"]:
            raise CliError("恢复候选与 journal 中已确认目标不一致")
        upgrade_step_id = f"{args.component}:upgrade"
        restart_step_id = "paseo-daemon:restart"
        already_current = all(entry.get(key) == target.get(key) for key in ("version", "ref", "sha256"))
        if already_current and active is None:
            _emit({"operation": "update", "component": args.component, "status": "no-op", "target": entry}, args.json)
            return 0
        restart_state = None
        if args.component == "paseo-cli":
            paseo_home = Path(config_model.config_value(kconf, "PENNIX_PASEO_HOME"))
            mode = config_model.choice_value(kconf, "PENNIX_PASEO_AUTOSTART_")
            run = config_model.config_value(kconf, "PENNIX_PASEO_RUN") == "y"
            paseo = Path(shutil.which("paseo") or Path.home() / ".local/bin/paseo")
            restart_state = paseo_service.runtime(paseo, paseo_home, mode, run)
            if restart_state["state"] == "unknown":
                raise CliError("Paseo daemon 运行态不可核验；未执行 CLI 更新")
            needs_restart = bool(restart_state["running"] and restart_state["daemon_version"] != target.get("version"))
            if needs_restart:
                try:
                    paseo_service.preflight(paseo_home, config_model.config_value(kconf, "PENNIX_PASEO_LISTEN"),
                                            mode, run, needs_systemd=mode != "off")
                    paseo_service.assert_no_active_work(paseo, paseo_home)
                except paseo_service.PaseoServiceError as error:
                    raise CliError(f"Paseo CLI 更新预检失败；未执行更新：{error}") from error
        has_restart_step = bool(args.component == "paseo-cli" and (
            any(item["id"] == restart_step_id for item in active["steps"])
            if active else restart_state["running"] and restart_state["daemon_version"] != target.get("version")))
        restart_pending = has_restart_step
        if (active is not None and apply_journal.step(active, upgrade_step_id)["status"] == "verified"
                and restart_pending):
            restart_step = apply_journal.step(active, restart_step_id)
            cli_version = args.component_lock["components"][args.component].get("version")
            if restart_state["running"] and restart_state["daemon_version"] == cli_version:
                active = apply_journal.verified(active, restart_step_id, {"daemon_version": cli_version, "running": True})
                if apply_journal.complete(active):
                    apply_journal.clear()
                    _emit({"operation": "update", "component": args.component,
                           "status": "recovered", "target": args.component_lock["components"][args.component]}, args.json)
                    return 0
            elif not restart_state["running"] and not restart_step["target"].get("was_running"):
                active = apply_journal.verified(active, restart_step_id, {"running": False})
                if apply_journal.complete(active):
                    apply_journal.clear()
                    _emit({"operation": "update", "component": args.component,
                           "status": "recovered", "target": args.component_lock["components"][args.component]}, args.json)
                    return 0
            elif restart_step["status"] == "unknown":
                raise CliError("Paseo restart 副作用未知；保留现场，不能自动重放")
            elif restart_step["status"] == "started":
                raise CliError("Paseo daemon restart 状态不确定且版本仍不一致；保留 journal，需人工核验")
            elif restart_step["status"] == "verified":
                raise CliError("Paseo daemon restart 回执与当前版本不一致；保留 journal，需人工核验")
        if already_current and not (active is not None and restart_pending
                                    and apply_journal.step(active, restart_step_id)["status"] != "verified"):
            _emit({"operation": "update", "component": args.component, "status": "no-op", "target": entry}, args.json)
            return 0
        component["_candidate_target"] = target
        _emit({"operation": "update", "component": args.component, "current": entry,
               "candidate": target, "impact": candidate.get("impact", "仅替换该组件；owner 安装器影响需按计划核验")}, args.json)
        if active is None:
            if input("确认仅更新该组件？输入 yes 继续: ").strip() != "yes":
                return 1
            _verify_inputs(args)
            config_digest = apply_journal.digest(config_path)
            lock_digest = apply_journal.digest(args.component_lock_path)
            steps = [{"id": upgrade_step_id, "component": args.component,
                      "action": "upgrade", "target": target}]
            steps.extend(_copilot_steps(args, catalog))
            if has_restart_step:
                steps.append({"id": restart_step_id, "component": "paseo-daemon",
                              "action": "restart", "target": {"mode": mode,
                                                                      "was_running": bool(restart_state["running"])}})
            active = _start_transaction(args, "update", args.component, steps)
        step_id = f"{args.component}:upgrade"
        def update_component():
            result = _run_component(args, catalog, args.component, "upgrade", candidate=candidate)
            args.component_lock = component_lock.set_entry(args.component_lock, args.component, target, args.component_lock_sources)
            _write_lock(args)
            return result
        active, result = _journal_mutate(active, step_id, update_component)
        active = _configure_copilot(args, catalog, active)
        if restart_pending:
            def restart_paseo():
                current = paseo_service.runtime(paseo, paseo_home, mode, run)
                if current["state"] == "unknown":
                    raise paseo_service.PaseoServiceError("Paseo daemon 运行态不可核验")
                if not current["running"]:
                    return "not-running"
                state = paseo_service.restart(paseo, paseo_home, mode)
                if state.get("daemon_version") != args.component_lock["components"][args.component].get("version"):
                    raise paseo_service.PaseoServiceError("Paseo CLI 与 daemon 版本不一致")
                return "restarted"
            active, results = _journal_mutate(active, restart_step_id, restart_paseo)
    apply_journal.clear()
    _emit({"operation": "update", "component": args.component, "status": result, "target": target}, args.json)
    return 0


def _reverse_uninstall_order(catalog: dict[str, Any]) -> list[str]:
    return [key for key in reversed(APPLY_ORDER)
            if key in catalog["components"]]


def cmd_uninstall(args: argparse.Namespace) -> int:
    kconf, _, catalog = _load(args)
    args.kconf = kconf
    args.codex_home = Path(os.path.expanduser(config_model.config_value(kconf, "PENNIX_CODEX_HOME")))
    if args.component == "paseo-relay":
        if args.confirm != args.component:
            raise CliError("relay 卸载必须使用 --confirm paseo-relay")
        active = _open_transaction(args, "uninstall", args.component,
            [{"id": "paseo-relay:uninstall", "component": "paseo-relay", "action": "uninstall", "target": {}}])
        try:
            def withdraw_relay():
                home = Path(config_model.config_value(kconf, "PENNIX_PASEO_HOME"))
                if relay_owner.local_owned():
                    relay_owner.remove(home,
                        nas_ssh=config_model.config_value(kconf, "PENNIX_RELAY_NAS_SSH"),
                        router_ssh=config_model.config_value(kconf, "PENNIX_RELAY_ROUTER_SSH"))
                paseo = Path(shutil.which("paseo") or Path.home() / ".local/bin/paseo")
                return relay_owner.unconfigure_daemon(paseo, home)
            active, result = _journal_mutate(active, "paseo-relay:uninstall", withdraw_relay)
        except relay_owner.RelayError as error:
            raise CliError(f"Paseo relay 撤回失败：{error}") from error
        apply_journal.clear()
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
                if not owner_engine.package_owned(key, component, codex_home=args.codex_home) and not owner_engine.package_absent(component):
                    raise CliError(f"全量卸载预检发现包归属漂移: {key}")
            elif delivery == "client":
                if owner_engine.local_clients.receipt(key, component) is None:
                    continue
                if owner_engine.local_clients.inspect(key, component)[0] not in {"match", "outdated"}:
                    raise CliError(f"全量卸载预检发现客户端漂移: {key}")
            elif delivery == "collection" and skill_collections.receipt_owned(component, args.codex_home) != "owned":
                if status["state"] in {"drifted", "blocked"}:
                    raise CliError(f"全量卸载预检发现集合漂移: {key}")
                continue
            elif delivery == "plugin":
                if owner_engine._plugin_receipt(key, component) is None:
                    continue
                if not (owner_engine._plugin_owned(key, component, args.codex_home)
                        or owner_engine.plugin_absent(key, component, args.codex_home)):
                    raise CliError(f"全量卸载预检发现 plugin 归属漂移: {key}")
            if status["state"] in {"drifted", "blocked"}:
                raise CliError(f"全量卸载预检发现配置漂移: {key}")
            targets.append(key)
        owned_connections = []
        for target, spec in catalog["configuration_targets"].items():
            status = connection_owners.inspect_target(target, spec, args.codex_home, enabled=False,
                target_home=Path(config_model.config_value(kconf, "PENNIX_COPILOT_HOME")) if spec.get("host") == "copilot-cli" else None)
            if spec.get("host") == "copilot-cli" and status["state"] == "blocked":
                raise CliError(f"全量卸载无法核验 Copilot 连接归属: {target}")
            if status.get("owned"):
                if status["state"] not in {"current", "orphaned"}:
                    raise CliError(f"全量卸载预检发现连接漂移: {target}")
                connection_owners.validate_target(target, spec, kconf, args.codex_home,
                    Path(config_model.config_value(kconf, "PENNIX_SECRETS_FILE")), "unconfigure")
                owned_connections.append(target)
        relay = snapshot["components"].get("paseo-relay")
        remove_relay = relay_owner.local_owned() or relay_owner.daemon_owned(Path(config_model.config_value(kconf, "PENNIX_PASEO_HOME")))
        if not remove_relay and relay and relay["selected"] and relay["state"] == "blocked":
            raise CliError(f"relay 远端撤回预检失败，未执行任何本地卸载：{relay.get('detail', '未知错误')}")
    else:
        if not args.component or args.confirm != args.component:
            raise CliError("单组件卸载必须使用 --confirm <component-id>")
        targets = [args.component]
        selected = config_model.selected_components(kconf)
        dependents = {key for key in selected if args.component in DEPENDENCIES.get(key, set())}
        dependents.update(f"connection:{name}" for name, spec in catalog["configuration_targets"].items()
                          if spec["component"] == args.component and config_model.config_value(kconf, spec["select"]) == "y")
        if args.component == "paseo-cli" and (
                config_model.choice_value(kconf, "PENNIX_PASEO_AUTOSTART_") != "off"
                or config_model.config_value(kconf, "PENNIX_PASEO_RUN") == "y"
                or config_model.config_value(kconf, "PENNIX_PASEO_RELAY") == "y"):
            dependents.add("paseo-autostart/relay")
        if dependents:
            raise CliError(f"卸载 {args.component} 会破坏已选择依赖；请先修改配置并 apply")
        if args.component not in catalog["components"]:
            raise CliError(f"未知组件: {args.component}")
        component = catalog["components"][args.component]
        if component["delivery"] == "package":
            if not (owner_engine.package_absent(component) or
                    owner_engine.package_owned(args.component, component, codex_home=args.codex_home)):
                native = component.get("native_config")
                if native and native_integrations.inspect(native["owner"], args.codex_home, enabled=False).get("owned"):
                    targets = []
                else:
                    raise CliError("卸载预检无法证明包归属；未修改本地配置")
        elif component["delivery"] == "plugin" and not (owner_engine._plugin_owned(args.component, component, args.codex_home)
                or owner_engine.plugin_absent(args.component, component, args.codex_home)):
            raise CliError("卸载预检无法证明 plugin 归属")
    paseo = Path(shutil.which("paseo") or Path.home() / ".local/bin/paseo")
    paseo_home = Path(config_model.config_value(kconf, "PENNIX_PASEO_HOME"))
    service_state = paseo_service.inspect(paseo, paseo_home) if args.all else "absent"
    if service_state not in {"absent", "current", "migration-required"}:
        raise CliError("全量卸载预检发现 Paseo unit 漂移；未撤回任何连接")
    if args.all and service_state != "absent":
        paseo_service.preflight(paseo_home, "127.0.0.1:1", "off", False, needs_systemd=True)
    native_targets = []
    for key in (list(catalog["components"]) if args.all else [args.component]):
        native = catalog["components"][key].get("native_config")
        if native:
            state = native_integrations.inspect(native["owner"], args.codex_home, enabled=False)
            if state["state"] in {"blocked", "drifted"}:
                raise CliError(f"native 配置卸载预检失败: {key}")
            if state.get("owned"):
                native_targets.append(key)
    steps = []
    if args.all and remove_relay:
        steps.append({"id": "paseo-relay:uninstall", "component": "paseo-relay", "action": "uninstall", "target": {}})
    if args.all:
        steps += [{"id": f"connection:{target}:unconfigure", "component": f"connection:{target}",
                   "action": "unconfigure", "target": {}} for target in owned_connections]
    if args.all and service_state != "absent":
        steps.append({"id": "paseo-autostart:unconfigure", "component": "paseo-autostart", "action": "unconfigure", "target": {}})
    steps += [{"id": f"{key}:unconfigure", "component": key, "action": "unconfigure", "target": {}} for key in native_targets]
    steps += [{"id": f"{key}:uninstall", "component": key, "action": "uninstall", "target": {}} for key in targets]
    if not steps and apply_journal.read() is None:
        _emit({"operation": "uninstall", "config_unchanged": True, "results": {}}, args.json)
        return 0
    active = _open_transaction(args, "uninstall", None if args.all else args.component, steps)
    results: dict[str, str] = {}
    for step in active["steps"]:
        _verify_inputs(args)
        key = step["component"]
        def withdraw(key=key, action=step["action"]):
            if key == "paseo-relay":
                if relay_owner.local_owned():
                    relay_owner.remove(paseo_home,
                        nas_ssh=config_model.config_value(kconf, "PENNIX_RELAY_NAS_SSH"),
                        router_ssh=config_model.config_value(kconf, "PENNIX_RELAY_ROUTER_SSH"))
                return relay_owner.unconfigure_daemon(paseo, paseo_home)
            if key.startswith("connection:"):
                target = key.split(":", 1)[1]
                return connection_owners.apply_target(target, catalog["configuration_targets"][target],
                    kconf, args.codex_home, Path(config_model.config_value(kconf, "PENNIX_SECRETS_FILE")), "unconfigure",
                    operation_id=f"{active.get('transaction', 'legacy')}:{step['id']}")
            if key == "paseo-autostart":
                return paseo_service.apply("off", paseo, paseo_home, "127.0.0.1:1", False)
            result = _run_component(args, catalog, key, action)
            if action == "uninstall" and key in args.component_lock["components"]:
                args.component_lock = component_lock.remove_entry(args.component_lock, key)
                _write_lock(args)
            return result
        active, results[key] = _journal_mutate(active, step["id"], withdraw)
    apply_journal.clear()
    shared = [key for key in targets if catalog["components"][key]["delivery"] == "collection"]
    _emit({"operation": "uninstall", "config_unchanged": True, "results": results,
           **({"shared_scope": "同一用户的全部宿主配置", "shared_collections": shared} if shared else {})}, args.json)
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
    cache = owned_files.xdg_home("XDG_CACHE_HOME", Path.home() / ".cache") / "pennix-lifecycle"
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
        result = release_owner._uninstall()
    _emit({"tool": result, "shared_path": "preserved for retained tools"}, args.json)
    return 0


def cmd_recover(args: argparse.Namespace) -> int:
    active = apply_journal.read()
    if not args.resolve:
        _emit({"transaction": active, "state": "pending" if active else "idle"}, args.json)
        return 0
    if not active or active.get("schema") != 2 or active.get("transaction") != args.transaction:
        raise CliError("恢复必须匹配当前可识别的事务；旧 journal 保留现场")
    if not args.step:
        raise CliError("恢复必须使用 --step 指定同一事务中的步骤")
    kconf, config, catalog = _load(args)
    home = Path(config_model.config_value(kconf, "PENNIX_CODEX_HOME"))
    expected = {"home": str(Path.home()), "codex_home": str(home), "state_root": str(owned_files.state_root())}
    if active["identity"] != expected or active["config_digest"] != apply_journal.digest(config):
        raise CliError("恢复目标或配置与原事务不同；保留现场")
    if active["lock_digest"] != apply_journal.digest(args.component_lock_path):
        raise CliError("锁文件与事务最后已证实的检查点不同；保留现场，不推断外部修改归属")
    step = apply_journal.step(active, args.step)
    if args.resolve in {"not-started", "restored"}:
        required = "none" if args.resolve == "not-started" else "restored"
        if step["component"].startswith("connection:") and args.resolve == "restored":
            target = step["component"].split(":", 1)[1]
            spec = catalog["configuration_targets"].get(target, {})
            if spec.get("host") == "copilot-cli":
                if step["status"] == "verified":
                    raise CliError("已核验步骤不能按失败恢复撤回")
                if step["action"] != "configure":
                    raise CliError("Copilot 撤回事务只能核验完成；不能推断重新安装授权")
                effect = native_integrations.copilot.recover(spec["native"],
                    Path(config_model.config_value(kconf, "PENNIX_COPILOT_HOME")),
                    operation_id=f"{active['transaction']}:{step['id']}")
                if effect in {"none", "restored"}:
                    active = apply_journal.rejected(active, step["id"], "restored")
                    step = apply_journal.step(active, args.step)
        if step["status"] != "pending" or (step.get("observed") or {}).get("effect") != required:
            if not (args.resolve == "not-started" and step["status"] == "pending" and step["observed"] is None):
                raise CliError("没有绑定此步骤的无副作用或恢复前值证据；不能释放 unknown")
        active = apply_journal.verified(active, step["id"], {"resolution": args.resolve, "desired_applied": False})
    else:
        component = catalog["components"].get(step["component"])
        if step["status"] == "verified":
            pass
        elif step["component"].startswith("connection:"):
            target = step["component"].split(":", 1)[1]
            spec = catalog["configuration_targets"].get(target)
            if spec is None:
                raise CliError("恢复目标未登记")
            if spec.get("host") == "copilot-cli" and step["action"] == "configure":
                native_integrations.copilot.complete(spec["native"],
                    Path(config_model.config_value(kconf, "PENNIX_COPILOT_HOME")),
                    operation_id=f"{active['transaction']}:{step['id']}")
            if spec.get("host") == "copilot-cli" and step["action"] == "unconfigure":
                native_integrations.copilot.unconfigure(spec["native"],
                    Path(config_model.config_value(kconf, "PENNIX_COPILOT_HOME")), recovery=True,
                    operation_id=f"{active['transaction']}:{step['id']}")
            values, secrets = connection_owners._inputs(target, spec, kconf,
                Path(config_model.config_value(kconf, "PENNIX_SECRETS_FILE"))) if step["action"] == "configure" else ({}, {})
            desired = connection_owners._desired_digest(target, values, secrets) if step["action"] == "configure" else None
            state = connection_owners.inspect_target(target, spec, home, desired, values.get("credential_mode"),
                target_home=Path(config_model.config_value(kconf, "PENNIX_COPILOT_HOME")) if spec.get("host") == "copilot-cli" else None)
            if (step["action"] == "configure" and (state["state"] != "current" or not state["owned"])
                    or step["action"] == "unconfigure" and state["state"] not in {"missing", "absent"}):
                raise CliError("owner 未证明步骤后置条件；保留 unknown")
            if target == "siyuan":
                mcp = native_integrations.inspect("siyuan", home, enabled=step["action"] == "configure")
                if mcp["state"] != ("current" if step["action"] == "configure" else "absent"):
                    raise CliError("SiYuan MCP 后置条件未证明")
            active = apply_journal.verified(active, step["id"], {"state": state["state"], "recovered": True})
        elif component and step["action"] in {"install", "upgrade", "adopt", "adopt-upgrade"} and step["target"]:
            component["_lock_target"] = step["target"]
            args.kconf, args.codex_home = kconf, home
            status = _component_status(args, step["component"], component)
            if component.get("native_config"):
                state, observed = owner_engine.probe_component(component, home)
                owned = owner_engine.package_owned(step["component"], component, observed, home)
            else:
                state, owned = status["state"], status.get("ownership") == "lifecycle"
                if component["delivery"] == "plugin":
                    owned = owner_engine._plugin_owned(step["component"], component, home)
                elif component["delivery"] == "collection":
                    if skill_collections._migration_receipt(component, home) is not None:
                        skill_collections.migrate(component, home)
                        state = skill_collections.inspect(component, home)[0]
                    owned = skill_collections.owned(component, home)
                elif component["delivery"] == "client":
                    owned = owner_engine.local_clients.receipt(step["component"], component) is not None
            if (state == "match" and not owned and step["action"] == "upgrade"
                    and component.get("package", {}).get("source") == "npm"):
                with tempfile.TemporaryDirectory(prefix="pennix-recover-npm-") as directory:
                    candidate = candidates.resolve(component, Path(directory), step["target"])
                    old = args.component_lock["components"].get(step["component"], {})
                    owner_engine.recover_npm_receipt(step["component"], component, old, Path(candidate["path"]))
                owned = owner_engine.package_owned(step["component"], component, codex_home=home)
            if state not in {"match", "current"} or not owned:
                raise CliError("组件身份、归属或精确目标未证明；保留 unknown")
            args.component_lock = component_lock.set_entry(args.component_lock, step["component"], step["target"], args.component_lock_sources)
            _write_lock(args)
            active = apply_journal.read()
            active = apply_journal.verified(active, step["id"], {"recovered": True, "state": state})
        else:
            raise CliError("此步骤缺少足够的原生恢复证据；保留 unknown，不能强制清除")
    if apply_journal.complete(active):
        apply_journal.clear()
    _emit({"operation": "recover", "transaction": args.transaction, "step": args.step,
           "resolution": args.resolve, "complete": apply_journal.complete(active)}, args.json)
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pennix", description="Pennix 工作流生命周期工具")
    sub = p.add_subparsers(dest="command", required=True)
    recover = sub.add_parser("recover", help="只读展示或按 owner 证据恢复当前事务")
    recover.add_argument("--transaction")
    recover.add_argument("--step")
    recover.add_argument("--resolve", choices=["not-started", "completed", "restored"])
    recover.add_argument("--config")
    recover.add_argument("--json", action="store_true")
    recover.set_defaults(function=cmd_recover)

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
    update.add_argument("--version", help="指定 npm 组件的精确版本；默认仍跟踪登记通道")
    update.add_argument("--config")
    update.add_argument("--json", action="store_true")
    update.set_defaults(function=cmd_update)

    reconcile = sub.add_parser("reconcile", help="显式接受已核验的精确外部更新；不安装或降级")
    reconcile.add_argument("component")
    reconcile.add_argument("--config")
    reconcile.add_argument("--json", action="store_true")
    reconcile.set_defaults(function=cmd_reconcile)

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
            args.command in {"apply", "repair", "update", "reconcile", "uninstall", "tool"}
            or args.command == "secrets" and args.secrets_command != "codex-token"
            or args.command == "recover" and args.resolve is not None
            or (args.command == "config" and args.config_command in {"init", "edit"})
            or (args.command == "cache" and args.cache_command == "clean" and args.yes)
        )
        if mutating:
            with owned_files.locked():
                if args.command in {"apply", "repair", "update", "reconcile", "uninstall"} or args.command == "tool" and args.tool_command == "install":
                    current_host = owner_engine.host.detect_host()
                    if not current_host["supported"]:
                        raise CliError(str(current_host["reason"]))
                if args.command not in {"apply", "repair", "uninstall", "update", "reconcile", "recover"} and apply_journal.read() is not None:
                    raise CliError("存在未完成 lifecycle 事务；先从原入口或 recover 恢复")
                return args.function(args)
        return args.function(args)
    except (CliError, candidates.CandidateError, apply_journal.JournalError, connection_owners.ConnectionError, config_model.ConfigError, codex_config.CodexConfigError, path_env.PathEnvError, paseo_service.PaseoServiceError, relay_owner.RelayError, release_owner.ReleaseError, skill_collections.CollectionError, owner_engine.local_clients.ClientError, secret_store.SecretError, owned_files.OwnershipError, native_integrations.NativeIntegrationError, native_integrations.hook_integrations.HookError, OSError) as error:
        print(f"pennix: {error}", file=sys.stderr)
        try:
            active = apply_journal.read()
            if active:
                verified = [step["id"] for step in active["steps"] if step["status"] == "verified"]
                print(f"已核验步骤: {', '.join(verified) or '无'}；事务仍保留。使用 pennix recover --json 核对恢复现场。", file=sys.stderr)
        except (apply_journal.JournalError, owned_files.OwnershipError, OSError):
            pass
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
