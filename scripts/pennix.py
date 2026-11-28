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
from urllib.parse import urlsplit

import _engine as owner_engine
import apply_journal
import arch_recovery
import aur_build
import codex_config
import component_lock
import candidates
import connection_owners
import config_model
import credential_import
import path_env
import paseo_service
import paseo_plugins
import copilot_takeover
import relay_owner
import release_owner
import skill_collections
import base_environment
import pnpm_migration
import ponytail
import owned_files
import secret_store
import native_integrations
import update_inventory
import system_devel_tools


ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "references" / "catalog.json"
SCOPED_COLLECTION = "pennix-system-devel-skills"
TOOLS_NEXT = f"先运行 pennix plan --scope {system_devel_tools.SCOPE} 核对工具；缺包使用同作用域 apply"
APPLY_ORDER = [
    "codex-cli", "copilot-cli", "trellis-cli", "pnpm", "context-mode", "codegraph-cli", "paseo-cli", "trellis-paseo-adapter",
    "ponytail-plugin", "cch-status", "tmux", "tmux-config", "aoe", "uv", "yazi",
    "markitdown", "pandoc", "libreoffice", "poppler", "tesseract-eng", "tesseract-chi-sim", "tesseract", "noto-fonts-cjk",
    "codex-config", "codex-agents", "pennix-skills-extra",
    "pennix-skills", SCOPED_COLLECTION,
    "base-environment",
]
DEPENDENCIES = {
    "base-environment": base_environment.SHARED,
    "tmux-config": {"tmux"}, "ponytail-plugin": {"codex-cli"},
    "codex-config": {"codex-cli"}, "codex-agents": {"codex-cli"},
    "context-mode": {"codex-cli"},
    "markitdown": {"uv"},
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
    if active is not None and getattr(args, "command", None) in {"apply", "update", "reconcile", "repair", "uninstall", "paseo-plugins"}:
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
    sources.update(paseo_plugins.sources(catalog))
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
    if hasattr(args, "plugin_profile_digest") and apply_journal.digest(args.plugin_profile_path) != args.plugin_profile_digest:
        raise CliError("确认后 Paseo 插件声明已变化；未执行旧计划")
    if hasattr(args, 'update_review'):
        review = args.update_review
        secret_path = Path(config_model.config_value(args.kconf, 'PENNIX_SECRETS_FILE'))
        if (apply_journal.digest(secret_path) != review['secrets_digest']
                or apply_journal.digest(CATALOG) != review['catalog_digest']
                or "runtime_digest" in review and release_owner._runtime_digest(ROOT) != review["runtime_digest"]):
            raise CliError('秘密输入或 catalog 与统一审阅不一致；保留事务，不继续执行')


def _write_lock(args: argparse.Namespace) -> None:
    _verify_inputs(args)
    previous = getattr(args, "component_lock_snapshot", owned_files.read(args.component_lock_path, private=True))
    component_lock.validate(args.component_lock, args.component_lock_sources)
    output = owned_files.json_bytes(args.component_lock)
    owned_files.replace(args.component_lock_path, output, previous)
    args.component_lock_snapshot = output
    apply_journal.checkpoint_lock(hashlib.sha256(output).hexdigest())


def _start_transaction(args, command, component, steps, *, require_bound_target=False):
    _verify_inputs(args)
    owned_files.bind_target(args.codex_home, register=True, require_existing=require_bound_target)
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
    if component_id == "base-environment":
        return base_environment.inspect(args.kconf)
    if component_id == "codex-config":
        values = config_model.values(args.kconf)
        active = apply_journal.read()
        checkpoint = next((step for step in (active or {}).get("steps", [])
                           if step["id"] == "codex-config:catalog-review"), None)
        defer = (getattr(args, "all", False)
                 and (checkpoint is None or checkpoint["status"] != "verified"))
        try:
            status = codex_config.inspect(args.codex_home / "config.toml", values, defer_catalog=defer)
        except (codex_config.CodexConfigError, OSError) as error:
            status = {"state": "blocked", "target": str(args.codex_home / "config.toml"),
                      "detail": str(error) if isinstance(error, codex_config.CodexConfigError) else "Codex 配置或目录读取失败"}
            try:
                repair = codex_config.catalog_repair_review(args.codex_home / "config.toml")
                if repair:
                    desired = {"derived.json": repair["catalog"]["files"]["derived.json"],
                               "manifest.json": hashlib.sha256(owned_files.json_bytes(repair["catalog"])).hexdigest()}
                    if repair["observed"] != desired:
                        status.update(state="drifted", catalog_repair_sha256=repair["catalog_repair_sha256"],
                                      catalog=repair["catalog"], catalog_observed=repair["observed"])
            except (codex_config.CodexConfigError, OSError):
                pass  # An invalid fixed source cannot be laundered through asset repair.
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
        if ponytail.selected(component):
            return ponytail.inspect(component, args.codex_home)
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
        owned_files.safe_path(paseo_home)
        identity = paseo_home / "server-id"
        if (not identity.exists() and not identity.is_symlink()
                and (not paseo_home.exists() or paseo_home.is_dir() and not any(paseo_home.iterdir()))
                and "paseo-cli" in config_model.selected_components(kconf)
                and not relay_owner.local_owned()):
            return {"selected": True, "register": True, "action": "install", "state": "pending-identity",
                    "detail": "等待原生 Paseo 配置生成独立身份，再精确登记 relay"}
        state = relay_owner.inspect(paseo_home, nas_ssh=nas_ssh, router_ssh=router_ssh)
        if state == "adoption-required":
            return {"selected": True, "register": True, "action": "blocked", "state": "adoption-required",
                    "detail": "旧 relay receipt 不证明原远端；须用当前审阅摘要显式 repair，历史远端仍不宣称可证",
                    "current_registration_review": relay_owner.review(paseo_home, nas_ssh=nas_ssh, router_ssh=router_ssh)}
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
        if getattr(args, "scope", None) == "base" and component_id not in base_environment.SCOPE:
            components[component_id] = {"selected": False, "action": "keep", "state": "outside-scope"}
            continue
        if component_id == "base-environment":
            if component_id in selected:
                status = base_environment.inspect(kconf)
                action = "blocked" if status["state"] == "blocked" else "keep" if status["state"] == "current" else "install"
                if status.get("packages", {}).get("versions") and config_model.config_value(kconf, "PENNIX_SYSTEM_UPGRADE") != "y":
                    action = "blocked"
                    status["detail"] = "基础软件安装需要同次确认的完整系统升级"
            else:
                owned = base_environment.receipt() is not None
                status = {"state": "current" if owned else "missing"}
                action = "uninstall" if owned else "keep"
            components[component_id] = {**status, "selected": component_id in selected, "action": action}
            continue
        if component_id == "pnpm" and pnpm_migration.needed():
            try:
                pnpm_migration.inspect(args.component_lock["components"].get("pnpm"))
                status = {"state": "migration-required", "source_migration": True,
                          "detail": "经旧 npm receipt/锁核验后退出旧入口，安装官方 pnpm 并更新唯一归属"}
                action = "install" if component_id in selected else "blocked"
                if config_model.config_value(kconf, "PENNIX_SYSTEM_UPGRADE") != "y":
                    action = "blocked"
            except (owner_engine.BootstrapError, owned_files.OwnershipError) as error:
                status, action = {"state": "blocked", "detail": str(error)}, "blocked"
            components[component_id] = {**status, "selected": component_id in selected, "action": action}
            continue
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
            if status["state"] in {"upgrade-required", "plugin-migration-required"}:
                action = "blocked"
            if (ponytail.selected(component) and status["state"] == "missing" and lock_target
                    and not ponytail.formal(lock_target, component)):
                action = "blocked"
                status.update(state="blocked", detail="旧 Ponytail 锁且无可信旧载荷，不能隐式首装或迁移")
            if status["state"] == "migration-required":
                action = "install" if component_id == "tmux-config" else "repair"
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
        if component_id == SCOPED_COLLECTION:
            status["tooling"] = system_devel_tools.inspect(component_id in selected)
            if component_id in selected and status["tooling"]["state"] != "ready":
                status["tooling"]["next"] = TOOLS_NEXT
                action = "blocked"
        components[component_id] = {**status, "selected": component_id in selected, "action": action}
    for key, target in getattr(args, 'update_targets', {}).items():
        item = components[key]
        locked = args.component_lock['components'].get(key, {})
        if item['selected'] and item['action'] == 'install' and item.get('package_state', item['state']) == 'missing':
            item["candidate_target"] = target
            continue
        if candidates.matches(locked, target):
            continue
        if not item['selected'] or item['action'] not in {'keep', 'system-refresh'} or not locked:
            raise CliError(f'{key} 不能从当前归属/漂移状态统一更新；先核验原生 repair/reconcile')
        item.update(action='upgrade', candidate_target=target)
        native = catalog['components'][key].get('native_config')
        if item.get('native_action') == 'keep' and native and config_model.config_value(kconf, native['select']) == 'y':
            item['native_action'] = 'configure'
    if getattr(args, "scope", None) == "base":
        components["paseo-autostart"] = {"selected": False, "action": "keep", "state": "outside-scope",
            "unit_state": "absent", "mode": "off", "run": False, "listen": "", "runtime": {"running": False}}
        return {"config": str(path), "codex_home": str(args.codex_home), "components": components,
                "connections": {}, "functional_checks_requested": False,
                "side_effects": {"path": path_env.plan(Path.home() / ".local", managed=True),
                    "system_upgrade": config_model.config_value(kconf, "PENNIX_SYSTEM_UPGRADE") == "y",
                    "system_upgrade_command": "sudo pacman -Syu --noconfirm",
                    "system_upgrade_components": [key for key, item in components.items() if item.get('selected')
                        and catalog['components'].get(key, {}).get('package', {}).get('source') in {'official', 'aur'}]}}
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
    review_paseo = ("paseo-cli" in getattr(args, "update_targets", {})
                    and getattr(args, "update_review", {}).get("mode") == "noninteractive-v1")
    if service_selected and components.get("paseo-cli", {}).get("state") == "missing":
        runtime = {"state": "missing", "running": False, "healthy": False, "enabled": False,
                   "linger": False, "daemon_version": None}
    elif (review_paseo or service_selected or service_state in {"current", "migration-required"}
          or config_model.config_value(kconf, "PENNIX_PASEO_RELAY") == "y"):
        runtime = paseo_service.runtime(paseo, paseo_home, mode, run)
    else:
        runtime = {"state": "current", "running": False, "enabled": False, "linger": False, "daemon_version": None}
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
    for key, spec in catalog.get('configuration_targets', {}).items():
        if (connections[key].get('selected') and connections[key]['action'] == 'keep'
                and components[spec['component']]['action'] == 'upgrade'):
            connections[key]['action'] = 'configure'
    arch_components = [key for key, item in components.items()
                       if item.get("selected") and catalog["components"].get(key, {}).get("package", {}).get("source") in {"official", "aur"}]
    return {"config": str(path), "codex_home": str(args.codex_home), "components": components, "connections": connections,
            "functional_checks_requested": config_model.config_value(kconf, "PENNIX_FUNCTIONAL_CHECKS") == "y",
            "side_effects": {
                "path": path_env.plan(Path.home() / ".local", managed="base-environment" in selected),
                "system_upgrade": config_model.config_value(kconf, "PENNIX_SYSTEM_UPGRADE") == "y",
                "system_upgrade_command": "sudo pacman -Syu --noconfirm",
                "system_upgrade_components": arch_components,
            }}


def _emit(value: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))
        return
    if isinstance(value, dict) and value.get("scope") == system_devel_tools.SCOPE and "tooling" in value:
        tooling = value["tooling"]
        print(f"系统开发工具: {tooling['state']}；要求 {len(tooling['packages'])} 个包提供项")
        if tooling["missing"]:
            print(f"直接缺包: {', '.join(tooling['missing'])}")
        for row in tooling["packages"].values():
            for name, entry in row["entries"].items():
                if entry["state"] != "system-command":
                    print(f"入口 {name}: {entry['state']}（{entry['visible'] or 'PATH 未找到'}）")
        if value.get("packages_in_transaction"):
            print(f"待审阅的完整 Arch 事务：{len(value['packages_in_transaction'])} 项")
            for row in value["packages_in_transaction"]:
                print(f"  {row['repo']}/{row['name']}: {row['installed'] or '新增'} → {row['target']}")
        if value["transaction_pending"]:
            print("存在未结束事务；使用 pennix recover 核对")
        if value.get("blocked"):
            print(value["blocked"])
        if tooling.get("next"):
            print(tooling["next"])
        if tooling["selected"]:
            print("仅核对包、执行入口和预览；未运行工具功能验证。")
        return
    if isinstance(value, dict) and "components" in value and "config" in value:
        print(f"配置: {value['config']}")
        print(f"Codex: {value['codex_home']}")
        for key, item in value["components"].items():
            print(f"{key}: {item['action']} ({item['state']})")
            if item.get("detail"):
                print(f"  {item['detail']}")
            if item.get("tooling"):
                tooling = item["tooling"]
                print(f"  工具预设: {tooling['state']}")
                if tooling["missing"]:
                    print(f"  缺包: {', '.join(tooling['missing'])}")
                if tooling.get("next"):
                    print(f"  {tooling['next']}")
            if key == "base-environment":
                print(f"  template: {item.get('template_source')} @ {item.get('template_version')}")
                for change in item.get("files", []):
                    print(f"  {change['action']}: {change['path']} ({change['before_sha256'] or 'missing'} -> {change['after_sha256']})")
                packages = item.get("packages", {})
                for name, version in packages.get("versions", {}).items():
                    print(f"  package: {name}={version}")
                for conflict in item.get("conflicts", []):
                    print(f"  conflict: {conflict}")
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
            path_plan = side_effects["path"]
            print(f"PATH: {path_plan['action']} {', '.join(path_plan.get('paths', [path_plan['path']]))} <- {path_plan['prefix']}/bin")
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
            if target == 'cch-tmux' and ('tmux' not in selected or not connections.get('cch', {}).get('selected')):
                raise CliError('CCH 状态栏依赖已选择的 tmux 与 CCH 连接')


