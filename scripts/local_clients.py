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
import urllib.parse

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


def _source_matches(key: str, component: dict, record: dict) -> bool:
    current = component["client"]["source"]
    source = record["source"]
    if all(source.get(field) == current.get(field) for field in ("repo", "path", "archive") if field in current):
        return True
    url = source.get("url")
    if not isinstance(url, str):
        return False
    parsed = urllib.parse.urlsplit(url)
    parts = parsed.path.strip("/").split("/")
    if not current.get("archive") and parsed.hostname == "raw.githubusercontent.com" and len(parts) >= 4:
        repo, ref, path = "/".join(parts[:2]), parts[2], "/".join(parts[3:])
        owned = ".tvly-hikari-real" if key == "tavily-hikari" else path
        return (repo == current.get("repo") and path == current.get("path")
                and re.fullmatch(r"[0-9a-f]{40}", ref) is not None
                and record["files"].get(owned, {}).get("digest") == source.get("sha256"))
    if current.get("archive") and parsed.hostname == "codeload.github.com" and len(parts) == 4 and parts[2] == "tar.gz":
        repo, ref = "/".join(parts[:2]), parts[3]
        return (repo == current.get("repo") and re.fullmatch(r"[0-9a-f]{40}", ref) is not None
                and re.fullmatch(r"[0-9a-f]{64}", source.get("sha256", "")) is not None)
    return False


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
        if not _source_matches(key, component, record):
            return "blocked", "本地客户端来源归属不一致"
        lock = component.get("_lock_target") or component.get("_candidate_target")
        if isinstance(lock, dict) and any(record["source"].get(key) != lock.get(key)
                                          for key in ("ref", "sha256") if key in lock):
            return "outdated", "本地客户端需要更新到锁定身份"
        return "match", "verified local runtime"
    except (OSError, ValueError, files.OwnershipError, ClientError, skill_collections.CollectionError) as error:
        return "blocked", str(error)


def lock_migration(key: str, component: dict) -> dict | None:
    try:
        record = receipt(key, component)
    except ClientError:
        return None
    if record is None:
        return None
    target = root(component)
    for name, metadata in record["files"].items():
        if _metadata(target / name) != metadata:
            return None
    for command, name in record["commands"].items():
        link = Path.home() / ".local/bin" / command
        if skill_collections._link_value(link) != str(target / name):
            return None
    spec = component["client"]["source"]
    source = record["source"]
    repo, path, archive = spec["repo"], spec.get("path"), spec.get("archive", False)
    ref = source.get("ref")
    digest = source.get("sha256")
    if _source_matches(key, component, record) and isinstance(source.get("repo"), str):
        ref = source.get("ref")
    elif _source_matches(key, component, record):
        url = urllib.parse.urlsplit(source.get("url", ""))
        parts = url.path.strip("/").split("/")
        ref = parts[2] if not archive else parts[3]
    elif source.get("repo") == repo and source.get("path") == path and source.get("archive", False) == archive:
        pass
    elif isinstance(source.get("url"), str):
        parsed = urllib.parse.urlsplit(source["url"])
        parts = parsed.path.strip("/").split("/")
        if not archive and parsed.hostname == "raw.githubusercontent.com" and len(parts) >= 4:
            old_repo, ref, old_path = "/".join(parts[:2]), parts[2], "/".join(parts[3:])
            if old_repo != repo or old_path != path:
                return None
            owned_name = ".tvly-hikari-real" if key == "tavily-hikari" else path
            if record["files"].get(owned_name, {}).get("digest") != digest:
                return None
        elif archive and parsed.hostname == "codeload.github.com" and len(parts) == 4 and parts[2] == "tar.gz":
            old_repo, ref = "/".join(parts[:2]), parts[3]
            if old_repo != repo:
                return None
        else:
            return None
    else:
        return None
    if not isinstance(ref, str) or not re.fullmatch(r"[0-9a-f]{40}", ref):
        return None
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        return None
    return {"source": component["lock_source"], "ref": ref, "sha256": digest}


