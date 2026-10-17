"""Private credential storage owned by pennix-lifecycle."""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path
from typing import Any

from tomlkit import document, parse


FIELDS = {
    "codex.api_key",
    "siyuan.api_token",
    "cch.admin_token",
    "hikari.api_token",
    "grok.api_key",
    "grok.tavily_api_key",
    "windsurf.api_key",
}


class SecretError(ValueError):
    """The private credential file is absent, unsafe, or invalid."""


def default_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")).expanduser()
    return base / "pennix" / "secrets.toml"


def _parts(field: str) -> tuple[str, str]:
    if field not in FIELDS or field.count(".") != 1:
        raise SecretError(f"不支持的秘密字段: {field}")
    return tuple(field.split(".", 1))  # type: ignore[return-value]


def _secure_parent(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parent.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            current.mkdir(mode=0o700)
            info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise SecretError("秘密文件目录不是安全的普通目录")
        if current == path.parent and hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise SecretError("秘密文件目录不属于当前用户")
        if current == path.parent:
            os.chmod(current, 0o700)


def _read(path: Path) -> Any:
    if path.is_symlink():
        raise SecretError("秘密文件不是安全的普通文件")
    if not path.exists():
        return document()
    if not path.is_file():
        raise SecretError("秘密文件不是安全的普通文件")
    if hasattr(os, "getuid") and path.stat().st_uid != os.getuid():
        raise SecretError("秘密文件不属于当前用户")
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise SecretError("秘密文件权限必须为 0600")
    try:
        return parse(path.read_text(encoding="utf-8"))
    except Exception as error:
        raise SecretError("秘密文件不是有效 TOML") from error


def _validate_doc(doc: Any) -> None:
    if not hasattr(doc, "items"):
        raise SecretError("秘密文件顶层必须是 TOML 表")
    for section, value in doc.items():
        if section not in {field.split(".", 1)[0] for field in FIELDS} or not hasattr(value, "items"):
            raise SecretError(f"秘密文件包含不支持的字段组: {section}")
        for key, item in value.items():
            field = f"{section}.{key}"
            if field not in FIELDS or not isinstance(item, str):
                raise SecretError(f"秘密文件包含不支持的字段: {field}")


def load(path: Path | None = None) -> dict[str, str]:
    target = path or default_path()
    doc = _read(target)
    _validate_doc(doc)
    result: dict[str, str] = {}
    for section, value in doc.items():
        for key, item in value.items():
            if item:
                result[f"{section}.{key}"] = str(item)
    return result


def ensure(path: Path | None = None, template: str = "") -> Path:
    target = path or default_path()
    _secure_parent(target)
    if target.exists() or target.is_symlink():
        _read(target)
        return target
    descriptor, name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(template)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def update(path: Path | None, updates: dict[str, str]) -> list[str]:
    target = path or default_path()
    if not updates:
        return []
    for field, value in updates.items():
        _parts(field)
        if not isinstance(value, str) or not value.strip() or "\x00" in value or "\n" in value or "\r" in value:
            raise SecretError(f"秘密字段无效: {field}")
    _secure_parent(target)
    doc = _read(target)
    _validate_doc(doc)
    changed: list[str] = []
    for field, value in updates.items():
        section, key = _parts(field)
        current = doc.get(section, {}).get(key) if hasattr(doc.get(section, {}), "get") else None
        if current not in (None, "", value):
            raise SecretError(f"秘密字段已有不同值，拒绝覆盖: {field}")
        if current != value:
            if section not in doc:
                doc.add(section, {})
            doc[section][key] = value
            changed.append(field)
    if not changed and target.exists():
        return []
    descriptor, name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(doc.as_string())
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
        os.chmod(target, 0o600)
    finally:
        temporary.unlink(missing_ok=True)
    return changed


def get(field: str, path: Path | None = None) -> str:
    value = load(path).get(field)
    if not value:
        raise SecretError(f"未设置秘密字段: {field}")
    return value