def _run_component(args: argparse.Namespace, catalog: dict[str, Any], component_id: str, operation: str, *, repair: bool = False,
                   candidate: dict[str, Any] | None = None) -> str:
    _verify_inputs(args)
    if component_id == SCOPED_COLLECTION and operation in {"install", "upgrade"}:
        _require_system_devel_tools()
    if operation in {"install", "upgrade", "uninstall"}:
        _delegation_preflight(args, component_id, operation)
    component = catalog["components"][component_id]
    owned_files.bind_target(args.codex_home, register=True)
    if component_id == "base-environment":
        active = apply_journal.read()
        target = next((s["target"].get("packages") for s in (active or {}).get("steps", [])
                       if s["component"] == component_id and s["action"] in {"install", "upgrade"}), None)
        return base_environment.uninstall() if operation == "uninstall" else base_environment.apply(args.kconf, catalog, package_target=target)
    native = component.get("native_config")
    if native and operation in {"configure", "unconfigure"}:
        if operation == "unconfigure":
            return native_integrations.unconfigure(native["owner"], args.codex_home)
        return native_integrations.apply(native["owner"], args.codex_home,
            adopt=config_model.config_value(args.kconf, "PENNIX_ADOPT_EXISTING_PACKAGES") == "y",
            repair=repair)
    if native and operation == "uninstall" and component["delivery"] == "package":
        if not (owner_engine.package_absent(component) or
                owner_engine.package_owned(component_id, component, codex_home=args.codex_home)):
            raise CliError("包归属未通过预检；未撤回 native 配置")
        native_integrations.unconfigure(native["owner"], args.codex_home)
    if component_id == "codex-config":
        if operation == "uninstall":
            return codex_config.uninstall(args.codex_home / "config.toml")
        values = config_model.values(args.kconf)
        if repair:
            review = codex_config.catalog_repair_review(args.codex_home / "config.toml")
            if review:
                desired = {"derived.json": review["catalog"]["files"]["derived.json"],
                           "manifest.json": hashlib.sha256(owned_files.json_bytes(review["catalog"])).hexdigest()}
                if review["observed"] != desired:
                    return codex_config.repair_catalog(args.codex_home / "config.toml",
                                                      getattr(args, "expected_sha256", None))
        return codex_config.apply(args.codex_home / "config.toml", values,
            expected_sha256=getattr(args, "expected_sha256", None))
    engine_args = _args_for_engine(args, operation)
    engine_args.repair = repair
    if candidate:
        engine_args.candidate = candidate
        engine_args.explicit_update = operation == "upgrade"
        if component.get('package', {}).get('source') == 'aur':
            active = apply_journal.read()
            identity = {'home': str(Path.home()), 'codex_home': str(args.codex_home),
                        'state_root': str(owned_files.state_root())}
            engine_args.arch_dependencies_allowed = bool(active and active['identity'] == identity
                and any(step['id'] == 'system-upgrade' and step['status'] == 'verified' for step in active['steps']))
    if component_id == "pnpm" and operation in {"install", "upgrade"} and pnpm_migration.needed():
        if candidate is None:
            raise CliError("pnpm 来源迁移缺少同事务的精确官方候选")
        return pnpm_migration.apply(engine_args, catalog, component, candidate)
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
        observed = paseo_service.inspect_delegations(Path(executable), home)
        identity = observed.get("identity", {})
        if (not isinstance(identity, dict) or identity.get("pid") != value["pid"]
                or identity.get("home") != str(home.resolve()) or not value.get("serverId")
                or identity.get("server_id") != value["serverId"]
                or type(observed.get("delegations_open")) is not bool
                or type(observed.get("agents_scanned")) is not int or observed["agents_scanned"] < 0):
            raise ValueError("unverified native delegation inspection")
        if observed["delegations_open"]:
            raise CliError("存在未关闭的 Trellis 工作树委派；先由 controller 保全、接受或取消并核销，再关闭原生 agent，未替换共享组件")
    except (OSError, ValueError, TypeError, paseo_service.PaseoServiceError, subprocess.TimeoutExpired) as error:
        raise CliError("Paseo 委派运行态不可核验；保留现场，未替换共享组件") from error


def cmd_config_init(args: argparse.Namespace) -> int:
    path = config_model.init(args.config, force=args.force, defconfig=args.defconfig,
                             profile=getattr(args, "profile", None),
                             git_name=getattr(args, "git_name", None), git_email=getattr(args, "git_email", None))
    print(path)
    return 0


def cmd_config_check(args: argparse.Namespace) -> int:
    kconf, path = config_model.load(args.config)
    _emit({"config": str(path), "values": config_model.values(kconf)}, args.json)
    return 0


def cmd_config_policy(args: argparse.Namespace) -> int:
    _emit(config_model.project_policy(args.config), args.json)
    return 0


def cmd_config_edit(args: argparse.Namespace) -> int:
    print(config_model.edit(args.config))
    return 0


def _scoped_collection_plan(args: argparse.Namespace) -> tuple[dict, dict, dict]:
    kconf, config, catalog = _load(args)
    args.kconf = kconf
    key = args.component
    if key != SCOPED_COLLECTION or key not in config_model.selected_components(kconf):
        raise CliError("定向首装仅用于已选择的 pennix-system-devel-skills")
    component = catalog["components"][key]
    contract = component.get("collection_contract", {})
    ref = contract.get("source", {}).get("ref", "")
    sha = contract.get("archive_sha256", "")
    if (component.get("delivery") != "collection" or component.get("adapter") != "skill-collection"
            or component.get("native_config") or DEPENDENCIES.get(key) or not contract.get("documentation_only")
            or contract.get("directory") != key or not candidates.SHA.fullmatch(ref)
            or not re.fullmatch(r"[0-9a-f]{64}", sha)):
        raise CliError("定向首装没有可核验的文档型集合来源、提交与归档摘要")
    target = {"source": component["lock_source"], "ref": ref, "sha256": sha}
    step = {"id": f"{key}:install", "component": key, "action": "install", "target": target}
    active = apply_journal.read()
    if active is not None:
        _check_scoped_collection_transaction(args, active, step)
    state, detail = skill_collections.inspect({**component, "_lock_target": target}, args.codex_home)
    locked = args.component_lock["components"].get(key)
    if state == "missing" and locked is None:
        action = "install"
    elif state == "match" and locked == target:
        action = "none"
    else:
        raise CliError(f"定向首装拒绝 {key} 的现有状态 {state} 或不匹配的锁；保留现场，不升级或修复（{detail}）")
    tooling = system_devel_tools.inspect(True)
    if tooling["state"] != "ready":
        action = "blocked"
        tooling["next"] = TOOLS_NEXT
    return ({"operation": "plan", "scope": "component", "component": key, "config": str(config),
             "codex_home": str(args.codex_home),
             "components": {key: {"selected": True, "state": state, "action": action, "target": target,
                                  "tooling": tooling}},
             "side_effects": {"system_upgrade": False}}, catalog, step)


def _check_scoped_collection_transaction(args: argparse.Namespace, active: dict, step: dict) -> None:
    identity = {"home": str(Path.home()), "codex_home": str(args.codex_home),
                "state_root": str(owned_files.state_root())}
    steps = [{key: item.get(key) for key in step} for item in active["steps"]]
    if (active.get("identity") != identity or active["command"] != "apply"
            or active["component"] != step["component"] or steps != [step]
            or active["config_digest"] != apply_journal.digest(args.config_path)
            or active["lock_digest"] != apply_journal.digest(args.component_lock_path)):
        raise CliError("存在不同范围、目标或输入的生命周期事务；保留现场")
    if active["steps"][0]["status"] in {"started", "unknown"}:
        raise CliError("定向首装结果未知；请在原事务使用 recover 核验")


def _apply_scoped_collection(args: argparse.Namespace) -> int:
    value, catalog, step = _scoped_collection_plan(args)
    key, target = step["component"], step["target"]
    if value["components"][key]["action"] == "blocked":
        raise CliError(TOOLS_NEXT)
    if value["components"][key]["action"] == "none":
        _verify_inputs(args)
        active = apply_journal.read()
        if active is not None:
            _check_scoped_collection_transaction(args, active, step)
            if not apply_journal.complete(active):
                raise CliError("集合已存在但事务未完结；请先使用 recover")
            apply_journal.clear()
        _emit({"operation": "apply", "scope": "component", "results": {key: "no-op"}}, args.json)
        return 0
    component = catalog["components"][key]
    with tempfile.TemporaryDirectory(prefix="pennix-collection-candidate-") as directory:
        candidate = candidates.resolve(component, Path(directory), target)
        if candidates.lock_entry(candidate) != target or _scoped_collection_plan(args)[0] != value:
            raise CliError("定向首装候选或计划在暂存期间改变；未执行安装")
        active = _open_transaction(args, "apply", key, [step])
        _check_scoped_collection_transaction(args, active, step)
        if apply_journal.complete(active):
            raise CliError("事务已完结但目标仍缺失；保留现场")
        component["_candidate_target"] = target

        def install():
            result = _run_component(args, catalog, key, "install", candidate=candidate)
            args.component_lock = component_lock.set_entry(args.component_lock, key, target, args.component_lock_sources)
            _write_lock(args)
            return result

        _, result = _journal_mutate(active, step["id"], install)
    apply_journal.clear()
    _emit({"operation": "apply", "scope": "component", "results": {key: result}}, args.json)
    return 0


def _require_system_devel_tools():
    if system_devel_tools.inspect(True)["state"] != "ready":
        raise CliError(TOOLS_NEXT)


def _tools_projection(args, *, preview=False):
    kconf, _, catalog = _load(args)
    args.kconf = kconf
    tooling = system_devel_tools.inspect(SCOPED_COLLECTION in config_model.selected_components(kconf))
    output = {"operation": args.command, "scope": system_devel_tools.SCOPE, "tooling": tooling,
              "side_effects": {"system_upgrade": False}, "transaction_pending": apply_journal.read() is not None}
    target = None
    if tooling["state"] == "missing":
        tooling["next"] = TOOLS_NEXT
        if preview and not output["transaction_pending"]:
            try:
                target = system_devel_tools.review(tooling["missing"])
            except system_devel_tools.ToolError as error:
                output["blocked"] = str(error)
            else:
                output["packages_in_transaction"] = target["transaction"]
                output["side_effects"]["system_upgrade"] = True
    return output, target, catalog


def _finish_scoped_tools(args, catalog, target):
    _verify_inputs(args)
    if not _scope_tools_complete(target):
        raise CliError("包版本、工具入口或数据库一致性未证明；保留事务，不重新执行升级")
    _refresh_arch_receipts(args, catalog, changed={
        row["name"]: (row["installed"], row["target"]) for row in target["transaction"]
        if row["installed"] is not None and row["installed"] != row["target"]})


def _scope_tools_complete(target):
    return (system_devel_tools.manifest_digest() == target["manifest_sha256"]
            and system_devel_tools.installed_packages() == system_devel_tools.expected_packages(target)
            and update_inventory.run(["/usr/bin/pacman", "-Dk"], noninteractive=True).returncode == 0
            and system_devel_tools.inspect(True)["state"] == "ready")


