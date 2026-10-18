"""Install or remove the one active lifecycle release."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import tempfile
import hashlib
import json
import re

import owned_files


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ("pennix", "scripts", "references", "config", "templates", "VERSION")
OLD_RELEASE_CONTENTS = {".gitignore", "README.md", "bootstrap.sh", "config", "pennix", "references", "scripts", "templates", "tests"}
VERSION_NAME = re.compile(r"^[0-9]+(?:\.[0-9]+)*$")


class ReleaseError(RuntimeError):
    pass


def _locations() -> tuple[Path, Path]:
    data = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")).expanduser()
    return data / "pennix-lifecycle" / "releases", Path.home() / ".local/bin/pennix"


def _receipt_path() -> Path:
    return owned_files.state_root() / "release.json"


def _receipt(raw: bytes | None) -> dict | None:
    if raw is None:
        return None
    try:
        value = json.loads(raw, object_pairs_hook=owned_files.unique_pairs)
    except (ValueError, UnicodeError) as error:
        raise ReleaseError("当前发行归属记录无效") from error
    if (not isinstance(value, dict) or set(value) != {"schema", "version", "path", "digest"}
            or value["schema"] != 1 or not all(isinstance(value[key], str) for key in ("version", "path", "digest"))):
        raise ReleaseError("当前发行归属记录格式无效")
    return value


def _version(source: Path) -> str:
    try:
        version = (source / "VERSION").read_text(encoding="ascii").strip()
    except OSError as error:
        raise ReleaseError("发行包缺少 VERSION") from error
    if not version or any(char not in "0123456789." for char in version):
        raise ReleaseError("发行版本号无效")
    return version


def _runtime_digest(path: Path) -> str:
    if not (path / "pennix").is_file() or not os.access(path / "pennix", os.X_OK):
        raise ReleaseError("发行包缺少可执行入口 pennix")
    digest = hashlib.sha256()
    for name in RUNTIME:
        item = path / name
        if item.is_symlink() or not item.exists():
            raise ReleaseError(f"发行包运行文件缺失或为符号链接: {name}")
        digest.update(name.encode() + b"\0")
        if item.is_dir():
            digest.update(owned_files.digest(item).encode())
        else:
            info = item.stat()
            if not item.is_file() or info.st_uid != os.getuid() or info.st_mode & 0o022:
                raise ReleaseError(f"发行文件类型、归属或权限不安全: {name}")
            digest.update(str(info.st_mode & 0o111).encode() + b"\0")
            digest.update(hashlib.sha256(item.read_bytes()).digest())
    return digest.hexdigest()


def _active_target(entry: Path, releases: Path) -> Path | None:
    if not entry.exists() and not entry.is_symlink():
        return None
    if not entry.is_symlink():
        raise ReleaseError(f"拒绝覆盖非本工具符号链接: {entry}")
    target = entry.resolve(strict=True)
    if target.parent.parent != releases or target.name != "pennix":
        raise ReleaseError(f"现有入口未指向本工具发行目录: {entry}")
    current_digest = _installed_digest(target.parent)
    stored = _receipt(owned_files.read(_receipt_path(), private=True))
    if stored is None and {item.name for item in target.parent.iterdir()} == OLD_RELEASE_CONTENTS:
        return target.parent
    if stored != {"schema": 1, "version": target.parent.name, "path": str(target.parent), "digest": current_digest}:
        raise ReleaseError("当前发行与唯一归属记录不匹配；拒绝覆盖或删除")
    return target.parent


def _installed_digest(path: Path) -> str:
    if path.is_symlink() or not path.is_dir() or not VERSION_NAME.fullmatch(path.name):
        raise ReleaseError(f"非受管发行目录: {path}")
    contents = {item.name for item in path.iterdir()}
    if contents == OLD_RELEASE_CONTENTS:
        # Only recognize complete old installations for removal, never run or copy them.
        if not os.access(path / "pennix", os.X_OK):
            raise ReleaseError("旧发行缺少可执行入口")
        return owned_files.digest(path)
    if contents != set(RUNTIME) or _version(path) != path.name:
        raise ReleaseError(f"发行目录包含非发行资源，拒绝自动清理: {path}")
    return _runtime_digest(path)


def _assert_release_tree(releases: Path, *allowed: Path | None) -> dict[Path, str]:
    allowed_paths = {path for path in allowed if path is not None}
    inactive = {}
    for item in releases.iterdir():
        if item in allowed_paths:
            continue
        if item.is_symlink() or not item.is_dir() or not VERSION_NAME.fullmatch(item.name):
            raise ReleaseError(f"发行目录包含未知或不安全资源，拒绝自动清理: {item}")
        inactive[item] = _installed_digest(item)
    return inactive


def _install(source: Path) -> str:
    source = source.resolve(strict=True)
    version = _version(source)
    source_digest = _runtime_digest(source)
    releases, entry = _locations()
    releases.mkdir(parents=True, mode=0o700, exist_ok=True)
    owned_files.safe_path(releases)
    if releases.stat().st_uid != os.getuid():
        raise ReleaseError("发行目录不属于当前用户")
    entry.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    owned_files.safe_path(entry.parent)
    if entry.parent.stat().st_uid != os.getuid():
        raise ReleaseError("入口目录不属于当前用户")

    current = _active_target(entry, releases)
    current_digest = _installed_digest(current) if current else None
    receipt_path = _receipt_path()
    previous_receipt = owned_files.read(receipt_path, private=True)
    if current is None and previous_receipt is not None:
        raise ReleaseError("存在无活动入口的发行归属记录；拒绝覆盖")
    target = releases / version
    inactive = _assert_release_tree(releases, current, target)
    if current and current != target:
        stored = _receipt(previous_receipt)
        if _installed_digest(current) != current_digest or (stored is not None and current_digest != stored["digest"]):
            raise ReleaseError("旧发行内容发生变化；拒绝切换发行")
    if target.exists():
        if current != target or _runtime_digest(target) != source_digest:
            raise ReleaseError(f"目标发行目录已存在且不能安全复用: {target}")
        for item, expected in inactive.items():
            if _installed_digest(item) != expected:
                raise ReleaseError("非活动发行在清理前发生变化")
            shutil.rmtree(item)
        return f"current {version}"
    else:
        staging = Path(tempfile.mkdtemp(prefix=f".{version}.", dir=releases))
        try:
            for name in RUNTIME:
                item = source / name
                if item.is_symlink() or not item.exists():
                    raise ReleaseError(f"发行包运行文件缺失或为符号链接: {name}")
                destination = staging / name
                if item.is_dir():
                    shutil.copytree(item, destination, symlinks=False,
                                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
                else:
                    shutil.copy2(item, destination, follow_symlinks=False)
            if _runtime_digest(staging) != source_digest:
                raise ReleaseError("暂存发行内容核验失败")
            os.replace(staging, target)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    link = entry.parent / f".pennix.{os.getpid()}.tmp"
    link.symlink_to(target / "pennix")
    previous = entry.readlink() if entry.is_symlink() else None
    try:
        os.replace(link, entry)
        if entry.resolve(strict=True) != target / "pennix":
            raise ReleaseError("入口切换核验失败")
        record = owned_files.json_bytes({
            "schema": 1, "version": version, "path": str(target), "digest": source_digest,
        })
        owned_files.replace(receipt_path, record, previous_receipt)
    except BaseException:
        link.unlink(missing_ok=True)
        if previous is not None:
            restore = entry.parent / f".pennix.{os.getpid()}.restore"
            restore.symlink_to(previous)
            os.replace(restore, entry)
        else:
            entry.unlink(missing_ok=True)
        if owned_files.read(receipt_path, private=True) != previous_receipt:
            try:
                owned_files.replace(receipt_path, previous_receipt, owned_files.read(receipt_path, private=True))
            except (OSError, owned_files.OwnershipError):
                pass
        if current and current != target:
            raise ReleaseError("入口切换失败；原发行版已恢复")
        raise

    if current and current != target:
        inactive[current] = current_digest
    for item, expected in inactive.items():
        try:
            if _installed_digest(item) != expected:
                raise ReleaseError("旧发行在清理前发生变化")
            shutil.rmtree(item)
        except BaseException as error:
            raise ReleaseError("新发行已生效，旧发行清理未完成；保留现场") from error
    return f"installed {version}"


def _uninstall() -> str:
    releases, entry = _locations()
    current = _active_target(entry, releases)
    if current is None:
        return "absent"
    receipt_path = _receipt_path()
    previous_receipt = owned_files.read(receipt_path, private=True)
    inactive = _assert_release_tree(releases, current)
    if inactive:
        raise ReleaseError("存在非活动发行；先安装当前发行收敛后再卸载")
    previous_link = entry.readlink()
    tombstone = releases / f".remove.{os.getpid()}"
    if tombstone.exists() or tombstone.is_symlink():
        raise ReleaseError("发行卸载暂存路径已存在")
    os.replace(current, tombstone)
    try:
        entry.unlink()
        owned_files.replace(receipt_path, None, previous_receipt)
        shutil.rmtree(tombstone)
    except BaseException as error:
        if not current.exists() and tombstone.exists():
            os.replace(tombstone, current)
        if not entry.exists() and not entry.is_symlink():
            restored = entry.parent / f".pennix.{os.getpid()}.restore"
            restored.symlink_to(previous_link)
            os.replace(restored, entry)
        if owned_files.read(receipt_path, private=True) != previous_receipt:
            try:
                owned_files.replace(
                    receipt_path,
                    previous_receipt,
                    owned_files.read(receipt_path, private=True),
                )
            except (OSError, owned_files.OwnershipError):
                pass
        raise ReleaseError("发行卸载失败；已尝试恢复活动入口和归属记录") from error
    try:
        releases.rmdir()
        releases.parent.rmdir()
    except OSError:
        pass
    return "uninstalled"


def install(source: Path) -> str:
    with owned_files.locked():
        return _install(source)


def uninstall() -> str:
    with owned_files.locked():
        return _uninstall()

