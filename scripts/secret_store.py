"""Read the private, strictly shaped workflow secrets file."""
from __future__ import annotations

import os
from pathlib import Path
import stat
import tomllib
from tomlkit import document, parse, table
from tomlkit.exceptions import ParseError

import owned_files


FIELDS = {
    "codex": {"api_key"},
    "siyuan": {"api_token"},
    "cch": {"admin_token"},
    "hikari": {"api_token"},
    "grok": {"api_key", "tavily_api_key"},
    "windsurf": {"api_key"},
}


class SecretError(RuntimeError):
    pass


def _validate(value: object) -> dict[str, dict[str, str]]:
    if not isinstance(value, dict) or set(value) - FIELDS.keys():
        raise SecretError("秘密文件包含未知表")
    for table_name, fields in value.items():
        if not isinstance(table_name, str) or not isinstance(fields, dict) or set(fields) - FIELDS[table_name]:
            raise SecretError("秘密文件包含未知字段或无效表")
        if any(not isinstance(item, str) or "\x00" in item for item in fields.values()):
            raise SecretError("秘密字段必须是文本")
    return value


def load(path: Path) -> dict[str, dict[str, str]]:
    try:
        owned_files.safe_path(path)
        raw = owned_files.read(path, private=True, limit=1024 * 1024)
    except owned_files.OwnershipError as error:
        raise SecretError("秘密文件的类型、归属或权限不安全") from error
    if raw is None:
        raise SecretError(f"私有秘密文件不存在: {path}")
    info = path.stat()
    if stat.S_IMODE(info.st_mode) != 0o600 or path.parent.stat().st_uid != os.getuid() or path.parent.stat().st_mode & 0o077:
        raise SecretError("秘密文件必须为 0600，父目录必须为当前用户所有且为 0700")
    try:
        value = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError) as error:
        raise SecretError("秘密文件不是有效 TOML") from error
    return _validate(value)


def update(path: Path, updates: dict[str, dict[str, str]]) -> set[str]:
    """Merge credential values without exposing them in the return value."""
    _validate(updates)
    try:
        before = owned_files.read(path, private=True)
    except owned_files.OwnershipError as error:
        raise SecretError("秘密文件的类型、归属或权限不安全") from error
    current = load(path) if before is not None else {}
    merged = {table_name: dict(fields) for table_name, fields in current.items()}
    for table_name, fields in updates.items():
        for field, value in fields.items():
            existing = current.get(table_name, {}).get(field)
            if existing and existing != value:
                raise SecretError(f"秘密字段已有不同值，拒绝覆盖: [{table_name}] {field}")
        merged.setdefault(table_name, {}).update(fields)
    _validate(merged)
    try:
        doc = parse(before.decode("utf-8")) if before is not None else document()
    except (UnicodeError, ParseError) as error:
        raise SecretError("秘密文件不是有效 TOML") from error
    for table_name, fields in updates.items():
        if table_name not in doc:
            doc.add(table_name, table())
        for field, value in fields.items():
            doc[table_name][field] = value
    after = doc.as_string().encode("utf-8")
    try:
        owned_files.replace(path, after, before, mode=0o600)
    except owned_files.OwnershipError as error:
        raise SecretError("秘密文件写入失败或目标在操作期间发生变化") from error
    load(path)
    return {f"{table_name}.{field}" for table_name, fields in updates.items() for field in fields}


def get(path: Path, table: str, field: str) -> str:
    value = load(path).get(table, {}).get(field, "")
    if not value or "\n" in value or "\r" in value:
        raise SecretError(f"缺少必填秘密字段: [{table}] {field}")
    return value