def _apply_system_devel_tools(args):
    scope, step = system_devel_tools.SCOPE, system_devel_tools.STEP
    output, target, catalog = _tools_projection(args, preview=sys.stdin.isatty() and sys.stdout.isatty())
    if output["transaction_pending"]:
        raise CliError("存在未完成的生命周期事务；先使用 pennix recover 核验，不重放包事务")
    state = output["tooling"]["state"]
    if state in {"ready", "not-selected"}:
        _emit({"operation": "apply", "scope": scope, "result": "no-op", "state": state}, args.json)
        return 0
    if target is None:
        raise CliError(output.get("blocked", "包事务需要可核验的工具状态、包候选和原生 TTY；" + TOOLS_NEXT))
    _emit(output, args.json)
    print("下一步会刷新真实 Arch 同步库。请在 pacman 的最终提示核对实际包事务，若与审阅清单不同，必须拒绝。")
    if input("允许进入原生包审阅？输入 yes 继续: ").strip() != "yes":
        return 1
    _verify_inputs(args)
    if system_devel_tools.review(output["tooling"]["missing"]) != target:
        raise CliError("候选在确认期间变化；请重新审阅，未写入包事务")
    active = _start_transaction(args, "apply", scope, [
        {"id": step, "component": scope, "action": "provision", "target": target},
        {"id": f"{step}:record", "component": scope, "action": "record", "target": {}},
    ])

    active = apply_journal.started(active, step)
    result = subprocess.run(["/usr/bin/sudo", "/usr/bin/pacman", "-Syu", "--needed", "--", *target["requested"]],
                            check=False)
    actual = system_devel_tools.installed_packages()
    if actual == target["before"]:
        active = apply_journal.verified(active, step, {"exit_code": result.returncode, "effect": "no-package-change"})
        apply_journal.verified(active, f"{step}:record", {"effect": "not-required"})
        apply_journal.clear()
        _emit({"operation": "apply", "scope": scope, "result": "no-package-change",
               "sync_database_may_have_changed": True}, args.json)
        return 1
    if result.returncode or actual != system_devel_tools.expected_packages(target):
        apply_journal.unknown(active, step)
        raise CliError("原生包事务未报告成功或偏离已审阅范围；保留现场，不重新执行升级")
    active = apply_journal.verified(active, step, {"exit_code": 0, "effect": "reviewed-packages-installed"})
    active, _ = _journal_mutate(active, f"{step}:record", lambda: _finish_scoped_tools(args, catalog, target))
    apply_journal.clear()
    _emit({"operation": "apply", "scope": scope, "result": "packages-verified"}, args.json)
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    if getattr(args, "scope", None) == system_devel_tools.SCOPE:
        _emit(_tools_projection(args, preview=True)[0], args.json)
        return 0
    if getattr(args, "component", None):
        _emit(_scoped_collection_plan(args)[0], True)
        return 0
    value = _plan(args)
    _emit(value, args.json)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    if getattr(args, "scope", None) == system_devel_tools.SCOPE:
        _emit(_tools_projection(args)[0], args.json)
        return 0
    value = _plan(args)
    _emit(value, args.json)
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    if getattr(args, "scope", None) == system_devel_tools.SCOPE:
        output, _, _ = _tools_projection(args)
        _emit(output, args.json)
        return int(output["transaction_pending"] or output["tooling"]["state"] not in {"ready", "not-selected"})
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
    if hasattr(args, 'update_review'):
        steps.append({'id': 'update-review', 'component': 'update-review', 'action': 'review', 'target': args.update_review})
    side_effects = value.get("side_effects", {})
    if side_effects.get("system_upgrade"):
        steps.append({"id": "system-upgrade", "component": "system", "action": "upgrade",
                      "target": getattr(args, 'update_review', {}).get('system', {})})
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
        if key == "codex-config" and item.get("selected"):
            existing = next((step for step in (apply_journal.read() or {}).get("steps", [])
                             if step["id"] == "codex-config:catalog-review"), None)
            if item.get("catalog_checkpoint") or existing:
                steps.append({"id": "codex-config:catalog-review", "component": key, "action": "catalog-review",
                              "target": existing["target"] if existing else {"source": "after-approved-dependencies"}})
                continue
        if item.get("selected") and action in {"install", "adopt", "upgrade", "adopt-upgrade"}:
            steps.append({"id": f"{key}:{action}", "component": key, "action": action,
                          "target": {"packages": item["packages"]} if key == "base-environment" else {} if item.get("source_migration") else item.get("candidate_target") or args.component_lock["components"].get(key, {})})
        if item.get("selected") and item.get("plugin_adoption"):
            steps.append({"id": f"{key}:adopt-plugin", "component": key,
                          "action": "adopt-plugin", "target": item["plugin_adoption"]})
        if key == "paseo-cli" and (value["components"]["paseo-autostart"].get("selected")
                or value["components"].get("paseo-relay", {}).get("state") == "pending-identity"):
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
                          "action": item["action"], "target": {'expected_sha256': item['fingerprint']}
                          if target == 'cch-tmux' and item.get('fingerprint') else {}})
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
    if not value['components'].get('base-environment', {}).get('selected'):
        steps.append({"id": "path-env:apply", "component": "path-env", "action": "apply", "target": {}})
    service = value["components"]["paseo-autostart"]
    paseo_action = value["components"].get("paseo-cli", {}).get("action")
    restart_daemon = bool(service["runtime"].get("running") and (
        service.get("paseo_config", {}).get("restart_required")
        or paseo_action in {"install", "upgrade", "adopt-upgrade"}
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
        if active["command"] != ('update-all' if getattr(args, 'all', False) else 'apply') or active["component"] is not None:
            raise CliError("存在不同范围的生命周期事务；请使用原入口或 recover")
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
    return _start_transaction(args, 'update-all' if getattr(args, 'all', False) else 'apply', None, steps), False


def _journal_mutate(value: dict, step_id: str, action, *, observed=None):
    step = apply_journal.step(value, step_id)
    if step["status"] == "verified":
        return value, "resumed"
    if step["status"] in {"started", "unknown"}:
        raise CliError(f"步骤 {step_id} 的副作用状态未知；保留 journal，需人工核验")
    if step["component"] == "codex-config":
        value = codex_config.prepare_journal(value, step_id)
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
    return apply_journal.verified(apply_journal.read() or value, step_id, observed), result


def _retry_unknown_update(active: dict, step_id: str, state: str) -> dict:
    if apply_journal.step(active, step_id)["status"] != "unknown":
        return active
    raise CliError("update 的副作用未知；版本相同不能证明完整恢复，需使用 recover 核验后置条件")


def _refresh_arch_receipts(args: argparse.Namespace, catalog: dict[str, Any], *, changed=None, entries=None) -> None:
    updates = []
    for component_id, component in catalog["components"].items():
        if entries is not None and component_id not in entries:
            continue
        if getattr(args, "scope", None) == "base" and component_id not in base_environment.SCOPE:
            continue
        if component_id == "pnpm" and pnpm_migration.needed():
            continue
        package = component.get("package") if component else None
        if not isinstance(package, dict) or (package.get("source") != "official" and
                (entries is None or package.get("source") != "aur")):
            continue
        if changed is not None and package["name"] not in changed:
            continue
        receipt = owner_engine._package_receipt(component_id, component)
        if receipt is None and entries is None:
            continue
        identity = owner_engine.package_identity(component)
        if changed is not None:
            before, after = changed[package["name"]]
            locked = args.component_lock["components"].get(component_id)
            receipt_version = receipt["version"] if receipt else None
            lock_version = locked["version"] if locked else None
            if (identity["version"] != after or receipt_version not in {before, after}
                    or lock_version not in {before, after} or locked and locked["source"] != component["lock_source"]):
                raise CliError(f"{component_id} 的原生包回执或锁与已审阅事务不一致；不确认归属")
            if receipt_version == after and lock_version == after and (entries is None or locked == entries[component_id]):
                continue
        updates.append((component_id, component, identity["version"]))
    for component_id, component, version in updates:
        owner_engine._write_package_receipt(component_id, component, version)
        args.component_lock = component_lock.set_entry(
            args.component_lock, component_id,
            entries[component_id] if entries is not None else {"source": component["lock_source"], "version": version},
            args.component_lock_sources,
        )
    if updates:
        _write_lock(args)


def _arch_owner_changes(args, catalog, target, new_owners, *, recorded=False):
    rows = {row["name"]: row for row in target["native_review"]["transaction"]}
    changes = {}
    for key, component in sorted(catalog["components"].items()):
        package = component.get("package", {})
        row = rows.get(package.get("name"))
        if row is None or package.get("source") != ("aur" if row["repo"] is None else "official"):
            continue
        receipt = owner_engine._package_receipt(key, component)
        locked = args.component_lock["components"].get(key)
        before, after = row["installed"], row["target"]
        versions = {before, after} if recorded else {before}
        if key not in new_owners and receipt is None and locked is None:
            continue  # Only a selected, explicitly requested install may gain a new owner.
        if ((before is None) != (key in new_owners)
                or (receipt["version"] if receipt else None) not in versions
                or (locked["version"] if locked else None) not in versions
                or locked and locked["source"] != component["lock_source"]
                or key == "pnpm" and pnpm_migration.needed()):
            raise CliError(f"{key} 的 Arch 归属或锁不能绑定到该事务；未确认组件归属")
        changes[key] = package["name"]
    return changes


def _arch_lock_entries(catalog, target, owners):
    versions = {row["name"]: row["target"] for row in target["native_review"]["transaction"]}
    local = target["native_review"].get("local", {})
    return {key: {"source": catalog["components"][key]["lock_source"], "version": versions[name],
                  **({field: local[name][field] for field in ("ref", "sha256")} if name in local else {})}
            for key, name in owners.items()}


def _arch_record_target(args, catalog, target, owners):
    lock = args.component_lock
    for key, entry in _arch_lock_entries(catalog, target, owners).items():
        lock = component_lock.set_entry(lock, key, entry, args.component_lock_sources)
    return {"owners": owners, "lock_after": hashlib.sha256(owned_files.json_bytes(lock)).hexdigest()}


def _record_reviewed_arch(args, catalog, target, expected):
    _verify_inputs(args)
    try:
        new = {key for key, name in expected["owners"].items() if name not in target["installed"]}
        owners = _arch_owner_changes(args, catalog, target, new, recorded=True)
        if not owners or _arch_record_target(args, catalog, target, owners) != expected:
            raise CliError("Arch 组件归属集合或目标锁与冻结记录不一致；保留事务")
        changed = {row["name"]: (row["installed"], row["target"]) for row in target["native_review"]["transaction"]
                   if row["name"] in owners.values()}
        _refresh_arch_receipts(args, catalog, changed=changed, entries=_arch_lock_entries(catalog, target, owners))
    except owner_engine.BootstrapError as error:
        raise CliError("Arch 包归属回执或实装身份无法核验；保留事务") from error
    if apply_journal.digest(args.component_lock_path) != expected["lock_after"]:
        raise CliError("Arch 组件归属的目标锁未写入；保留事务")
    if apply_journal.read()["lock_digest"] != expected["lock_after"]:
        apply_journal.checkpoint_lock(expected["lock_after"])
    return "current"


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
               if item.get("state") == "pending-owner" and catalog["configuration_targets"][name].get("host") != "copilot-cli"}
    for provider in {"cch-status" if name in {"cch", "cch-tmux"} else "pennix-skills" for name in pending}:
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
                if ("cch-status" if name in {"cch", "cch-tmux"} else "pennix-skills") != provider:
                    continue
                if provider == "cch-status":
                    command = [str(workspace / "prefix/bin" / spec["command"][0]), *spec["command"][1:]]
                elif spec["command"][0] in manifest["commands"]:
                    command = [str(staging / manifest["commands"][spec["command"][0]])]
                else:
                    command = [item.replace("$HOME/.agents/skills/pennix-skills", str(staging)) for item in spec["command"]]
                payload = connection_owners._payload(name, spec, args.kconf,
                    Path(config_model.config_value(args.kconf, "PENNIX_SECRETS_FILE")), "configure")
                validated = connection_owners._run(command, "validate", payload, codex_home=args.codex_home)
                if name == 'cch-tmux':
                    fingerprint = validated.get('fingerprint')
                    if not fingerprint:
                        raise CliError('CCH 状态栏候选未给出现场摘要')
                    next(item for item in steps if item['component'] == 'connection:cch-tmux')['target'] = {'expected_sha256': fingerprint}
            step["target"] = candidates.lock_entry(candidate)


def cmd_apply(args: argparse.Namespace) -> int:
    if getattr(args, "scope", None) == system_devel_tools.SCOPE:
        return _apply_system_devel_tools(args)
    if getattr(args, "component", None):
        return _apply_scoped_collection(args)
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
    reviewed = getattr(args, "noninteractive_review", None)
    steps = _apply_steps(value, args, catalog) if reviewed is None else _reviewed_update_steps(value, args, catalog)
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
    reviewed_paseo = getattr(args, "update_review", {}).get("mode") == "noninteractive-v1"

    def paseo_still_stopped():
        return _paseo_cli_execution_ready({**service, "runtime": paseo_service.runtime(
            paseo, paseo_home, service["mode"], service["run"])})

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
                or relay and relay["selected"] and relay["action"] != "keep"
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
        if reviewed_paseo and not _paseo_cli_execution_ready(service):
            raise CliError("Paseo 未证明同一目标 daemon 已停止；未继续更新")
        component = catalog["components"]["paseo-cli"]
        target = paseo_step["target"] or None
        try:
            with tempfile.TemporaryDirectory(prefix="pennix-paseo-preflight-") as directory:
                candidate = candidates.resolve(component, Path(directory), target)
                candidate_target = candidates.lock_entry(candidate)
                paseo_service.preflight_candidate(candidate, plugins_required=bool(paseo_plugins.profile_path(args.kconf)))
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
    if active is None and reviewed is None and (value.get("side_effects", {}).get("system_upgrade") or getattr(args, 'all', False)):
        _emit(value, args.json)
        if hasattr(args, 'update_review'):
            _emit({'unified_review': {key: val for key, val in args.update_review.items() if key != 'secrets_digest'},
                   'updates': args.update_inventory}, args.json)
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
        if getattr(args, 'all', False):
            _verify_update_candidates(args, catalog)
        if hasattr(args, "config_snapshot") and _plan(args) != value:
            raise CliError("确认期间资源或归属已变化；请重新计划")
        if reviewed is not None and _update_review_output(value, steps, args) != reviewed:
            raise CliError("全量更新确认后写入步骤或已有身份改变；请重新审阅")
        active, _ = _journal_for_apply(args, value, steps, create=True)
    relay_home = paseo_home
    results: dict[str, str] = {}
    system_step = next((item for item in active["steps"] if item["id"] == "system-upgrade"), None)
    if system_step is not None and reviewed is not None:
        transaction, target = active["transaction"], system_step["target"]
        frozen = args.update_review
        digest = arch_recovery.receipt_digest(transaction, target)
        if apply_journal.step(active, "update-review")["status"] != "verified":
            active = apply_journal.verified(active, "update-review", {"inputs": "bound"})
        active, _ = _journal_mutate(active, "system-upgrade",
            lambda: arch_recovery.commit(transaction, target, frozen["arch_executor"]),
            observed={"state": "native-commit-verified", "receipt_sha256": digest})
        results["system-upgrade"] = "changed"
        owner_engine._candidate.cache_clear()
        if record := frozen.get("arch_record"):
            active, results["system-upgrade:record"] = _journal_mutate(active, "system-upgrade:record",
                lambda: _record_reviewed_arch(args, catalog, target, record))
        value = _plan(args)
        _, _, catalog = _load(args)
        if _reviewed_update_steps(value, args, catalog) != steps:
            raise CliError("Arch 提交后其他写入步骤或组件归属发生变化；保留记录，不执行其余步骤")
        system_step = None  # Confirmed native work must not enter the legacy writer or replanner.
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
        if system_step['target']:
            update_inventory.verify_system(system_step['target']['installed'], system_step['target']['upgrades'],
                accepted=getattr(args, 'update_review', {}).get('reviewed_after_upgrade'))
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
        if step_id == 'update-review':
            active = apply_journal.verified(active, step_id, {'inputs': 'bound'})
            continue
        if component_id == "codex-config" and action == "catalog-review":
            result = _catalog_checkpoint(args, active, step)
            if result is not None:
                return result
            active = apply_journal.read()
            results[component_id] = "verified-current"
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
        if action in {"install", "adopt", "upgrade", "adopt-upgrade"} and component_id in catalog["components"]:
            component = catalog["components"][component_id]
            operation = "upgrade" if action in {"upgrade", "adopt-upgrade"} else "install"
            target = step["target"] or args.component_lock["components"].get(component_id)
            if component_id == "pnpm" and pnpm_migration.needed() and not step["target"]:
                target = (pnpm_migration.pending() or {}).get("new_target")
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
                    check_paseo = reviewed_paseo and component_id == "paseo-cli"
                    if check_paseo and not paseo_still_stopped():
                        raise CliError("Paseo 停止状态自审阅后发生变化；未更新 CLI")
                    def install(candidate=candidate, component_id=component_id, component=component, target=target):
                        result = _run_component(args, catalog, component_id, operation, candidate=candidate)
                        args.component_lock = component_lock.set_entry(args.component_lock, component_id, target, args.component_lock_sources)
                        _write_lock(args)
                        if component_id == "pnpm":
                            pnpm_migration.finish(component, target)
                        if check_paseo and not paseo_still_stopped():
                            raise CliError("Paseo CLI 已写入，但 daemon 停止状态无法继续证明；保留事务")
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
                        operation_id=f"{active.get('transaction', 'legacy')}:{step_id}",
                        expected_sha256=step['target'].get('expected_sha256'))
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
            name = "zshrc-user-bin-path" if path_env.targets()[0].name == ".zshrc" else "bash-user-bin-path"
            active, results[name] = _journal_mutate(
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


def _connection_update_steps(args, catalog):
    result = []
    for name, spec in catalog.get('configuration_targets', {}).items():
        if ((spec.get('host') != 'copilot-cli' and name != 'cch-tmux') or spec['component'] != args.component
                or config_model.config_value(args.kconf, spec['select']) != 'y'):
            continue
        target = {}
        if name == 'cch-tmux':
            observed = connection_owners.inspect_target(name, spec, args.codex_home)
            if observed['state'] in {'blocked', 'drifted', 'external'} or not observed.get('fingerprint'):
                raise CliError('CCH 状态栏现场不可核验；未更新包')
            target = {'expected_sha256': observed['fingerprint']}
        result.append({'id': f'connection:{name}:configure', 'component': f'connection:{name}',
                       'action': 'configure', 'target': target})
    return result


def _configure_updated_connections(args, catalog, active, *, repair=False):
    for step in active['steps']:
        if not step['component'].startswith('connection:'):
            continue
        name = step["component"].split(":", 1)[1]
        active, _ = _journal_mutate(active, step["id"], lambda name=name:
            connection_owners.apply_target(name, catalog["configuration_targets"][name], args.kconf,
                args.codex_home, Path(config_model.config_value(args.kconf, "PENNIX_SECRETS_FILE")), "configure", repair=repair,
                operation_id=f"{active['transaction']}:{step['id']}", expected_sha256=step['target'].get('expected_sha256')))
    return active


def cmd_repair(args: argparse.Namespace) -> int:
    kconf, _, catalog = _load(args)
    args.kconf = kconf
    args.codex_home = Path(os.path.expanduser(config_model.config_value(kconf, "PENNIX_CODEX_HOME")))
    active = apply_journal.read()
    if active is not None and (active["command"] != "repair" or active["component"] != args.component):
        raise CliError("存在其他未完成 lifecycle 事务；先从原入口或 recover 核验恢复")
    if args.component == "paseo-relay":
        if (not args.expected_sha256 or config_model.config_value(kconf, "PENNIX_PASEO_RELAY") != "y"
                or config_model.config_value(kconf, "PENNIX_PASEO_RELAY_REGISTER") != "y"):
            raise CliError("relay repair 需要已选择远端登记及 plan 中当前登记审阅摘要")
        target = {"review_sha256": args.expected_sha256}
        steps = [{"id": "paseo-relay:adopt-current", "component": "paseo-relay", "action": "adopt-current", "target": target}]
        active = _open_transaction(args, "repair", args.component, steps)
        if active["steps"][0]["target"] != target:
            raise CliError("relay 当前登记审阅与同事务不一致")
        active, result = _journal_mutate(active, steps[0]["id"], lambda: relay_owner.adopt_current(
            Path(config_model.config_value(kconf, "PENNIX_PASEO_HOME")), args.expected_sha256,
            nas_ssh=config_model.config_value(kconf, "PENNIX_RELAY_NAS_SSH"),
            router_ssh=config_model.config_value(kconf, "PENNIX_RELAY_ROUTER_SSH")))
        apply_journal.clear()
        _emit({"operation": "repair", "component": args.component, "status": result}, args.json)
        return 0
    if args.component not in catalog["components"]:
        raise CliError(f"未知组件: {args.component}")
    operation = "upgrade"
    component = catalog["components"][args.component]
    if args.component not in config_model.selected_components(kconf):
        raise CliError(f"组件未在 workflow.config 中选择: {args.component}")
    if args.component == SCOPED_COLLECTION:
        _require_system_devel_tools()
    if args.expected_sha256 is not None and args.component not in {"codex-agents", "codex-config"} and component.get("delivery") != "client":
        raise CliError("--expected-sha256 只用于审阅后的配置、relay 当前登记或固定本地客户端升级")
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
            repair_bundle = state == "blocked" and owner_engine.codegraph_main_owned(args.component, component)
            status = {"state": state, "ownership": "lifecycle" if repair_bundle or owner_engine.package_owned(args.component, component, observed, args.codex_home) else "external",
                      "repair_bundle": repair_bundle}
        missing_owned = (status["state"] == "missing" and component.get("delivery") == "package"
                         and owner_engine._package_receipt(args.component, component) is not None
                         and owner_engine.package_absent(component))
        if status["state"] not in {"match", "outdated", "migration-required"} and not missing_owned and not status.get("repair_bundle"):
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
            steps.extend(_connection_update_steps(args, catalog))
            active = _open_transaction(args, "repair", args.component, steps)
            if apply_journal.step(active, steps[0]["id"])["target"] != candidate_target:
                raise CliError("修复候选与已记录的精确目标不同")
            def repair_component():
                result = _run_component(args, catalog, args.component, operation, candidate=candidate, repair=True)
                args.component_lock = component_lock.set_entry(args.component_lock, args.component, candidate_target, args.component_lock_sources)
                _write_lock(args)
                return result
            active, result = _journal_mutate(active, steps[0]["id"], repair_component)
    else:
        active = _open_transaction(args, "repair", args.component,
            [{"id": f"{args.component}:repair", "component": args.component, "action": "upgrade", "target": {}}])
        active, result = _journal_mutate(active, f"{args.component}:repair",
            lambda: _run_component(args, catalog, args.component, operation,
                                   repair=args.component == "codex-config"))
    native = component.get("native_config")
    if native and config_model.config_value(kconf, native["select"]) == "y":
        active, configured = _journal_mutate(active, f"{args.component}:configure",
            lambda: _run_component(args, catalog, args.component, "configure", repair=True))
        result = f"{result}; native {configured}"
    if args.component != "base-environment":
        active = _configure_updated_connections(args, catalog, active, repair=True)
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
    if ponytail.selected(component):
        raise CliError("Ponytail 不接受当前分支载荷；请用 pennix update ponytail-plugin 核验正式发行和旧登记")
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


def _updates(args):
    value = _plan(args)
    kconf, _, catalog = _load(args)
    report = update_inventory.inspect(catalog, config_model.selected_components(kconf),
        args.component_lock['components'], value,
        system_upgrade=config_model.config_value(kconf, 'PENNIX_SYSTEM_UPGRADE') == 'y',
        reviewed=getattr(args, 'plan', False) or getattr(args, 'confirm', None) is not None)
    report['paseo_plugins'] = paseo_plugins.inventory(kconf, catalog, args.component_lock['components'])
    if ("codex-config" in config_model.selected_components(kconf)
            and config_model.config_value(kconf, "PENNIX_CODEX_MANAGED_CATALOG") == "y"):
        if getattr(args, "all", False):
            result = {"state": "native", "available": None,
                      "catalog_source_state": "deferred-after-approved-dependencies",
                      "next": "same-journal native catalog review; not an assertion of freshness"}
        else:
            try:
                result = codex_config.catalog_inventory(args.codex_home / "config.toml",
                    config_model.values(kconf), args.component_lock_path)
            except (codex_config.CodexConfigError, OSError, ValueError, subprocess.SubprocessError):
                result = {"state": "unknown", "available": None, "catalog_source_state": "unverified",
                          "detail": "原生模型目录的新鲜度或现有归属无法核验；未修改活动资产",
                          "next": "pennix update codex-config --plan"}
        report["components"]["codex-config"] = {**report["components"]["codex-config"], **result}
    return report, value, catalog


def cmd_updates(args):
    report, _, _ = _updates(args)
    _emit(report, args.json)
    return 2 if report['system']['state'] != 'current' or any(
        item['state'] == 'unknown' for group in (report['components'], report['paseo_plugins'])
        for item in group.values()) else 0


def _verify_update_candidates(args, catalog):
    review = args.update_review
    native = review['system'].get('native_review')
    if native is not None or (getattr(args, 'noninteractive_review', None) is not None
                              and args.update_inventory['system_upgrade_enabled']):
        system = update_inventory.arch(include_packages=native['requested'] if native else (), review_full=True)
    else:
        system = update_inventory.arch()
    if system['state'] != 'current' or any(system[key] != review['system'][key] for key in ('installed', 'upgrades')):
        raise CliError('确认期间完整系统升级清单改变；未开始事务，请重新审阅')
    recomputed = system.get('native_review')
    if native and native.get("local"):
        recomputed = _with_aur_artifacts(system, native["local"])["native_review"]
    if (recomputed != native or native
            and system.get('real_sync_sha256') != review['system'].get('real_sync_sha256')):
        raise CliError('确认期间 Arch 包工件或原生配置改变；未开始事务，请重新审阅')
    for key, available in review['available'].items():
        try:
            current = update_inventory.available(catalog['components'][key], args.component_lock['components'].get(key), system)
        except update_inventory.CANDIDATE_FAILURES as error:
            detail = update_inventory.candidate_failure(error)
            raise CliError(f"{key}: {detail['detail']}；{detail['next']}") from error
        if current != available:
            raise CliError(f'{key} 来源候选在确认期间改变；请重新审阅')


def _paseo_cli_execution_ready(service):
    runtime = service["runtime"]
    return (runtime.get("state") == "current" and runtime.get("running") is False
            and runtime.get("stopped_verified") is True and not service["run"])


def _with_aur_artifacts(system, local):
    native = system.get("native_review") or {"requested": [], "transaction": [], "policy": update_inventory._arch_policy()}
    rows = [*native["transaction"]]
    for name, approval in local.items():
        if any(row["name"] == name for row in rows):
            raise CliError("AUR 工件与原生同步包计划重名；未开始升级")
        rows.append({"repo": None, "name": name, "installed": system["installed"].get(name), "target": approval["version"],
                     "filename": approval["filename"], "sha256": approval["sha256"]})
    return {**system, "native_review": {**native, "transaction": sorted(rows, key=lambda item: item["name"]), "local": local}}


def _reviewed_update_steps(value, args, catalog):
    review = args.update_review
    system = review["system"]
    native = system.get("native_review")
    if (native and not review.get("arch_executor")) or (
            not native and system["upgrades"] and value["side_effects"].get("system_upgrade")):
        raise CliError("需要经审阅的特权 Arch 事务；本入口未取得可用的固定执行器")
    for key, item in value["components"].items():
        action = item.get("action")
        if action in {"keep", "ignored"} and item.get("native_action", "keep") == "keep":
            continue
        if (action == "system-refresh" and key in review.get("arch_owners", {})
                and item.get("native_action", "keep") == "keep"):
            continue
        if action not in {"install", "upgrade"} or item.get("native_action", "keep") != "keep":
            raise CliError(f"{key} 涉及 {action} 或原生配置变更；无 TTY 全量未执行")
    service = value["components"]["paseo-autostart"]
    paseo_update = "paseo-cli" in args.update_targets
    steps = _apply_steps(value, args, catalog)
    exact = []
    owners, local = review.get("arch_owners", {}), (native or {}).get("local", {})
    for step in steps:
        key = step["component"]
        if key == "system":
            if native:
                exact.append(step)
                if record := review.get("arch_record"):
                    exact.append({"id": "system-upgrade:record", "component": "system", "action": "record",
                                  "target": record})
            continue
        if ((key == "paseo-config" and service.get("paseo_config", {}).get("state") == "current") or
                (key == "paseo-autostart" and service["action"] == "keep" and not service["run"]
                 and (not step["target"]["restart_daemon"] or
                      paseo_update and not service.get("paseo_config", {}).get("restart_required"))) or
                (key == "path-env" and value["side_effects"].get("path", {}).get("action") == "keep")):
            continue
        if key == "paseo-autostart" and step["target"]["restart_daemon"]:
            raise CliError("本次更新需要重启 Paseo，但尚无经核验的维护边界；未执行写入")
        if key != 'update-review':
            component = catalog['components'].get(key, {})
            if key == "codex-config" and step["action"] == "catalog-review":
                exact.append(step)
                continue
            if key == "aur-helper":
                if all(owners.get(item["component"]) in local for item in steps
                       if catalog["components"].get(item["component"], {}).get("package", {}).get("source") == "aur"):
                    continue
            if (step["action"] == "install" and component.get("package", {}).get("source") == "official"
                    and owners.get(key) in (native or {}).get("requested", [])):
                continue  # The fixed system step and its record step own this exact installation.
            if step["action"] == "install" and owners.get(key) in local:
                continue
            if component.get("package", {}).get("source") in {"official", "aur"} or key == "aur-helper":
                raise CliError(f"{key} 需要固定特权包执行器；无 TTY 全量未执行")
            if (step["action"] not in {"install", "upgrade"} or not value["components"].get(key, {}).get("selected")
                    or not step["target"].get("sha256") or step["target"] != args.update_targets.get(key)):
                raise CliError(f"{key} 需要独立原生审阅或缺少精确候选；无 TTY 全量未执行")
        exact.append(step)
    return exact


def _update_review_output(value, steps, args):
    binding = _review_target()
    public_steps = [{"component": item["component"], "action": item["action"],
                     "candidate": ({"owners": item["target"]["owners"]}
                                   if item["id"] == "system-upgrade:record" else item["target"])}
                    for item in steps if item["id"] != "update-review"]
    public = {"operation": "update-all", "target_id": binding[:16], "steps": public_steps,
              "system_upgrade": any(item["id"] == "system-upgrade" for item in steps), "service_restart": False,
              "status": "review-required" if public_steps else "no-op"}
    if not public_steps:
        return public
    preconditions = []
    plan = value
    if any(item["component"] == "paseo-cli" for item in public_steps):
        preconditions.append("paseo-daemon-stopped")
        service = value["components"]["paseo-autostart"]
        public.update(execution_preconditions=preconditions, execution_ready=_paseo_cli_execution_ready(service))
        # Stopping the daemon satisfies the bound condition; it does not change the approved writes.
        stable_service = {**service, "runtime": {
            key: service["runtime"].get(key) for key in ("enabled", "linger")
        }}
        if "paseo_config" in service:
            config = service["paseo_config"]
            stable_service["paseo_config"] = {**config, "versions": {"cli": config["versions"]["cli"]}}
        plan = {**value, "components": {**value["components"], "paseo-autostart": stable_service}}
    payload = {"schema": 2, "binding": binding, "config": apply_journal.digest(args.config_path),
               "config_path": str(args.config_path), "lock": apply_journal.digest(args.component_lock_path),
               "runtime": release_owner._runtime_digest(ROOT), "plan": plan, "steps": steps,
               "execution_preconditions": preconditions}
    return {**public, "review_sha256": hashlib.sha256(owned_files.json_bytes(payload)).hexdigest()}


def cmd_update_all(args):
    active = apply_journal.read()
    explicit = getattr(args, "plan", False) or getattr(args, "confirm", None) is not None
    if getattr(args, "resume", False) and (active is None or explicit or args.review_system_changes):
        raise CliError("--resume 仅用于同一未结全量事务，不能与新审阅或系统重审混用")
    if active is not None:
        if explicit:
            raise CliError("已有未完成事务；先使用 pennix recover 核对，不签发或重放审阅摘要")
        if active['command'] != 'update-all':
            raise CliError('存在其他未完成事务；先使用原入口恢复')
        review = next((step['target'] for step in active['steps'] if step['id'] == 'update-review'), None)
        if not review:
            raise CliError('统一更新缺少冻结审阅；保留现场')
        if getattr(args, "resume", False) and (review.get("mode") != "noninteractive-v1"
                or not any(step["id"] == "codex-config:catalog-review" for step in active["steps"])):
            raise CliError("--resume 仅用于包含目录检查点的同一已审阅全量事务；其他现场按原入口或 recover")
        # A reviewed transaction must never resume through legacy interactive apply.
        if review.get('mode') == 'noninteractive-v1':
            if not getattr(args, "resume", False):
                raise CliError('已有非交互审阅事务；使用 recover 核对，不通过旧入口重放；已核验检查点用 update --all --resume')
            if any(step["status"] in {"started", "unknown"} for step in active["steps"]):
                raise CliError("全量事务有未知副作用；先使用同次 recover，不能通过 --resume 重试")
            args.noninteractive_review = {"resume": True}
        args.update_targets = review['targets']
        args.update_review = review
        args.update_inventory = {'state': 'resume-frozen-journal'}
        if args.review_system_changes:
            system_step = next((step for step in active['steps'] if step['id'] == 'system-upgrade'), None)
            if not system_step or system_step['status'] != 'verified':
                raise CliError('只有已证明完成的系统升级可重审；不释放 unknown')
            args.kconf, _, _ = _load(args)
            _verify_inputs(args)
            actual = update_inventory.installed_arch()
            expected = dict(review['system']['installed'])
            expected.update({item['name']:item['available'] for item in review['system']['upgrades']})
            _emit({'system_after_upgrade': {key: {'reviewed':expected.get(key), 'actual':actual.get(key)}
                for key in sorted(expected.keys() | actual.keys()) if expected.get(key) != actual.get(key)}}, args.json)
            if not sys.stdin.isatty() or not sys.stdout.isatty() or input('确认接受已完成系统升级的实际差异？输入 yes: ').strip() != 'yes':
                return 1
            _verify_inputs(args)
            if update_inventory.installed_arch() != actual:
                raise CliError('系统包在重审期间改变；未接受')
            review = {**review, 'reviewed_after_upgrade':actual}
            apply_journal.set_target(active, 'update-review', review)
            args.update_review = review
        return cmd_apply(args)
    if args.review_system_changes:
        raise CliError('没有可重审的统一更新事务')
    report, value, catalog = _updates(args)
    if not explicit:
        _emit(report, args.json)
    if report['components'].get(ponytail.KEY, {}).get('state') == 'migration-required':
        raise CliError('Ponytail 旧登记必须先经专用单项更新迁移；未执行统一更新')
    if report['system']['state'] != 'current':
        if explicit and report['system'].get('detail'):
            raise CliError(f"完整 Arch 影响尚不可核验：{report['system']['detail']}；未执行统一更新")
        raise CliError('完整 Arch 影响尚不可核验；未执行统一更新')
    unknown = [f"{key}: {item.get('detail', '无法核验当前组件')}；{item.get('next', '先运行 pennix plan 核对受管身份')}"
               for key, item in report['components'].items() if item['state'] in {'unknown', 'unmanaged'}]
    if unknown:
        raise CliError('全量更新未执行：' + '；'.join(unknown))
    if not explicit and (not sys.stdin.isatty() or not sys.stdout.isatty()):
        raise CliError('统一更新必须在真实终端审阅并确认；清单为只读，未执行变更')
    targets, aur = {}, {}
    for key, item in report['components'].items():
        if item['state'] != 'update':
            continue
        component = catalog['components'][key]
        if component.get('package', {}).get('source') == 'official':
            if not report['system_upgrade_enabled']:
                raise CliError('官方包更新必须选择完整系统升级；拒绝部分升级')
            continue
        if explicit and component.get('package', {}).get('source') == 'aur':
            if not report["system_upgrade_enabled"]:
                raise CliError("AUR 工件需要完整系统事务；拒绝部分升级")
            name = component["package"]["name"]
            if any(package == name for package, _ in aur.values()):
                raise CliError("多个组件不能共享同一 AUR 安装归属")
            try:
                with tempfile.TemporaryDirectory(prefix="pennix-aur-source-") as directory:
                    candidates.resolve(component, Path(directory), item["available"])
            except update_inventory.CANDIDATE_FAILURES as error:
                failure = update_inventory.candidate_failure(error)
                raise CliError(f"{key}: {failure['detail']}；{failure['next']}") from error
            aur[key] = (name, arch_recovery.aur_candidate(name, item["available"]))
            continue
        try:
            with tempfile.TemporaryDirectory(prefix='pennix-update-freeze-') as directory:
                candidate = candidates.resolve(component, Path(directory), item['available'])
                targets[key] = candidates.lock_entry(candidate)
        except update_inventory.CANDIDATE_FAILURES as error:
            failure = update_inventory.candidate_failure(error)
            raise CliError(f"{key}: {failure['detail']}；{failure['next']}") from error
    if aur:
        report = {**report, "system": _with_aur_artifacts(report["system"], dict(aur.values()))}
    args.update_targets = targets
    args.update_inventory = report
    args.update_review = {'targets': targets,
        'available': {key: item['available'] for key, item in report['components'].items() if item['available'] is not None},
        'system': report['system'], 'catalog_digest': apply_journal.digest(CATALOG),
        'secrets_digest': apply_journal.digest(Path(config_model.config_value(args.kconf, 'PENNIX_SECRETS_FILE')))}
    if value["components"].get("codex-config", {}).get("catalog_checkpoint"):
        args.update_review["runtime_digest"] = release_owner._runtime_digest(ROOT)
    if explicit:
        args.update_review['mode'] = 'noninteractive-v1'
        value = _plan(args)
        _verify_inputs(args)
        native = report["system"].get("native_review")
        if native:
            if not value["side_effects"].get("system_upgrade"):
                raise CliError("固定 Arch 事务需要选择完整系统升级；拒绝部分升级")
            new = {key: package["name"] for key, item in value["components"].items()
                   if key in APPLY_ORDER and item.get("selected") and item["action"] == "install"
                   and (package := catalog["components"].get(key, {}).get("package", {})).get("source") == "official"}
            if set(new.values()) != set(native["requested"]):
                raise CliError("Arch 明确安装请求与选定组件不一致；未开始升级")
            for key, (name, _) in aur.items():
                item = value["components"][key]
                expected = "keep" if name in report["system"]["installed"] else "install"
                if not item["selected"] or item["action"] != expected or item.get("native_action", "keep") != "keep":
                    raise CliError(f"{key} 的归属或原生配置不能并入本次 AUR 工件事务")
                if expected == "install":
                    new[key] = name
            args.update_review["arch_executor"] = arch_recovery.review_executor(report["system"])
            try:
                owners = _arch_owner_changes(args, catalog, report["system"], new)
            except owner_engine.BootstrapError as error:
                raise CliError("Arch 组件原有归属回执不可核验；未开始升级") from error
            if any(owners.get(key) != name for key, (name, _) in aur.items()):
                raise CliError("AUR 工件缺少与选定组件匹配的归属；未开始升级")
            args.update_review["arch_owners"] = owners
            if owners:
                args.update_review["arch_record"] = _arch_record_target(args, catalog, report["system"], owners)
        steps = _reviewed_update_steps(value, args, catalog)
        review = _update_review_output(value, steps, args)
        if args.plan:
            _emit(review, args.json)
            return 0
        if review.get("review_sha256") != args.confirm:
            raise CliError("全量更新审阅摘要不匹配；请重新执行 pennix update --all --plan")
        if review.get("execution_ready") is False:
            raise CliError("审阅摘要匹配，但写入前必须证明同一目标 Paseo daemon 已停止；未执行升级")
        args.noninteractive_review = review
    return cmd_apply(args)


def cmd_aur_prepare(args):
    if apply_journal.read() is not None:
        raise CliError("已有未完成事务；先通过 recover 核对")
    kconf, _, catalog = _load(args)
    component = catalog["components"].get(args.component, {})
    if (component.get("package", {}).get("source") != "aur"
            or args.component not in config_model.selected_components(kconf)):
        raise CliError("只准备 workflow.config 已选、来源已登记的 AUR 组件")
    try:
        with tempfile.TemporaryDirectory(prefix="pennix-aur-build-") as directory:
            checkout, data = aur_build.inspect(component, Path(directory))
            target = _review_target()
            review = {"operation": "aur-prepare", "component": args.component, "target_id": target[:16], **data}
            binding = {"review": review, "config": apply_journal.digest(args.config_path),
                       "catalog": apply_journal.digest(CATALOG), "runtime": release_owner._runtime_digest(ROOT),
                       "target": target}
            digest = hashlib.sha256(owned_files.json_bytes(binding)).hexdigest()
            _verify_inputs(args)
            if args.plan:
                _emit({**review, "review_sha256": digest, "installs_packages": False}, args.json)
                return 0
            if args.confirm != digest:
                raise CliError("AUR 构建审阅摘要不匹配；没有执行配方")
            result = aur_build.build(checkout, data)
    except candidates.CandidateError:
        raise
    except update_inventory.CANDIDATE_FAILURES + (arch_recovery.ArchCommitError,) as error:
        raise CliError("AUR 构建材料或隔离环境无法核验；没有安装软件") from error
    _emit({"operation": "aur-prepare", "component": args.component, "artifact": result,
           "installs_packages": False, "next": "由管理员核对工件并独立签署审批，再审阅全量安装"}, args.json)
    return 0


def _ponytail_update(args, component):
    if args.version:
        raise CliError("Ponytail 只从已登记的正式发行选择候选，不接受 --version")
    if apply_journal.read() is not None:
        raise CliError("存在未完成事务；Ponytail 只能使用同一事务的 recover，不重放 update")
    native = ponytail.Native(component, args.codex_home)
    before = native.observe()
    record, raw = ponytail.receipt(component)
    previous = args.component_lock["components"].get(ponytail.KEY)
    if (not ponytail.matches_receipt(previous, record) or raw != owned_files.json_bytes(record)
            or args.component_lock_snapshot != owned_files.json_bytes(args.component_lock)
            or not before["payload"] or not before["market"]):
        raise CliError("Ponytail 当前锁、载荷或可信回执不完整；未开始更新")
    if record["schema"] == 1:
        if not ponytail.matches_legacy(before, record):
            raise CliError("Ponytail 旧登记或载荷不符合迁移条件；保留现场")
    elif not native.matches(before, previous):
        raise CliError("Ponytail 正式登记或实际载荷发生漂移；保留现场")
    with tempfile.TemporaryDirectory(prefix="pennix-ponytail-review-") as directory:
        workspace = Path(directory)
        target = candidates.lock_entry(candidates.resolve(component, workspace))
        if record["schema"] == 2:
            candidates.verify_plugin_upgrade(previous, target)
        if record["schema"] == 2 and previous == target:
            if args.confirm:
                raise CliError("预览所对应的 Ponytail 更新已完成或不存在；不重放确认")
            _emit({"operation": "update", "component": ponytail.KEY, "status": "no-op", "target": target}, args.json)
            return 0
        ponytail.prove_retrievable(native, workspace / 'previous.git',
                                   before["market"]["ref"], before["payload"]["ref"])
    next_lock = component_lock.set_entry(args.component_lock, ponytail.KEY, target, args.component_lock_sources)
    frozen = {"before": before, "previous": previous, "receipt_before": record, "desired": target,
              "lock_before": apply_journal.digest(args.component_lock_path),
              "lock_after": hashlib.sha256(owned_files.json_bytes(next_lock)).hexdigest()}
    digest = hashlib.sha256(owned_files.json_bytes({
        **frozen, "config_digest": apply_journal.digest(args.config_path)
    })).hexdigest()
    _verify_inputs(args)
    if not args.confirm:
        _emit({"operation": "update", "component": ponytail.KEY, "current": previous,
               "actual": before["payload"], "candidate": target, "tag": f"v{target['version']}",
               "review_sha256": digest, "status": "review-required"}, args.json)
        return 0
    if args.confirm != digest:
        raise CliError("Ponytail 审阅摘要不匹配；重新审阅实际载荷和正式候选")
    if native.observe() != before or ponytail.receipt(component)[1] != raw:
        raise CliError("Ponytail 预览期间原生登记、载荷或回执改变")
    step_id = f"{ponytail.KEY}:release"
    active = _start_transaction(args, "update", ponytail.KEY, [
        {"id": step_id, "component": ponytail.KEY, "action": "pin-release", "target": frozen}],
        require_bound_target="target" not in record)
    active, _ = _journal_mutate(active, step_id, lambda: _ponytail_apply(args, component, frozen, restore=False))
    apply_journal.clear()
    _emit({"operation": "update", "component": ponytail.KEY, "status": "changed", "target": target}, args.json)
    return 0


def _ponytail_apply(args, component, frozen, *, restore):
    _verify_inputs(args)
    if "target" not in frozen["receipt_before"]:
        owned_files.bind_target(args.codex_home, require_existing=True)
    native = ponytail.Native(component, args.codex_home)
    before, desired = frozen["before"], frozen["desired"]
    old_record = owned_files.json_bytes(frozen["receipt_before"])
    new_record = owned_files.json_bytes(ponytail.receipt_record(component, desired))
    _, current_receipt = ponytail.receipt(component)
    if current_receipt not in {old_record, new_record}:
        raise CliError("Ponytail 回执脱离事务记录；保留现场")
    previous = frozen["previous"]
    lock_before = component_lock.set_entry(args.component_lock, ponytail.KEY, previous, args.component_lock_sources)
    lock_after = component_lock.set_entry(args.component_lock, ponytail.KEY, desired, args.component_lock_sources)
    if (hashlib.sha256(owned_files.json_bytes(lock_before)).hexdigest() != frozen["lock_before"]
            or hashlib.sha256(owned_files.json_bytes(lock_after)).hexdigest() != frozen["lock_after"]
            or apply_journal.digest(args.component_lock_path) not in {frozen["lock_before"], frozen["lock_after"]}):
        raise CliError("Ponytail 锁不匹配全部冻结检查点；未继续事务")
    if restore:
        ponytail.restore(native, before, desired)
    else:
        ponytail.move(native, before, desired)
    _verify_inputs(args)
    owned_files.replace(ponytail.receipt_path(), old_record if restore else new_record, current_receipt)
    args.component_lock = lock_before if restore else lock_after
    _write_lock(args)


def _ponytail_recover(args, component, active, step):
    frozen = step["target"]
    if step["status"] == "verified":
        expected = (step["observed"] or {}).get("resolution") or "completed"
        if args.resolve != expected:
            raise CliError("Ponytail 事务已经核验为不同结果；保留现场")
        native = ponytail.Native(component, args.codex_home)
        state = native.observe()
        record, raw = ponytail.receipt(component)
        completed = expected == "completed"
        same_native = native.matches(state, frozen["desired"]) if completed else state == frozen["before"]
        expected_record = (ponytail.receipt_record(component, frozen["desired"])
                           if completed else frozen["receipt_before"])
        expected_lock = frozen["lock_after" if completed else "lock_before"]
        if (not same_native or record != expected_record or raw != owned_files.json_bytes(expected_record)
                or apply_journal.digest(args.component_lock_path) != expected_lock):
            raise CliError("Ponytail 已验证结果与现场不符；保留事务")
    elif args.resolve == "not-started":
        if (step["status"] != "pending" or frozen.get("restoring")
                or apply_journal.digest(args.component_lock_path) != frozen["lock_before"]
                or ponytail.receipt(component)[0] != frozen["receipt_before"]
                or ponytail.Native(component, args.codex_home).observe() != frozen["before"]):
            raise CliError("Ponytail 缺少未执行的完整证据；保留事务")
        active = apply_journal.verified(active, step["id"], {"resolution": "not-started", "desired_applied": False})
    else:
        restore = args.resolve == "restored"
        if frozen.get("restoring") and not restore:
            raise CliError("Ponytail 已开始精确回退，只能沿同一事务继续回退")
        if restore and not frozen.get("restoring"):
            frozen = {**frozen, "restoring": True}
            active = apply_journal.set_target(active, step["id"], frozen)
        if step["status"] == "pending":
            active = apply_journal.started(active, step["id"])
        try:
            _ponytail_apply(args, component, frozen, restore=restore)
        except BaseException:
            apply_journal.unknown(apply_journal.read(), step["id"])
            raise
        active = apply_journal.verified(apply_journal.read(), step["id"],
                                        {"resolution": args.resolve, "desired_applied": not restore})
    apply_journal.clear()
    _emit({"operation": "recover", "transaction": args.transaction, "step": args.step,
           "resolution": args.resolve, "complete": True}, args.json)
    return 0


def cmd_update(args: argparse.Namespace) -> int:
    if getattr(args, "resume", False) and not args.all:
        raise CliError("--resume 仅用于同一 update --all 事务")
    if getattr(args, "plan", False) and not args.all and args.component != "codex-config":
        raise CliError("--plan 仅适用于 update --all 或 codex-config")
    if getattr(args, "takeover", False):
        if (args.component != copilot_takeover.COMPONENT or args.version
                or args.all or args.review_system_changes):
            raise CliError("--takeover 仅用于已登记的 Copilot CLI 精确接管")
        args.kconf, _, catalog = _load(args)
        if args.component not in config_model.selected_components(args.kconf):
            raise CliError("Copilot CLI 未在 workflow.config 中选择")
        try:
            return copilot_takeover.command(args, catalog["components"][args.component], _emit, _verify_inputs,
                                            _start_transaction, _journal_mutate, _write_lock)
        except owner_engine.BootstrapError as error:
            raise CliError(str(error)) from error
    if getattr(args, 'all', False):
        if args.component or args.version or (args.review_system_changes and
                (getattr(args, "plan", False) or getattr(args, "confirm", None) is not None)):
            raise CliError('--all 不接受 component、--version；重审已有事务不能与 --plan 或 --confirm 混用')
        return cmd_update_all(args)
    if not args.component:
        raise CliError('必须指定组件或 --all')
    if getattr(args, 'review_system_changes', False):
        raise CliError('--review-system-changes 仅用于 --all 的已完成系统升级重审')
    kconf, config_path, catalog = _load(args)
    if args.component in catalog.get("paseo_plugins", {}):
        if getattr(args, "version", None):
            raise CliError("Paseo 插件的目标提交来自共享声明")
        args.plugin_action = "update"
        return cmd_paseo_plugins(args)
    if args.component == ponytail.KEY:
        if args.component not in config_model.selected_components(kconf):
            raise CliError("Ponytail 未在 workflow.config 中选择")
        return _ponytail_update(args, catalog["components"][args.component])
    if args.component == "codex-config":
        return _update_codex_catalog(args, kconf)
    if getattr(args, "confirm", None):
        raise CliError("--confirm 仅用于 Paseo 插件的更新摘要")
    args.kconf = kconf
    args.codex_home = Path(os.path.expanduser(config_model.config_value(kconf, "PENNIX_CODEX_HOME")))
    component = catalog["components"].get(args.component)
    if component is None or component.get("delivery") not in {"package", "plugin", "client", "collection"}:
        raise CliError(f"不支持更新的组件: {args.component}")
    if args.component not in config_model.selected_components(kconf):
        raise CliError(f"组件未在 workflow.config 中选择: {args.component}")
    if args.component == SCOPED_COLLECTION:
        _require_system_devel_tools()
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
    previous_main_needed = False
    if component.get("native_config"):
        try:
            package_state, package_observed = owner_engine.probe_component(component, args.codex_home)
            package_owned = owner_engine.package_owned(args.component, component, package_observed, args.codex_home)
        except owner_engine.BootstrapError as error:
            raise CliError(f"组件状态不允许更新: {error}") from error
        previous_main_needed = package_state == "blocked" and owner_engine.codegraph_main_owned(args.component, component)
        if package_state not in {"match", "outdated"} and not previous_main_needed:
            raise CliError(f"组件状态不允许更新: {package_state}")
        if not package_owned and not previous_main_needed:
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
            if previous_main_needed:
                previous_dir = Path(directory) / "previous-main"
                previous_dir.mkdir()
                candidate["previous_main"] = candidates.codegraph_previous_main(component, previous_dir, entry)
                owner_engine.verify_npm_archive(owner_engine._prefix(component) / "lib/node_modules/@pennixrv/codegraph",
                                               Path(candidate["previous_main"]["path"]))
        except (candidates.CandidateError, owner_engine.BootstrapError) as error:
            raise CliError(str(error)) from error
        target = candidates.lock_entry(candidate)
        if args.component == "paseo-cli":
            paseo_service.preflight_candidate(candidate, plugins_required=bool(paseo_plugins.profile_path(kconf)))
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
            steps.extend(_connection_update_steps(args, catalog))
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
        active = _configure_updated_connections(args, catalog, active)
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


def _update_codex_catalog(args, kconf):
    args.kconf = kconf
    if args.version is not None or args.review_system_changes:
        raise CliError("静态 Codex 配置不接受包版本或系统事务重审")
    if "codex-config" not in config_model.selected_components(kconf):
        raise CliError("Codex 配置未在 workflow.config 中选择")
    active = apply_journal.read()
    checkpoint = None
    if active is not None:
        checkpoint = next((step for step in active["steps"] if step["id"] == "codex-config:catalog-review"), None)
        if (active["command"] != "update-all" or checkpoint is None or checkpoint["status"] != "pending"
                or any(step["status"] != "verified" for step in active["steps"][:active["steps"].index(checkpoint)])
                or any(step["status"] in {"started", "unknown"} for step in active["steps"])):
            raise CliError("未结事务不是已就绪的目录审阅检查点；先按同次 journal 核验，不重放")
        if (active["config_digest"] != apply_journal.digest(args.config_path)
                or active["lock_digest"] != apply_journal.digest(args.component_lock_path)):
            raise CliError("目录检查点的配置或锁改变；保留同次事务")
        parent = next((step["target"] for step in active["steps"] if step["id"] == "update-review"), None)
        if parent is None:
            raise CliError("目录检查点缺少原始全量审阅")
        args.update_review = parent
        _verify_inputs(args)
    path = args.codex_home / "config.toml"
    values = config_model.values(kconf)
    digest = getattr(args, "confirm", None)
    if getattr(args, "plan", False) and digest is not None:
        raise CliError("静态配置 --plan 与 --confirm 互斥")
    if digest is None:
        review = codex_config.prepare_catalog_review(path, values, args.component_lock_path)
        if getattr(args, "plan", False):
            _emit(review, args.json)
            return 0
        if review["status"] == "no-op":
            codex_config.reviewed_catalog(path, values, args.component_lock_path, review["review_sha256"])
            if checkpoint:
                finished = apply_journal.verified(active, step_id=checkpoint["id"], observed={
                    "catalog_source": "fresh-native-verified", "source_sha256": review["source_sha256"]})
                if apply_journal.complete(finished):
                    apply_journal.clear()
                else:
                    review.update(transaction=active["transaction"], next="pennix update --all --resume")
            _emit(review, args.json)
            return 0
        _emit(review, args.json)
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise CliError("静态配置更新需精确确认；使用 update codex-config --plan 后 --confirm <摘要>")
        if input("输入上面的 review_sha256 以提交模型目录: ").strip() != review["review_sha256"]:
            return 1
        digest = review["review_sha256"]
    candidate = codex_config.reviewed_catalog(path, values, args.component_lock_path, digest)
    target = {"review_sha256": digest, "catalog": candidate["descriptor"]}
    step_id = checkpoint["id"] if checkpoint else "codex-config:configure"
    journal = (apply_journal.set_target(active, step_id, target) if checkpoint else
               _start_transaction(args, "update", "codex-config", [{
                   "id": step_id, "component": "codex-config", "action": "configure", "target": target}]))
    journal, result = _journal_mutate(journal, step_id,
        lambda: codex_config.apply(path, values, catalog_candidate=candidate))
    complete = apply_journal.complete(journal)
    if complete:
        apply_journal.clear()
    _emit({"operation": "update", "component": "codex-config", "status": result,
           "review_sha256": digest, "catalog": candidate["descriptor"],
           **({"transaction": journal["transaction"], "next": "pennix update --all --resume"}
              if not complete else {})}, args.json)
    return 0


def _catalog_checkpoint(args, journal, step):
    if (journal["command"] != "update-all" or step["status"] != "pending"
            or any(item["status"] != "verified" for item in journal["steps"][:journal["steps"].index(step)])):
        raise CliError("目录源审阅必须在同次已批准依赖核验后，且尚无目录写入副作用")
    values = config_model.values(args.kconf)
    review = codex_config.prepare_catalog_review(args.codex_home / "config.toml", values, args.component_lock_path)
    if review["status"] == "no-op":
        codex_config.reviewed_catalog(args.codex_home / "config.toml", values, args.component_lock_path,
                                     review["review_sha256"])
        apply_journal.verified(journal, step["id"], {"catalog_source": "fresh-native-verified",
                                                 "source_sha256": review["source_sha256"]})
        return None
    _emit({**review, "status": "catalog-review-required", "transaction": journal["transaction"],
           "step": step["id"], "next": "pennix update codex-config --confirm <review_sha256>"}, args.json)
    return 2


def _reverse_uninstall_order(catalog: dict[str, Any]) -> list[str]:
    return [key for key in reversed(APPLY_ORDER)
            if key in catalog["components"]]


def cmd_uninstall(args: argparse.Namespace) -> int:
    kconf, _, catalog = _load(args)
    registered_plugins = paseo_plugins.sources(catalog)
    if registered_plugins and (args.all or args.component == "paseo-cli") and (
            registered_plugins.keys() & args.component_lock["components"].keys()):
        raise CliError("先通过 pennix paseo-plugins 解除接管或卸载插件，再卸载 Paseo CLI")
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
        owned_connections = {}
        for target, spec in catalog["configuration_targets"].items():
            status = connection_owners.inspect_target(target, spec, args.codex_home, enabled=False,
                target_home=Path(config_model.config_value(kconf, "PENNIX_COPILOT_HOME")) if spec.get("host") == "copilot-cli" else None)
            if spec.get("host") == "copilot-cli" and status["state"] == "blocked":
                raise CliError(f"全量卸载无法核验 Copilot 连接归属: {target}")
            if status.get("owned"):
                if status["state"] not in ({"current", "orphaned", "update-required"} if target == 'cch-tmux' else {"current", "orphaned"}):
                    raise CliError(f"全量卸载预检发现连接漂移: {target}")
                connection_owners.validate_target(target, spec, kconf, args.codex_home,
                    Path(config_model.config_value(kconf, "PENNIX_SECRETS_FILE")), "unconfigure")
                owned_connections[target] = status
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
        elif component["delivery"] == "client":
            state, detail = owner_engine.local_clients.inspect(args.component, component)
            if state not in {"match", "outdated", "missing"}:
                raise CliError(f"卸载预检无法证明客户端归属: {detail}")
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
                   "action": "unconfigure", "target": {'expected_sha256': owned_connections[target]['fingerprint']}
                   if target == 'cch-tmux' else {}} for target in owned_connections]
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
                    operation_id=f"{active.get('transaction', 'legacy')}:{step['id']}",
                    expected_sha256=step['target'].get('expected_sha256'))
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


