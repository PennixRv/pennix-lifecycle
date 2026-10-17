#!/usr/bin/env python3
"""Pennix lifecycle command line interface."""

from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import _engine as owner_engine
import codex_config
import config_model
import path_env
import paseo_service


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
        if component_id in selected:
            action = "keep" if status["state"] == "match" else "install"
        else:
            action = "uninstall" if status["state"] == "match" else "keep"
        components[component_id] = {"selected": component_id in selected, "action": action, **status}
    prefix = Path(os.path.expanduser(config_model.config_value(kconf, "PENNIX_NPM_PREFIX")))
    paseo = prefix / "bin" / "paseo"
    if not paseo.is_file():
        paseo = Path(shutil.which("paseo") or paseo)
    mode = "linger" if kconf.syms["PENNIX_PASEO_AUTOSTART_LINGER"].tri_value else (
        "login" if kconf.syms["PENNIX_PASEO_AUTOSTART_LOGIN"].tri_value else "off"
    )
    service_state = paseo_service.inspect(paseo, Path.home() / ".paseo")
    service_action = "keep" if (mode == "off" and service_state == "absent") or (mode != "off" and service_state == "current") else (
        "uninstall" if mode == "off" else "install"
    )
    components["paseo-autostart"] = {
        "selected": mode != "off", "mode": mode, "action": service_action, "state": service_state,
    }
    if kconf.syms["PENNIX_PASEO_RELAY"].tri_value:
        components["paseo-relay"] = {
            "selected": True, "action": "blocked", "state": "blocked",
            "detail": "受信 SSH/nginx 登记 owner 尚未接入新生命周期工具",
        }
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
    engine_args = _args_for_engine(args, operation)
    try:
        if component.get("delivery") in {"static", "collection"}:
            return owner_engine.static_operation(engine_args, component_id, component, operation, catalog)
        return owner_engine.component_operation(engine_args, catalog, component_id, component, operation)
    except Exception as error:
        raise CliError(f"{component_id} {operation} 失败: {error}") from error


def cmd_config_init(args: argparse.Namespace) -> int:
    path = config_model.init(args.config, force=args.force)
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
    bad = [item for item in value["components"].values() if item["selected"] and item["state"] not in {"match", "current"}]
    return 1 if bad else 0


def cmd_apply(args: argparse.Namespace) -> int:
    value = _plan(args)
    _, _, catalog = _load(args)
    args.kconf, _, _ = _load(args)
    selected = {key for key, item in value["components"].items() if item["selected"]}
    relay = value["components"].get("paseo-relay")
    if relay and relay["selected"]:
        raise CliError("Paseo relay 已选择，但远端 nginx/SSH owner 尚未完成接入；未执行任何动作")
    for component_id in selected:
        if component_id in catalog["components"]:
            _ensure_dependencies(selected, component_id)
    if value["components"]["paseo-autostart"]["mode"] != "off" and "paseo-cli" not in selected:
        raise CliError("Paseo 自启动需要选择 paseo-cli")
    order = [
        "codex-cli", "trellis-cli", "pnpm", "fastctx", "codegraph-cli", "paseo-cli",
        "ponytail-plugin", "cch-status", "tmux", "tmux-config", "aoe", "tavily-hikari",
        "codex-config", "codex-agents", "pennix-skills-extra", "pennix-skills",
    ]
    results: dict[str, str] = {}
    for component_id in order:
        item = value["components"][component_id]
        if item["selected"] and item["action"] == "install":
            results[component_id] = _run_component(args, catalog, component_id, "install")
        elif item["selected"] and item["state"] in {"match", "current"} and config_model.config_value(args.kconf, "PENNIX_UPDATE_COMPONENTS") == "y":
            results[component_id] = _run_component(args, catalog, component_id, "upgrade")
        elif not item["selected"] and item["action"] == "uninstall":
            results[component_id] = _run_component(args, catalog, component_id, "uninstall")
    npm_selected = any(
        item["selected"]
        and isinstance(catalog["components"].get(key, {}).get("package"), dict)
        and catalog["components"][key]["package"].get("source") == "npm"
        for key, item in value["components"].items()
        if key in catalog["components"]
    )
    prefix = config_model.config_value(args.kconf, "PENNIX_NPM_PREFIX")
    results["zshrc-npm-path"] = path_env.apply(prefix) if npm_selected else path_env.remove()
    mode = value["components"]["paseo-autostart"]["mode"]
    paseo = prefix / "bin" / "paseo"
    if not paseo.is_file():
        paseo = Path(shutil.which("paseo") or paseo)
    try:
        results["paseo-autostart"] = paseo_service.apply(mode, paseo, args.codex_home.parent / ".paseo")
    except paseo_service.PaseoServiceError as error:
        raise CliError(str(error)) from error
    if config_model.config_value(args.kconf, "PENNIX_SYSTEM_UPGRADE") == "y":
        result = subprocess.run(["sudo", "pacman", "-Syu", "--noconfirm"], check=False)
        if result.returncode:
            raise CliError(f"完整系统升级失败: {result.returncode}")
        results["system-upgrade"] = "changed"
    _emit({"operation": "apply", "results": results}, args.json)
    return 0


def cmd_repair(args: argparse.Namespace) -> int:
    kconf, _, catalog = _load(args)
    args.kconf = kconf
    args.codex_home = Path(os.path.expanduser(config_model.config_value(kconf, "PENNIX_CODEX_HOME")))
    if args.component not in catalog["components"]:
        raise CliError(f"未知组件: {args.component}")
    result = _run_component(args, catalog, args.component, "upgrade")
    _emit({"operation": "repair", "component": args.component, "status": result}, args.json)
    return 0


def _reverse_uninstall_order(catalog: dict[str, Any]) -> list[str]:
    order = [
        "pennix-skills", "pennix-skills-extra", "codex-agents", "codex-config", "tavily-hikari",
        "aoe", "tmux-config", "tmux", "cch-status", "ponytail-plugin", "paseo-cli",
        "codegraph-cli", "fastctx", "pnpm", "trellis-cli", "codex-cli",
    ]
    return [key for key in order if key in catalog["components"]]


def cmd_uninstall(args: argparse.Namespace) -> int:
    kconf, _, catalog = _load(args)
    args.kconf = kconf
    args.codex_home = Path(os.path.expanduser(config_model.config_value(kconf, "PENNIX_CODEX_HOME")))
    if args.all:
        if args.confirm_all != "ALL":
            raise CliError("全量卸载必须使用 --confirm-all ALL")
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
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")).expanduser() / "pennix"
    path = base / "secrets.toml"
    base.mkdir(parents=True, mode=0o700, exist_ok=True)
    if not path.exists():
        path.write_text(
            "# 明文编辑，保存前请核对；此文件不会进入 JSON 输出或 Git。\n"
            "# [siyuan]\n# token = \"\"\n",
            encoding="utf-8",
        )
        path.chmod(0o600)
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
    result = subprocess.run([editor, str(path)], check=False)
    if result.returncode:
        raise CliError(f"编辑器退出码: {result.returncode}")
    print(path)
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

    secrets = sub.add_parser("secrets", help="编辑私有明文 secrets.toml")
    secrets_sub = secrets.add_subparsers(dest="secrets_command", required=True)
    secret_edit = secrets_sub.add_parser("edit")
    secret_edit.set_defaults(function=cmd_secrets_edit)

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
    except (CliError, config_model.ConfigError, codex_config.CodexConfigError, path_env.PathEnvError, paseo_service.PaseoServiceError) as error:
        print(f"pennix: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
