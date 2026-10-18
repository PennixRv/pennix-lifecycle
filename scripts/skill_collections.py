"""Install fixed Skill collections using their own versioned manifests."""
from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tempfile

import yaml
import owned_files as files

CollectionError = files.OwnershipError
safe_path = files.safe_path
digest = files.digest
NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
SHA = re.compile(r"[0-9a-f]{40}")


class UniqueYaml(yaml.SafeLoader):
    pass


def _mapping(loader, node):
    return files.unique_pairs((loader.construct_object(key), loader.construct_object(value))
                              for key, value in node.value)


UniqueYaml.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def relative(value: str) -> str:
    if not isinstance(value, str):
        raise CollectionError("集合路径必须是字符串")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or not path.parts or str(path) != value:
        raise CollectionError("集合包含非法相对路径")
    return value


def destination(component: dict, codex_home: Path) -> Path:
    name = component["collection_contract"]["directory"]
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise CollectionError("集合目录名称无效")
    path = codex_home / "skills" / name
    safe_path(path)
    return path


def source_identity(component: dict) -> dict:
    source = component["collection_contract"]["source"]
    if (set(source) != {"repo", "ref", "manifest"} or not REPO.fullmatch(source["repo"])
            or not SHA.fullmatch(source["ref"])):
        raise CollectionError("集合来源必须固定到完整提交及自身 manifest")
    relative(source["manifest"])
    return source.copy()


def validate_manifest(value: dict, name: str) -> None:
    if (not isinstance(value, dict) or set(value) != {"schema", "name", "skills", "commands", "materialized"}
            or type(value["schema"]) is not int or value["schema"] != 1 or value["name"] != name
            or not isinstance(value["skills"], dict) or not value["skills"]
            or not isinstance(value["commands"], dict) or not isinstance(value["materialized"], dict)):
        raise CollectionError("集合 manifest 格式无效")
    paths = set()
    for member, entry in value["skills"].items():
        if not NAME.fullmatch(member) or set(entry) != {"path", "license"}:
            raise CollectionError("集合成员格式无效")
        path = relative(entry["path"])
        if Path(path).name != member or path in paths:
            raise CollectionError("集合成员路径重复或身份不一致")
        paths.add(path)
        if entry["license"] is not None:
            relative(entry["license"])
    for command, target in value["commands"].items():
        parts = PurePosixPath(relative(target)).parts
        if not NAME.fullmatch(command) or len(parts) < 2 or parts[0] not in value["skills"]:
            raise CollectionError("集合命令格式无效")
    for member, material in value["materialized"].items():
        if (member not in value["skills"] or not isinstance(material, dict)
                or not REPO.fullmatch(material.get("repo", "")) or not SHA.fullmatch(material.get("commit", ""))
                or material.get("ref") != material["commit"] or material.get("name") != member
                or material.get("snapshot") not in {"archive", "npm-pack"}
                or material.get("post_install", []) not in [[], ["grok-search-runtime"]]):
            raise CollectionError("物化来源未固定或动作不受支持")


def validate_members(path: Path, manifest: dict) -> None:
    if not path.is_dir() or {item.name for item in path.iterdir()} != set(manifest["skills"]):
        raise CollectionError("集合成员集合不一致")
    digest(path)
    for name, entry in manifest["skills"].items():
        skill = path / name / "SKILL.md"
        raw = files.read(skill)
        if raw is None:
            raise CollectionError(f"缺少 SKILL.md: {name}")
        text = raw.decode("utf-8")
        header = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|$)", text, re.S)
        try:
            metadata = yaml.load(header[1], Loader=UniqueYaml) if header else None
        except yaml.YAMLError as error:
            raise CollectionError(f"Skill YAML 无效: {name}") from error
        if (not isinstance(metadata, dict) or metadata.get("name") != name
                or not isinstance(metadata.get("description"), str) or not metadata["description"].strip()):
            raise CollectionError(f"Skill 身份或描述无效: {name}")
        if entry["license"] is not None and not files.read(path / name / entry["license"]):
            raise CollectionError(f"集合许可文件缺失: {name}")
    for target in manifest["commands"].values():
        if not (path / target).is_file() or not os.access(path / target, os.X_OK):
            raise CollectionError("集合命令缺失或不可执行")


def receipt_path(path: Path) -> Path:
    return path.parent / f".{path.name}.receipt.json"


def read_receipt(path: Path) -> dict | None:
    value = files.read_json(receipt_path(path))
    if value is None:
        return None
    if (not isinstance(value, dict) or set(value) != {"schema", "destination", "digest", "source", "manifest"}
            or type(value["schema"]) is not int or value["schema"] != 1 or value["destination"] != path.name
            or not re.fullmatch(r"[0-9a-f]{64}", value.get("digest", ""))):
        raise CollectionError("集合 receipt 格式不受支持")
    source_identity({"collection_contract": {"source": value["source"]}})
    validate_manifest(value["manifest"], path.name)
    return value


def links(manifest: dict, path: Path) -> dict[Path, str]:
    parent = Path.home() / ".local/bin"
    safe_path(parent)
    return {parent / name: str(path / target) for name, target in manifest["commands"].items()}


def _link_value(path: Path) -> str | None:
    safe_path(path.parent)
    if path.is_symlink():
        return os.readlink(path)
    if path.exists():
        raise CollectionError(f"命令位置由其他资源占用: {path.name}")
    return None