def cmd_secrets_paseo_session_insights(args: argparse.Namespace) -> int:
    if not args.private_stdout or sys.stdout.isatty():
        raise CliError("会话指标配置只能通过私有子进程管道读取")
    config, _ = config_model.load(args.config)
    def value(key):
        return config_model.config_value(config, key)

    home = Path(args.paseo_home)
    if not home.is_absolute() or home != Path(value("PENNIX_PASEO_HOME")):
        raise CliError("Paseo 家目录与 Lifecycle 配置不一致")
    is_sub2api = args.source == "sub2api"
    source = "Sub2API" if is_sub2api else "CCHP"
    option = f"PENNIX_PASEO_SESSION_INSIGHTS_{args.source.upper()}"
    if value(option) != "y":
        return 0

    url = value(f"{option}_URL")
    try:
        endpoint = urlsplit(url)
        port = endpoint.port
    except ValueError as error:
        raise CliError(f"{source} 控制面地址无效") from error
    if (len(url) > 2048 or re.search(r"[\s\\?#]", url)
            or endpoint.scheme != "https" or not endpoint.hostname or endpoint.username is not None
            or endpoint.path not in ("", "/") or port == 0):
        raise CliError(f"{source} 控制面地址必须是 HTTPS 源地址")
    secrets_path = Path(value("PENNIX_SECRETS_FILE"))
    if is_sub2api:
        account_id = int(value(f"{option}_ACCOUNT_ID"))
        if not 0 < account_id <= 9007199254740991:
            raise CliError("Sub2API 账号 ID 必须为正整数")
        key = secret_store.load(secrets_path).get("sub2api", {}).get("account_read_key") if secrets_path.exists() else None
        if not key:
            json.dump({"state": "missing-key"}, sys.stdout)
            return 0
        if not re.fullmatch(r"adm_sc_[A-Za-z0-9_-]{1,256}", key):
            raise CliError("Sub2API 受限管理员 Key 格式无效")
        json.dump({"baseUrl": url, "accountId": account_id, "token": key}, sys.stdout, separators=(",", ":"))
        return 0

    key_id = value(f"{option}_KEY_ID")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", key_id):
        raise CliError("CCHP 推理密钥 ID 无效")
    token = secret_store.get(secrets_path, "cchp", "usage_token")
    if not re.fullmatch(r"[!-~]{1,4096}", token):
        raise CliError("CCHP 用量令牌格式无效")
    json.dump({"baseUrl": url, "apiKeyId": key_id, "token": token}, sys.stdout, separators=(",", ":"))
    return 0


