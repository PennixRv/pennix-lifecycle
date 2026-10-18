"""Filesystem boundaries shared by the lifecycle owners."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import tarfile
import tempfile
import urllib.request


class OwnershipError(RuntimeError):
    pass


def safe_path(path: Path) -> None:
    if not path.is_absolute() or ".." in path.parts:
        raise OwnershipError("目标必须是无穿越的绝对路径")
    for item in (path, *path.parents):
        if item.is_symlink():
            raise OwnershipError(f"目标路径包含符号链接: {item}")
        if item.exists():
            info = item.stat()
            if info.st_uid not in {0, os.getuid()}:
                raise OwnershipError(f"目标路径归属不安全: {item}")
            if info.st_mode & 0o022 and not (stat.S_ISDIR(info.st_mode) and info.st_mode & stat.S_ISVTX):
                raise OwnershipError(f"目标路径可被其他用户修改: {item}")


def ensure_parent(path: Path) -> None:
    safe_path(path)
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    safe_path(path)
    if path.parent.stat().st_uid != os.getuid():
        raise OwnershipError("目标目录不属于当前用户")


def read(path: Path, *, private: bool = False, limit: int = 8 * 1024 * 1024) -> bytes | None:
    safe_path(path)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_size > limit or (private and info.st_mode & 0o077)):
            raise OwnershipError(f"文件类型、归属、权限或大小不安全: {path}")
        return stream.read(limit + 1)


def replace(path: Path, content: bytes | None, before: bytes | None, *, mode: int = 0o600) -> None:
    ensure_parent(path)
    if read(path) != before:
        raise OwnershipError(f"目标在操作期间发生变化: {path}")
    if content == before:
        return
    if content is None:
        path.unlink()
        return
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(mode)
        if read(path) != before:
            raise OwnershipError(f"目标在提交前发生变化: {path}")
        os.replace(temporary, path)
        if read(path) != content:
            raise OwnershipError(f"目标写入核验失败: {path}")
    finally:
        temporary.unlink(missing_ok=True)


def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise OwnershipError("JSON 包含重复键")
        result[key] = value
    return result


def read_json(path: Path):
    raw = read(path, private=True)
    if raw is None:
        return None
    try:
        return json.loads(raw, object_pairs_hook=unique_pairs)
    except (ValueError, UnicodeError) as error:
        raise OwnershipError(f"JSON 记录无效: {path}") from error


def json_bytes(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n").encode()


def state_root() -> Path:
    path = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "pennix-lifecycle"
    safe_path(path)
    return path


@contextmanager
def locked():
    # One user owns shared commands, services and receipts across CODEX_HOME values.
    path = state_root() / "lock"
    ensure_parent(path)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "r+") as stream:
        info = os.fstat(stream.fileno())
        if info.st_uid != os.getuid() or not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise OwnershipError("生命周期锁不安全")
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise OwnershipError("另一个生命周期操作正在执行") from error
        yield


def download(url: str, destination: Path, *, limit: int = 512 * 1024 * 1024) -> None:
    if not url.startswith("https://"):
        raise OwnershipError("下载只接受 HTTPS")
    with urllib.request.urlopen(url, timeout=60) as response, destination.open("xb") as stream:
        if not response.url.startswith("https://"):
            raise OwnershipError("下载重定向离开 HTTPS")
        total = 0
        while chunk := response.read(65536):
            total += len(chunk)
            if total > limit:
                raise OwnershipError("下载超过大小上限")
            stream.write(chunk)


def extract(archive: Path, destination: Path) -> Path:
    """Extract one-root source/runtime archives without links or special files."""
    destination.mkdir(mode=0o700)
    seen = set()
    roots = set()
    total = 0
    with tarfile.open(archive, "r:gz") as source:
        for entry in source:
            name = PurePosixPath(entry.name)
            if (name.is_absolute() or ".." in name.parts or not name.parts
                    or str(name) in seen or not (entry.isdir() or entry.isfile())):
                raise OwnershipError("归档包含重复、不安全路径、链接或特殊文件")
            seen.add(str(name))
            roots.add(name.parts[0])
            total += entry.size
            if len(roots) != 1 or total > 1024 * 1024 * 1024 or len(seen) > 100000:
                raise OwnershipError("归档结构或大小超限")
            target = destination.joinpath(*name.parts)
            if entry.isdir():
                target.mkdir(parents=True, exist_ok=True, mode=0o700)
            else:
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with source.extractfile(entry) as incoming, target.open("xb") as outgoing:
                    while chunk := incoming.read(65536):
                        outgoing.write(chunk)
                target.chmod(0o700 if entry.mode & 0o111 else 0o600)
    if not roots:
        raise OwnershipError("归档为空")
    return destination / roots.pop()


def digest(path: Path) -> str:
    safe_path(path)
    result = hashlib.sha256()
    for item in sorted(path.rglob("*"), key=lambda p: p.relative_to(path).as_posix()):
        relative = item.relative_to(path)
        info = item.lstat()
        if (not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))
                or info.st_uid != os.getuid() or info.st_mode & 0o022):
            raise OwnershipError(f"受管树包含不安全资源: {relative}")
        if "__pycache__" in relative.parts or item.suffix == ".pyc":
            continue
        if item.is_dir():
            result.update(f"D\0{relative.as_posix()}\0".encode())
        else:
            result.update(f"F\0{relative.as_posix()}\0{info.st_mode & 0o111:o}\0".encode())
            with item.open("rb") as stream:
                for chunk in iter(lambda: stream.read(65536), b""):
                    result.update(chunk)
    return result.hexdigest()
