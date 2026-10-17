"""Optional, fixed-source credential import over a user-selected SSH alias."""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Callable

from secret_store import SecretError, update


ALIAS = re.compile(r"^[A-Za-z0-9._-]+$")
SOURCES = {
    "codex.api_key": (".codex/auth.json", "OPENAI_API_KEY"),
    "siyuan.api_token": (".config/pennix-siyuan/connection.json", "api_token"),
    "cch.admin_token": (".config/cch-codex-tmux-status/cch-admin-token", None),
    "hikari.api_token": (".config/tavily-hikari-cli/config.json", "token"),
    "grok.api_key": (".config/grok-search/config.json", "apiKey"),
    "grok.tavily_api_key": (".config/grok-search/config.json", "tavilyApiKey"),
}


class CredentialImportError(ValueError):
    """The selected fixed credential source could not be imported."""


def _fetch(alias: str, remote: str, destination: Path) -> None:
    if not ALIAS.fullmatch(alias):
        raise CredentialImportError("SSH alias 格式无效")
    batch = f"get ~/{remote} {destination}\n"
    result = subprocess.run(
        ["sftp", "-q", "-b", "-", alias],
        input=batch,
        text=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=30,
        check=False,
    )
    if result.returncode or not destination.is_file():
        raise CredentialImportError(f"SSH 来源不可用: {remote}")


def _extract(path: Path, key: str | None) -> str:
    raw = path.read_text(encoding="utf-8").strip()
    if key is None:
        value = raw
    else:
        try:
            current = json.loads(raw)
        except json.JSONDecodeError as error:
            raise CredentialImportError("SSH 来源不是有效 JSON") from error
        for part in key.split("."):
            if not isinstance(current, dict) or part not in current:
                raise CredentialImportError(f"SSH 来源缺少字段: {key}")
            current = current[part]
        value = current
    if not isinstance(value, str) or not value.strip() or "\x00" in value or "\n" in value or "\r" in value:
        raise CredentialImportError("SSH 来源字段为空或格式无效")
    return value.strip()


def import_fields(alias: str, fields: list[str], path: Path | None = None, fetch: Callable[[str, str, Path], None] = _fetch) -> list[str]:
    if not fields:
        raise CredentialImportError("至少选择一个秘密字段")
    unknown = [field for field in fields if field not in SOURCES]
    if unknown:
        raise CredentialImportError("不支持的秘密字段: " + ", ".join(unknown))
    updates: dict[str, str] = {}
    with tempfile.TemporaryDirectory(prefix="pennix-credential-import-") as directory:
        cache: dict[str, Path] = {}
        for field in dict.fromkeys(fields):
            remote, key = SOURCES[field]
            if remote not in cache:
                target = Path(directory) / str(len(cache))
                fetch(alias, remote, target)
                cache[remote] = target
            updates[field] = _extract(cache[remote], key)
    try:
        return update(path, updates)
    except SecretError as error:
        raise CredentialImportError(str(error)) from error