def cmd_secrets_import_ssh(args: argparse.Namespace) -> int:
    kconf, config_path = config_model.load(args.config)
    before = kconf.snapshot
    path = Path(config_model.config_value(kconf, "PENNIX_SECRETS_FILE"))
    try:
        if args.field:
            if args.service:
                raise CliError("--field 与 --service 不能混用")
            values = credential_import.read_from_lifecycle(args.alias, args.field)
        elif args.service:
            values = credential_import.read_from_ssh(args.alias, args.service)
        else:
            raise CliError("必须选择中央库 --field 或原生 owner --service")
        if owned_files.read(config_path, private=True) != before:
            raise CliError("配置在凭据传输期间改变；未写入秘密")
        updated = secret_store.update(path, values)
    except credential_import.CredentialImportError as error:
        raise CliError(str(error)) from error
    _emit({"operation": "secrets-import-ssh", "updated": sorted(updated)}, args.json)
    return 0


def cmd_secrets_check(args: argparse.Namespace) -> int:
    kconf, _ = config_model.load(args.config)
    catalog = owner_engine.load_catalog(CATALOG)
    required = credential_import.required_fields(kconf, catalog)
    path = Path(config_model.config_value(kconf, "PENNIX_SECRETS_FILE"))
    if not required and not path.exists() and not path.is_symlink():
        _emit({'state': 'current', 'required': [], 'missing': []}, args.json)
        return 0
    try:
        values = secret_store.load(path)
    except secret_store.SecretError as error:
        _emit({"state": "blocked", "required": required, "detail": str(error)}, args.json)
        return 2
    missing = [name for name in required if not values.get(name.split('.')[0], {}).get(name.split('.')[1])
               or any(c in values[name.split('.')[0]][name.split('.')[1]] for c in '\r\n')]
    _emit({"state": "current" if not missing else "blocked", "required": required, "missing": missing}, args.json)
    return 2 if missing else 0