def stage(component: dict, workspace: Path, candidate: dict | None = None) -> dict[str, Path]:
    spec = component["client"]
    asset = workspace / "asset"
    if candidate:
        staged = Path(candidate["path"])
        if hashlib.sha256(staged.read_bytes()).hexdigest() != (candidate.get("sha256") or candidate.get("archive_sha256")):
            raise ClientError("本地客户端候选摘要不符")
        source = spec["source"]
        checkout = Path(candidate["checkout"]) if candidate.get("checkout") else None
        if source.get("repo") != candidate.get("repo"):
            raise ClientError("本地客户端候选来源不符")
        if source.get("archive") and checkout is None:
            raise ClientError("本地客户端归档候选缺少校验后的源码树")
        if not source.get("archive"):
            asset = staged
    else:
        if "url" not in spec["source"] or "sha256" not in spec["source"]:
            raise ClientError("本地客户端尚未解析精确候选")
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
    if component.get("id") == "tavily-hikari":
        if not shutil.which("tvly"):
            raise ClientError("Hikari 客户端需要先完成官方 tvly 交付")
        version = subprocess.run(["tvly", "--version"], capture_output=True, text=True, timeout=10, check=False)
        parsed = re.search(r"\d+\.\d+\.\d+", version.stdout)
        if version.returncode or not parsed or tuple(map(int, parsed[0].split("."))) < (0, 1, 3):
            raise ClientError("Hikari 所需 tvly 未达到最低版本")
        generated = workspace / "generated"
        generated.mkdir(mode=0o700)
        config = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "tavily-hikari-cli"
        real = generated / ".tvly-hikari-real"
        real.write_bytes(asset.read_bytes())
        real.chmod(0o700)
        wrapper = generated / "tvly-hikari"
        wrapper.write_text("#!/usr/bin/env bash\nset -euo pipefail\n"
                           'if [[ -z "${TAVILY_HIKARI_CONFIG_DIR:-}" && -z "${TAVILY_HIKARI_CONFIG_FILE:-}" ]]; then\n'
                           f"  export TAVILY_HIKARI_CONFIG_DIR={shlex.quote(str(config))}\nfi\n"
                           f'exec {shlex.quote(str(root(component) / ".tvly-hikari-real"))} "$@"\n')
        wrapper.chmod(0o700)
        result = {name: generated / name for name in ("tvly-hikari", ".tvly-hikari-real")}
    return result


def operate(key: str, component: dict, operation: str, *, adopt: bool = False, expected_sha256: str | None = None,
            candidate: dict | None = None) -> str:
    state, detail = inspect(key, component)
    old = receipt(key, component)
    reviewed = expected_sha256 is not None and operation == "upgrade" and fingerprint(component) == expected_sha256
    if expected_sha256 is not None and not reviewed:
        raise ClientError("客户端与已审阅 SHA-256 不符；未执行修改")
    if operation == "uninstall" and old is None:
        return "preserved"
    if state == "blocked" or state == "drifted" and not reviewed or state == "external" and not (adopt or reviewed):
        raise ClientError(f"拒绝操作本地客户端 {key}: {detail}")
    if state == "match" and operation != "uninstall" and candidate is None:
        return "no-op"
    target = root(component)
    with tempfile.TemporaryDirectory(prefix="pennix-client-") as directory:
        incoming = stage(component, Path(directory), candidate) if operation != "uninstall" else {}
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
        receipt_source = ({key: value for key, value in component["client"]["source"].items()
                          if key in {"repo", "path", "archive"}} | {
                              key: candidate.get(key, candidate.get("archive_sha256"))
                              for key in ("ref", "sha256") if candidate and key in candidate
                          }) if candidate else component["client"]["source"]
        output = files.json_bytes({"schema": 1, "root": str(target), "source": receipt_source,
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
