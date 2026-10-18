"""Install only the catalog's fixed local runtime files; preserve adjacent user data."""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
import shutil
import shlex
import subprocess
import tempfile

import owned_files as files
import skill_collections


class ClientError(RuntimeError):
    pass


def root(component: dict) -> Path:
    path = Path(os.path.expanduser(os.path.expandvars(component["client"]["root"])))
    files.safe_path(path)
    return path


def receipt_path(key: str) -> Path:
    return files.state_root() / "clients" / f"{key}.json"


def receipt(key: str, component: dict) -> dict | None:
    record = files.read_json(receipt_path(key))
    if record is None:
        return None
    if (not isinstance(record, dict) or set(record) != {"schema", "root", "source", "files", "commands"}
            or record["schema"] != 1 or record["root"] != str(root(component))
            or not isinstance(record["files"], dict) or not isinstance(record["commands"], dict)):
        raise ClientError(f"本地客户端归属记录无效: {key}")
    for name, metadata in record["files"].items():
        skill_collections.relative(name)
        if not isinstance(metadata, dict) or set(metadata) != {"digest", "mode"}:
            raise ClientError("客户端文件记录无效")
    return record


def _metadata(path: Path) -> dict | None:
    raw = files.read(path)
    return None if raw is None else {"digest": hashlib.sha256(raw).hexdigest(), "mode": path.stat().st_mode & 0o777}


def fingerprint(component: dict) -> str:
    target = root(component)
    current = {}
    for name in component["client"]["paths"]:
        path = target / name
        files.safe_path(path)
        selected = sorted(path.rglob("*")) if path.is_dir() else [path]
        for item in selected:
            if not item.is_dir():
                current[str(item)] = _metadata(item)
    for name in component["client"].get("commands", {}):
        path = Path.home() / ".local/bin" / name
        files.safe_path(path.parent)
        current[str(path)] = {"link": os.readlink(path)} if path.is_symlink() else _metadata(path)
    return hashlib.sha256(files.json_bytes(current)).hexdigest()


def inspect(key: str, component: dict) -> tuple[str, str]:
    try:
        record = receipt(key, component)
        target = root(component)
        if record is None:
            present = target.exists() or any(os.path.lexists(Path.home() / ".local/bin" / name)
                                             for name in component["client"].get("commands", {}))
            return ("external" if present else "missing"), "local runtime has no lifecycle receipt"
        for name, metadata in record["files"].items():
            if _metadata(target / name) != metadata:
                return "drifted", f"本地客户端文件漂移: {name}"
        for command, name in record["commands"].items():
            skill_collections.relative(name)
            link = Path.home() / ".local/bin" / command
            if skill_collections._link_value(link) != str(target / name):
                return "drifted", f"本地客户端命令漂移: {command}"
        return ("match" if record["source"] == component["client"]["source"] else "outdated"), "verified local runtime"
    except (OSError, ValueError, files.OwnershipError, ClientError, skill_collections.CollectionError) as error:
        return "blocked", str(error)


def stage(component: dict, workspace: Path) -> dict[str, Path]:
    spec = component["client"]
    asset = workspace / "asset"
    files.download(spec["source"]["url"], asset)
    if hashlib.sha256(asset.read_bytes()).hexdigest() != spec["source"]["sha256"]:
        raise ClientError("本地客户端固定资产 SHA-256 不符")
    checkout = files.extract(asset, workspace / "source") if spec["source"].get("archive") else None
    result = {}
    for name in spec["paths"]:
        skill_collections.relative(name)
        source = checkout / name if checkout else asset
        if source.is_dir():
            for item in source.rglob("*"):
                if item.is_file():
                    result[item.relative_to(checkout).as_posix()] = item
        elif source.is_file():
            result[name] = source
        else:
            raise ClientError(f"固定客户端资产缺少文件: {name}")
    if not result:
        raise ClientError("客户端运行文件为空")
    if "installer" in spec:
        installer = workspace / "install.sh"
        files.download(spec["installer"]["url"], installer)
        if hashlib.sha256(installer.read_bytes()).hexdigest() != spec["installer"]["sha256"]:
            raise ClientError("本地客户端 installer SHA-256 不符")
        if not shutil.which("tvly"):
            raise ClientError("Hikari 原生 installer 前必须先完成官方 tvly 交付")
        version = subprocess.run(["tvly", "--version"], capture_output=True, text=True, timeout=10, check=False)
        parsed = re.search(r"\d+\.\d+\.\d+", version.stdout)
        if version.returncode or not parsed or tuple(map(int, parsed[0].split("."))) < (0, 1, 3):
            raise ClientError("Hikari 所需 tvly 未达到原生最低版本；未调用 installer")
        generated = workspace / "generated"
        config = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "tavily-hikari-cli"
        process = subprocess.run(["bash", str(installer), "--install-dir", str(generated), "--config-dir", str(config)],
            env={**os.environ, "HIKARI_INSTALL_LOCAL_TVLY_HIKARI": str(asset)},
            capture_output=True, text=True, timeout=60, check=False)
        if process.returncode or (generated / ".tvly-hikari-real").read_bytes() != asset.read_bytes():
            raise ClientError("无凭据 Hikari 原生客户端生成失败")
        wrapper = generated / "tvly-hikari"
        before = wrapper.read_text()
        expected = ("#!/usr/bin/env bash\nset -euo pipefail\n"
                    'if [[ -z "${TAVILY_HIKARI_CONFIG_DIR:-}" && -z "${TAVILY_HIKARI_CONFIG_FILE:-}" ]]; then\n'
                    f"  export TAVILY_HIKARI_CONFIG_DIR={shlex.quote(str(config))}\nfi\n"
                    f'exec {shlex.quote(str(generated / ".tvly-hikari-real"))} "$@"\n')
        if before != expected:
            raise ClientError("Hikari 原生 wrapper 生成合同变化")
        wrapper.write_text(before.replace(shlex.quote(str(generated / ".tvly-hikari-real")),
                                         shlex.quote(str(root(component) / ".tvly-hikari-real"))))
        result = {name: generated / name for name in ("tvly-hikari", ".tvly-hikari-real")}
    return result