def _set_link(path: Path, want: str | None, before: str | None) -> None:
    if _link_value(path) != before:
        raise CollectionError(f"命令链接在操作期间改变: {path.name}")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if want is None:
        path.unlink()
    elif before is None:
        path.symlink_to(want)
    elif before != want:
        raise CollectionError("受管命令目标不能跨集合接管")


def _verified(path: Path) -> dict:
    value = read_receipt(path)
    if value is None or value["digest"] != digest(path):
        raise CollectionError("集合缺少归属或内容已漂移")
    validate_members(path, value["manifest"])
    for link, target in links(value["manifest"], path).items():
        if _link_value(link) != target:
            raise CollectionError(f"集合命令归属已漂移: {link.name}")
    return value


def inspect(component: dict, codex_home: Path) -> tuple[str, str]:
    try:
        path = destination(component, codex_home)
        source = source_identity(component)
        if not path.exists():
            return ("blocked", "孤立集合 receipt") if receipt_path(path).exists() else ("missing", str(path))
        value = _verified(path)
        if any(value["source"][key] != source[key] for key in ("repo", "manifest")):
            return "blocked", "集合来源归属不一致"
        return ("match", str(path)) if value["source"] == source else ("upgrade-required", "固定来源提交已更新")
    except (CollectionError, OSError, ValueError, KeyError, TypeError) as error:
        return "drifted", str(error)


def owned(component: dict, codex_home: Path) -> bool:
    return inspect(component, codex_home)[0] in {"match", "upgrade-required"}


def _stage(component: dict, workspace: Path) -> tuple[Path, dict]:
    source = source_identity(component)
    archive = workspace / "source.tar.gz"
    files.download(f"https://codeload.github.com/{source['repo']}/tar.gz/{source['ref']}", archive)
    checkout = files.extract(archive, workspace / "source")
    import json
    manifest = json.loads((checkout / source["manifest"]).read_bytes(), object_pairs_hook=files.unique_pairs)
    validate_manifest(manifest, component["collection_contract"]["directory"])
    staging = workspace / "collection"
    staging.mkdir(mode=0o700)
    for name, entry in manifest["skills"].items():
        shutil.copytree(checkout / entry["path"], staging / name)
    validate_members(staging, manifest)
    for name, material in manifest["materialized"].items():
        if material.get("post_install") == ["grok-search-runtime"]:
            result = subprocess.run(["npm", "ci", "--omit=dev", "--ignore-scripts", "--no-audit", "--no-fund"],
                                    cwd=staging / name, capture_output=True, timeout=240)
            if result.returncode:
                raise CollectionError(f"集合生产依赖安装失败: {name}")
    validate_members(staging, manifest)
    return staging, manifest


def operate(component: dict, codex_home: Path, operation: str) -> str:
    path = destination(component, codex_home)
    files.ensure_parent(path)
    receipt = receipt_path(path)
    state, reason = inspect(component, codex_home)
    if operation == "uninstall" and files.read(receipt) is None:
        return "preserved" if path.exists() else "no-op"
    if state not in {"missing", "match", "upgrade-required"}:
        raise CollectionError(f"拒绝操作 {state} 集合: {reason}")
    if state == "match" and operation != "uninstall":
        return "no-op"
    old = _verified(path) if path.exists() else None
    old_raw = files.read(receipt, private=True)
    workspace = Path(tempfile.mkdtemp(prefix=f".{path.name}.stage-", dir=path.parent))
    recoverable = False
    try:
        staging, manifest = _stage(component, workspace) if operation != "uninstall" else (None, None)
        old_links = links(old["manifest"], path) if old else {}
        new_links = links(manifest, path) if manifest else {}
        before_links = {link: _link_value(link) for link in old_links.keys() | new_links.keys()}
        for link, target in before_links.items():
            if target != old_links.get(link):
                raise CollectionError(f"拒绝接管已有命令: {link.name}")
        if old and _verified(path) != old or not old and path.exists() or files.read(receipt) != old_raw:
            raise CollectionError("集合在暂存期间改变")
        after_raw = files.json_bytes({"schema": 1, "destination": path.name, "digest": digest(staging),
                                      "source": source_identity(component), "manifest": manifest}) if staging else None
        backup = workspace / "previous"
        changed_links = []
        installed = False
        receipt_changed = False
        try:
            if old:
                os.replace(path, backup)
            if staging:
                os.replace(staging, path)
                installed = True
            files.replace(receipt, after_raw, old_raw)
            receipt_changed = True
            for link, before in before_links.items():
                want = new_links.get(link)
                if want != before:
                    _set_link(link, want, before)
                    changed_links.append(link)
            if staging and inspect(component, codex_home)[0] != "match":
                raise CollectionError("集合切换后核验失败")
        except BaseException as original:
            try:
                for link in reversed(changed_links):
                    _set_link(link, before_links[link], new_links.get(link))
                if installed:
                    if digest(path) != __import__('json').loads(after_raw)["digest"]:
                        raise CollectionError("切换后的集合发生外部改变")
                    os.replace(path, staging)
                if backup.exists():
                    os.replace(backup, path)
                if receipt_changed:
                    files.replace(receipt, old_raw, after_raw)
            except BaseException as rollback:
                recoverable = True
                raise CollectionError(f"集合恢复未完成，可恢复资产保留于 {workspace}") from rollback
            raise original
        return "changed"
    finally:
        if not recoverable:
            shutil.rmtree(workspace)