def cmd_secrets_export(args: argparse.Namespace) -> int:
    if not args.private_stdout or sys.stdout.isatty():
        raise CliError("私有导出仅允许显式 --private-stdout 的机器管道；不得打印到终端或 Agent 输出")
    kconf, _ = config_model.load(args.config)
    values = secret_store.select(Path(config_model.config_value(kconf, "PENNIX_SECRETS_FILE")), args.field)
    sys.stdout.write(json.dumps({"schema": 1, "fields": values}) + '\n')
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
    elif args.tool_command == 'update':
        target = release_owner.latest()
        current = release_owner._version(ROOT)
        if tuple(map(int, target['version'].split('.'))) < tuple(map(int, current.split('.'))):
            raise CliError('latest 比当前工具更旧；拒绝隐式降级')
        if args.plan or args.confirm is not None:
            review = _tool_update_review(target, current)
            if args.plan:
                _emit(review, args.json)
                return 0
            if review.get("review_sha256") != args.confirm:
                raise CliError("Lifecycle 审阅摘要不匹配；请重新执行 pennix tool update --plan")
        else:
            _emit({'installed': current, 'available': target, 'next': '切换后以新进程运行 pennix updates --json'}, args.json)
            if target['version'] == current:
                return 0
        if args.confirm is None and (
                not sys.stdin.isatty() or not sys.stdout.isatty() or input('确认升级 Lifecycle 工具？输入 yes: ').strip() != 'yes'):
            return 1
        if release_owner.latest() != target:
            raise CliError('确认期间公开发行改变；请重新审阅')
        if args.confirm is not None and _tool_update_review(target, current) != review:
            raise CliError('确认期间 Lifecycle 目标或实装身份改变；请重新审阅')
        result = release_owner.update(target)
    else:
        if not args.yes:
            raise CliError("工具自身卸载需要显式 --yes")
        result = release_owner._uninstall()
    _emit({"tool": result, "shared_path": "preserved for retained tools"}, args.json)
    return 0