def operate(key: str, component: dict, operation: str, *, adopt: bool = False, expected_sha256: str | None = None) -> str:
    state, detail = inspect(key, component)
    old = receipt(key, component)
    reviewed = expected_sha256 is not None and operation == "upgrade" and fingerprint(component) == expected_sha256
    if expected_sha256 is not None and not reviewed:
        raise ClientError("客户端与已审阅 SHA-256 不符；未执行修改")
    if operation == "uninstall" and old is None:
        return "preserved"
    if state == "blocked" or state == "drifted" and not reviewed or state == "external" and not (adopt or reviewed):
        raise ClientError(f"拒绝操作本地客户端 {key}: {detail}")
    if state == "match" and operation != "uninstall":
        return "no-op"
    target = root(component)
    with tempfile.TemporaryDirectory(prefix="pennix-client-") as directory:
        incoming = stage(component, Path(directory)) if operation != "uninstall" else {}
        old_files = old["files"] if old else {}
        before = {name: files.read(target / name) for name in old_files.keys() | incoming.keys()}
        modes = {name: (target / name).stat().st_mode & 0o777 for name, raw in before.items() if raw is not None}
        for name, raw in before.items():
            if not reviewed and name not in old_files and raw is not None and (name not in incoming or raw != incoming[name].read_bytes()):
                raise ClientError(f"客户端接管拒绝覆盖非固定来源的现有文件: {name}")
        old_commands = old["commands"] if old else {}
        commands = component["client"].get("commands", {}) if incoming else {}
        links = {Path.home() / ".local/bin" / command: name for command, name in {**old_commands, **commands}.items()}
        command_files = {}
        for link in links:
            if reviewed and not link.is_symlink() and link.is_file():
                command_files[link] = (files.read(link), link.stat().st_mode & 0o777)
                continue
            if skill_collections._link_value(link) != (str(target / old_commands[link.name]) if link.name in old_commands else None):
                raise ClientError(f"客户端命令不是本工具归属: {link.name}")
        previous = files.read(receipt_path(key), private=True)
        output = files.json_bytes({"schema": 1, "root": str(target), "source": component["client"]["source"],
                                   "files": {name: {"digest": hashlib.sha256(path.read_bytes()).hexdigest(),
                                                    "mode": modes.get(name, 0o700 if path.stat().st_mode & 0o111 or name.endswith(".sh") or key == "tavily-hikari" else 0o600)}
                                             for name, path in incoming.items()}, "commands": commands}) if incoming else None
        changed = []
        changed_links = []
        written = False
        try:
            if reviewed and fingerprint(component) != expected_sha256:
                raise ClientError("客户端在提交前改变")
            for name, raw in before.items():
                after = incoming[name].read_bytes() if name in incoming else None
                mode = modes.get(name, 0o700 if name in incoming and (incoming[name].stat().st_mode & 0o111 or name.endswith(".sh") or key == "tavily-hikari") else 0o600)
                files.replace(target / name, after, raw, mode=mode)
                changed.append((name, after))
            for link in links:
                if link in command_files:
                    files.replace(link, None, command_files[link][0])
                previous_link = str(target / old_commands[link.name]) if link.name in old_commands else None
                want = str(target / commands[link.name]) if link.name in commands else None
                skill_collections._set_link(link, want, previous_link)
                changed_links.append((link, previous_link, want))
            files.replace(receipt_path(key), output, previous)
            written = True
            if incoming and inspect(key, component)[0] != "match":
                raise ClientError("本地客户端写入后核验失败")
        except BaseException as error:
            try:
                if written:
                    files.replace(receipt_path(key), previous, output)
                for link, previous_link, want in reversed(changed_links):
                    skill_collections._set_link(link, previous_link, want)
                for link, (raw, mode) in command_files.items():
                    if not link.exists() and not link.is_symlink():
                        files.replace(link, raw, None, mode=mode)
                for name, after in reversed(changed):
                    files.replace(target / name, before[name], after, mode=modes.get(name, 0o600))
            except BaseException as rollback:
                raise ClientError("本地客户端恢复未完成；保留现场") from rollback
            raise error
    return "changed; preserved adjacent user data"