def _review_target() -> str:
    def identifier(path, pattern):
        value = Path(path).read_text(encoding="ascii").strip()
        if not re.fullmatch(pattern, value):
            raise CliError("非交互审阅需要可核验的宿主身份")
        return value

    return hashlib.sha256(owned_files.json_bytes({
        "machine": identifier("/etc/machine-id", r"[0-9a-f]{32}"),
        "boot": identifier("/proc/sys/kernel/random/boot_id", r"[0-9a-f-]{36}"),
        "namespace": Path("/proc/self/ns/mnt").stat().st_ino,
        "uid": os.getuid(), "home": str(Path.home()),
        "state_root": str(owned_files.state_root()),
    })).hexdigest()


def _tool_update_review(target, current):
    if apply_journal.read() is not None or owned_files.read(release_owner._switch_path(), private=True) is not None:
        raise CliError("已有 Lifecycle 事务或发行切换未结束；沿原入口核验恢复")
    releases, entry = release_owner._locations()
    active = release_owner._active_target(entry, releases)
    if active is None or active != ROOT or active.name != current:
        raise CliError("请从已安装且归属可核验的 pennix 入口审阅与确认")
    binding = _review_target()
    review = {"operation": "tool-update", "installed": current, "available": target,
              "target_id": binding[:16], "impact": "仅切换当前用户的 Lifecycle 发行；不升级系统包",
              "next": "切换后以新进程运行 pennix updates --json"}
    if target['version'] == current:
        return {**review, "status": "no-op"}
    payload = {"schema": 1, "review": review, "binding": binding, "entry": str(entry),
               "active_digest": release_owner._installed_digest(active)}
    return {**review, "status": "review-required",
            "review_sha256": hashlib.sha256(owned_files.json_bytes(payload)).hexdigest()}


def cmd_recover(args: argparse.Namespace) -> int:
    active = apply_journal.read()
    if not args.resolve:
        public = None if active is None else {**active, "steps": [
            {**step, "target": {key: val for key, val in step["target"].items() if key != "secrets_digest"}}
            if step["id"] == "update-review" else step for step in active["steps"]
        ]}
        _emit({"transaction": public, "state": "pending" if active else "idle"}, args.json)
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
    step = apply_journal.step(active, args.step)
    if (active["command"] == "update" and active["component"] == ponytail.KEY
            and len(active["steps"]) == 1 and step["component"] == ponytail.KEY
            and step["id"] == f"{ponytail.KEY}:release" and step["action"] == "pin-release"):
        return _ponytail_recover(args, catalog["components"][ponytail.KEY], active, step)
    review = next((item["target"] for item in active["steps"] if item["id"] == "update-review"), {})
    reviewed_all = active["command"] == "update-all" and review.get("mode") == "noninteractive-v1"
    arch_record = (reviewed_all and args.resolve == "completed" and step["id"] == "system-upgrade:record"
                   and step["component"] == "system" and step["action"] == "record")
    lock_digest = apply_journal.digest(args.component_lock_path)
    lock_checked = (arch_record and step["status"] in {"started", "unknown"}
                    and bool(step["target"].get("lock_after"))
                    and step["target"] == review.get("arch_record") and lock_digest == step["target"]["lock_after"])
    if active["lock_digest"] != lock_digest and not lock_checked:
        raise CliError("锁文件与事务最后已证实的检查点不同；保留现场，不推断外部修改归属")
    if active["command"] == "apply" and active["component"] == system_devel_tools.SCOPE:
        return _recover_scoped_tools(args, catalog, active, step)
    if step["component"] == "codex-config" and step["target"].get("codex_config_protocol") == 1:
        evidence = codex_config.recover(home / "config.toml", active, step, args.resolve)
        active = apply_journal.verified(apply_journal.read(), step["id"], evidence)
        if apply_journal.complete(active):
            apply_journal.clear()
        _emit({"operation": "recover", "component": "codex-config", **evidence}, args.json)
        return 0
    if args.resolve in {"not-started", "restored"}:
        required = "none" if args.resolve == "not-started" else "restored"
        if step['component'] == 'pnpm' and args.resolve == 'restored' and step['status'] != 'verified' and pnpm_migration.pending():
            args.kconf, args.codex_home = kconf, home
            previous = pnpm_migration.restore(_args_for_engine(args, 'install'), catalog, catalog['components']['pnpm'])
            args.component_lock = component_lock.set_entry(args.component_lock, 'pnpm', previous, args.component_lock_sources)
            _write_lock(args)
            pnpm_migration.finish_restore(previous)
            active = apply_journal.rejected(apply_journal.read(), step['id'], 'restored')
            step = apply_journal.step(active, args.step)
        if step["component"].startswith("connection:") and args.resolve == "restored":
            target = step["component"].split(":", 1)[1]
            spec = catalog["configuration_targets"].get(target, {})
            if target == 'cch-tmux':
                evidence = connection_owners._run(connection_owners._command(spec, home), 'restore',
                    {'target': target, 'operation_id': f"{active['transaction']}:{step['id']}"}, codex_home=home)
                if evidence.get('effect') in {'none', 'restored'}:
                    active = apply_journal.rejected(active, step['id'], 'restored')
                    step = apply_journal.step(active, args.step)
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
        elif (active["command"] == "update-all" and step["id"] == "system-upgrade"
              and step["component"] == "system" and step["action"] == "upgrade"):
            evidence = (arch_recovery.observe(active["transaction"], step["target"])
                        if reviewed_all and review.get("system") == step["target"]
                        and step["status"] in {"started", "unknown"} else None)
            if evidence is None:
                raise CliError("缺少同一 Arch 提交的原生完成回执或当前包状态不符；保留现场，不重跑包事务")
            active = apply_journal.verified(active, step["id"], evidence)
        elif arch_record:
            package = apply_journal.step(active, "system-upgrade")
            if (not review.get("arch_record") or step["target"] != review["arch_record"]
                    or package["target"] != review.get("system")
                    or package["status"] != "verified"
                    or (package.get("observed") or {}).get("state") != "native-commit-verified"
                    or review.get("catalog_digest") != apply_journal.digest(CATALOG)
                    or arch_recovery.observe(active["transaction"], package["target"]) is None):
                raise CliError("没有同一 Arch 提交及组件归属的证据；不补写记录或重跑包事务")
            _record_reviewed_arch(args, catalog, package["target"], step["target"])
            active = apply_journal.verified(apply_journal.read(), step["id"], {"recovered": True})
        elif step["component"] == "paseo-relay" and step["action"] == "adopt-current" and active["command"] == "repair":
            record = relay_owner._record()
            paseo_home = Path(config_model.config_value(kconf, "PENNIX_PASEO_HOME"))
            review = relay_owner.review(paseo_home,
                nas_ssh=config_model.config_value(kconf, "PENNIX_RELAY_NAS_SSH"),
                router_ssh=config_model.config_value(kconf, "PENNIX_RELAY_ROUTER_SSH"))
            if (not record or record["schema"] != 2 or record["server_id"] != review["server_id"]
                    or record["target"] != review["target"] or review["state"] != "present"
                    or review["review_sha256"] != step["target"].get("review_sha256")):
                raise CliError("relay 当前登记接管后置条件不符；保留 unknown")
            active = apply_journal.verified(active, step["id"], {"recovered": True, "state": "owned-current-registration"})
        elif (active['command'] == 'apply' and step['component'] == 'paseo-relay'
                and step['action'] in {'install','configure'} and step['status'] in {'unknown','started'}):
            paseo_home = Path(config_model.config_value(kconf, 'PENNIX_PASEO_HOME'))
            paseo = Path(shutil.which('paseo') or Path.home()/'.local/bin/paseo')
            if config_model.config_value(kconf, 'PENNIX_PASEO_RELAY_REGISTER') == 'y':
                record = relay_owner._record()
                if (not record or record['server_id'] != relay_owner._server_id(paseo_home)
                        or relay_owner.inspect(paseo_home,
                            nas_ssh=config_model.config_value(kconf, 'PENNIX_RELAY_NAS_SSH'),
                            router_ssh=config_model.config_value(kconf, 'PENNIX_RELAY_ROUTER_SSH')) != 'present'):
                    raise CliError('relay 远端登记的同身份归属未证明；保留 unknown')
            endpoint = config_model.config_value(kconf, 'PENNIX_PASEO_RELAY_ENDPOINT')
            public_endpoint = config_model.config_value(kconf, 'PENNIX_PASEO_RELAY_PUBLIC_ENDPOINT')
            tls = bool(kconf.syms['PENNIX_PASEO_RELAY_TLS'].tri_value)
            public_tls = bool(kconf.syms['PENNIX_PASEO_RELAY_PUBLIC_TLS'].tri_value)
            relay_owner.configure_daemon(paseo, paseo_home, endpoint, public_endpoint, tls, public_tls)
            if (not relay_owner.daemon_owned(paseo_home) or relay_owner.inspect_daemon(
                    paseo, paseo_home, endpoint, public_endpoint, tls, public_tls) != 'current'):
                raise CliError('relay 本地配置归属或后置条件未证明；保留 unknown')
            active = apply_journal.verified(active, step['id'], {'recovered':True, 'state':'current'})
        elif step["component"].startswith("connection:"):
            target = step["component"].split(":", 1)[1]
            spec = catalog["configuration_targets"].get(target)
            if spec is None:
                raise CliError("恢复目标未登记")
            if target == 'cch-tmux':
                connection_owners._run(connection_owners._command(spec, home), 'complete',
                    {'target': target, 'operation_id': f"{active['transaction']}:{step['id']}"}, codex_home=home)
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
        elif step["component"] == "pnpm" and pnpm_migration.needed() and step["target"]:
            component["_lock_target"] = step["target"]
            args.kconf, args.codex_home = kconf, home
            with tempfile.TemporaryDirectory(prefix="pennix-pnpm-recover-") as directory:
                candidate = candidates.resolve(component, Path(directory), step["target"])
                _run_component(args, catalog, "pnpm", "install", candidate=candidate)
            args.component_lock = component_lock.set_entry(args.component_lock, "pnpm", step["target"], args.component_lock_sources)
            _write_lock(args)
            pnpm_migration.finish(component, step["target"])
            active = apply_journal.verified(apply_journal.read(), step["id"], {"recovered": True, "state": "match"})
        elif step["component"] == "base-environment" and step["action"] == "uninstall":
            base_environment.uninstall()
            if base_environment.receipt() is not None:
                raise CliError("基础撤回 owner 未证明完成")
            active = apply_journal.verified(active, step["id"], {"recovered": True, "state": "absent"})
        elif step["component"] == "base-environment" and step["action"] in {"install", "upgrade"}:
            recover_missing_receipt = (
                active["command"] == "apply" and step["action"] == "install"
                and step["status"] in {"started", "unknown"}
            )
            if base_environment.receipt() is None and not recover_missing_receipt:
                raise CliError("基础环境没有同目标 owner receipt；不能释放 unknown")
            base_environment.apply(kconf, catalog, package_target=step["target"].get("packages"),
                                   recover_missing_receipt=recover_missing_receipt)
            if base_environment.inspect(kconf)["state"] != "current":
                raise CliError("基础环境 owner 未证明完成")
            active = apply_journal.verified(active, step["id"], {"recovered": True, "state": "current"})
        elif component and step["action"] in {"install", "upgrade", "adopt", "adopt-upgrade"} and step["target"]:
            component["_lock_target"] = step["target"]
            args.kconf, args.codex_home = kconf, home
            retry_arch = (not reviewed_all and step["action"] == "install"
                          and component.get("package", {}).get("source") in {"official", "aur"}
                          and owner_engine.package_absent(component))
            retry_plugin = (
                active["command"] == "apply" and step["action"] == "install"
                and step["status"] in {"started", "unknown"} and component["delivery"] == "plugin"
                and owner_engine._plugin_receipt(step["component"], component) is None
                and owner_engine.probe_component(component, home)[0] == "missing"
            )
            if retry_arch or retry_plugin:
                if retry_arch and not any(item["id"] == "system-upgrade" and item["status"] == "verified" for item in active["steps"]):
                    raise CliError("缺失的 Arch 包恢复须有同次完整系统升级证据；保留 unknown")
                with tempfile.TemporaryDirectory(prefix="pennix-recover-install-") as directory:
                    candidate = candidates.resolve(component, Path(directory), step["target"])
                    _run_component(args, catalog, step["component"], "install", candidate=candidate)
            if (active["command"] == "update" and active["component"] == step["component"]
                    and step["action"] == "upgrade" and step["status"] in {"started", "unknown"}
                    and component["delivery"] == "client"):
                key = step["component"]
                state, _ = owner_engine.local_clients.inspect(key, component)
                if state == "outdated":
                    previous = args.component_lock["components"].get(key, {})
                    if (not {"ref", "sha256"} <= previous.keys() or not {"ref", "sha256"} <= step["target"].keys()
                            or owner_engine.local_clients.inspect(key, {**component, "_lock_target": previous})[0] != "match"):
                        raise CliError("旧版客户端与事务锁检查点不符；保留 unknown")
                    with tempfile.TemporaryDirectory(prefix="pennix-recover-client-") as directory:
                        candidate = candidates.resolve(component, Path(directory), step["target"])
                        if candidates.lock_entry(candidate) != step["target"]:
                            raise CliError("客户端恢复候选与事务冻结目标不符；保留 unknown")
                        _run_component(args, catalog, key, "upgrade", candidate=candidate)
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
                    owner_engine.recover_npm_receipt(step["component"], component, old, Path(candidate["path"]),
                        **({"platform_archive": Path(candidate["platform_path"])} if "platform_path" in candidate else {}))
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


def _recover_scoped_tools(args, catalog, active, step):
    package_step = system_devel_tools.STEP
    record_step = f"{package_step}:record"
    if [item["id"] for item in active["steps"]] != [package_step, record_step]:
        raise CliError("宿主工具事务结构不匹配；保留现场")
    provision, record = active["steps"]
    target = provision["target"]
    if (args.resolve == "not-started" and step["id"] == package_step
            and provision["status"] == record["status"] == "pending"
            and system_devel_tools.installed_packages() == target["before"]):
        active = apply_journal.verified(active, package_step, {"effect": "not-started"})
        active = apply_journal.verified(active, record_step, {"effect": "not-required"})
    elif args.resolve == "completed" and step["id"] == record_step and provision["status"] == "verified":
        effect = (provision["observed"] or {}).get("effect")
        if effect == "reviewed-packages-installed":
            _finish_scoped_tools(args, catalog, target)
            active = apply_journal.verified(apply_journal.read(), record_step, {"recovered": True})
        elif (effect in {"not-started", "no-package-change"}
                and system_devel_tools.installed_packages() == target["before"]):
            active = apply_journal.verified(active, record_step, {"effect": "not-required"})
        else:
            raise CliError("未证明原生包事务结果；保留现场")
    else:
        raise CliError("原生包事务未留下可核验的成功回执；不重放、不凭版本推断，按已验收恢复点处置")
    if not apply_journal.complete(active):
        raise CliError("宿主工具事务仍有未核验步骤；保留现场")
    apply_journal.clear()
    _emit({"operation": "recover", "scope": system_devel_tools.SCOPE, "state": "idle"}, args.json)
    return 0


def cmd_paseo_plugins(args: argparse.Namespace) -> int:
    try:
        return paseo_plugins.command(args, _load, _verify_inputs, _write_lock, _start_transaction, _emit)
    except paseo_plugins.PluginError as error:
        raise CliError(str(error)) from error


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pennix", description="Pennix 工作流生命周期工具")
    sub = p.add_subparsers(dest="command", required=True)
    paseo_plugins.add_parser(sub, cmd_paseo_plugins)
    recover = sub.add_parser("recover", help="只读展示或按 owner 证据恢复当前事务")
    recover.add_argument("--transaction")
    recover.add_argument("--step")
    recover.add_argument("--resolve", choices=["not-started", "completed", "restored"])
    recover.add_argument("--config")
    recover.add_argument("--json", action="store_true")
    recover.set_defaults(function=cmd_recover)

    config = sub.add_parser("config", help="管理 workflow.config")
    config_sub = config.add_subparsers(dest="config_command", required=True)
    for name, function in (("init", cmd_config_init), ("edit", cmd_config_edit), ("check", cmd_config_check), ("policy", cmd_config_policy)):
        child = config_sub.add_parser(name)
        child.add_argument("--config")
        child.add_argument("--json", action="store_true")
        if name == "init":
            child.add_argument("--force", action="store_true")
            child.add_argument("--defconfig", help="从严格校验的 defconfig 生成 workflow.config")
            child.add_argument("--profile", choices=["development"], help="显式合并基础能力及共享依赖，不取消已有选择")
            child.add_argument("--git-name", help="仅向目标机绑定 Git 姓名，不复制认证")
            child.add_argument("--git-email", help="仅向目标机绑定 Git 邮箱，不复制认证")
        child.set_defaults(function=function)

    for name, function in (("plan", cmd_plan), ("status", cmd_status), ("check", cmd_check), ("apply", cmd_apply)):
        child = sub.add_parser(name)
        child.add_argument("--config")
        child.add_argument("--json", action="store_true")
        scope = child.add_mutually_exclusive_group()
        scope.add_argument("--scope", choices=["base", system_devel_tools.SCOPE],
                           help="仅处理基础能力，或只处理随系统开发 Skill 选择启用的宿主工具")
        if name in {"plan", "apply"}:
            scope.add_argument("--component", choices=[SCOPED_COLLECTION], help="只计划或首装指定文档型集合")
        child.set_defaults(function=function)

    repair = sub.add_parser("repair")
    repair.add_argument("component")
    repair.add_argument("--config")
    repair.add_argument("--json", action="store_true")
    repair.add_argument("--expected-sha256", help="仅当目标文件与已审阅的完整摘要相符时修复或释放配置归属")
    repair.set_defaults(function=cmd_repair)

    update = sub.add_parser("update", help="明确升级一个已选择且受管的组件")
    update.add_argument("component", nargs='?')
    update.add_argument('--all', action='store_true', help='统一审阅并在同一 native journal 更新所选组件')
    update.add_argument('--resume', action='store_true', help='继续同一已核验全量事务，不重试 unknown')
    update.add_argument('--review-system-changes', action='store_true', help='重审同一 --all 事务中已证实系统升级的额外变化；不释放 unknown')
    update.add_argument("--version", help="指定 npm 组件的精确版本；默认仍跟踪登记通道")
    update.add_argument("--takeover", action="store_true", help="仅预览或确认接管已审阅的外部 Copilot CLI 安装")
    update_review = update.add_mutually_exclusive_group()
    update_review.add_argument("--plan", action="store_true", help="审阅 --all 精确步骤或 codex-config 冻结候选，不写活动资产")
    update_review.add_argument("--confirm", help="执行全量升级、静态 Codex 目录、Ponytail、Paseo 插件或 Copilot 接管的精确摘要")
    update.add_argument("--config")
    update.add_argument("--json", action="store_true")
    update.set_defaults(function=cmd_update)
    aur = sub.add_parser("aur-prepare", help="审阅及隔离构建已选 AUR 工件，不安装软件")
    aur.add_argument("component")
    aur_mode = aur.add_mutually_exclusive_group(required=True)
    aur_mode.add_argument("--plan", action="store_true")
    aur_mode.add_argument("--confirm", help="精确构建审阅摘要")
    aur.add_argument("--config")
    aur.add_argument("--json", action="store_true")
    aur.set_defaults(function=cmd_aur_prepare)
    updates = sub.add_parser('updates', help='只读查询安装、锁、登记来源候选及完整系统升级影响')
    updates.add_argument('--config')
    updates.add_argument('--json', action='store_true')
    updates.set_defaults(function=cmd_updates)

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
    update_tool = tool_sub.add_parser('update', help='从固定公开发行更新自身；切换后必须重新审阅组件')
    update_tool.add_argument('--json', action='store_true')
    tool_review = update_tool.add_mutually_exclusive_group()
    tool_review.add_argument("--plan", action="store_true", help="只读审阅当前工具发行候选")
    tool_review.add_argument("--confirm", help="执行同一目标与候选的审阅摘要")
    update_tool.set_defaults(function=cmd_tool)
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
    insights_secret = secrets_sub.add_parser("paseo-session-insights", help="供 Paseo 插件服务端通过私有管道读取来源配置")
    insights_secret.add_argument("--source", choices=("cchp", "sub2api"), default="cchp")
    insights_secret.add_argument("--config")
    insights_secret.add_argument("--paseo-home", required=True)
    insights_secret.add_argument("--private-stdout", action="store_true")
    insights_secret.set_defaults(function=cmd_secrets_paseo_session_insights)
    secret_import = secrets_sub.add_parser("import-ssh", help="从既有 SSH 别名读取已登记凭据文件")
    secret_import.add_argument("--alias", required=True)
    secret_import.add_argument("--service", action="append", choices=sorted(credential_import.SOURCES))
    secret_import.add_argument("--field", action="append", help="从来源 Lifecycle 中央库选择 table.field")
    secret_import.add_argument("--config")
    secret_import.add_argument("--json", action="store_true")
    secret_import.set_defaults(function=cmd_secrets_import_ssh)
    secret_check = secrets_sub.add_parser("check", help="仅检查所选配置需要的字段，不返回值")
    secret_check.add_argument("--config")
    secret_check.add_argument("--json", action="store_true")
    secret_check.set_defaults(function=cmd_secrets_check)
    secret_export = secrets_sub.add_parser("export", help="仅供可信 SSH 子进程私有管道，不得捕获到 Agent 上下文")
    secret_export.add_argument("--config")
    secret_export.add_argument("--field", action="append", required=True)
    secret_export.add_argument("--private-stdout", action="store_true")
    secret_export.set_defaults(function=cmd_secrets_export)

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
            args.command in {"apply", "repair", "update", "aur-prepare", "reconcile", "uninstall", "tool"}
            or args.command == "secrets" and args.secrets_command in {"edit", "import-ssh"}
            or args.command == "recover" and args.resolve is not None
            or (args.command == "config" and args.config_command in {"init", "edit"})
            or (args.command == "cache" and args.cache_command == "clean" and args.yes)
            or args.command == "paseo-plugins" and bool(getattr(args, "confirm", None))
        )
        if mutating and not getattr(args, "plan", False):
            with owned_files.locked():
                if args.command in {"apply", "repair", "update", "reconcile", "uninstall", "paseo-plugins"} or args.command == "tool" and args.tool_command in {"install", "update"}:
                    current_host = owner_engine.host.detect_host()
                    if not current_host["supported"]:
                        raise CliError(str(current_host["reason"]))
                if args.command not in {"apply", "repair", "uninstall", "update", "reconcile", "recover", "paseo-plugins"} and apply_journal.read() is not None:
                    raise CliError("存在未完成 lifecycle 事务；先从原入口或 recover 恢复")
                return args.function(args)
        return args.function(args)
    except (CliError, arch_recovery.ArchCommitError, update_inventory.UpdateError, system_devel_tools.ToolError, base_environment.BaseError, candidates.CandidateError, apply_journal.JournalError, connection_owners.ConnectionError, config_model.ConfigError, codex_config.CodexConfigError, path_env.PathEnvError, paseo_service.PaseoServiceError, paseo_plugins.PluginError, ponytail.PonytailError, relay_owner.RelayError, release_owner.ReleaseError, skill_collections.CollectionError, owner_engine.local_clients.ClientError, secret_store.SecretError, owned_files.OwnershipError, native_integrations.NativeIntegrationError, native_integrations.hook_integrations.HookError, OSError) as error:
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
