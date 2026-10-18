"""Import only known credential fields from fixed files over an existing SSH alias."""
from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import tempfile
from collections.abc import Callable

import secret_store


class CredentialImportError(RuntimeError):
    pass


SOURCES = {
    "codex": (".codex/auth.json", "json", {"OPENAI_API_KEY": "api_key"}),
    "siyuan": (".config/pennix-siyuan/connection.json", "json", {"api_token": "api_token"}),
    "cch": (".config/cch-codex-tmux-status/cch-admin-token", "text", {"": "admin_token"}),
    "hikari": (".config/tavily-hikari-cli/config.json", "json", {"token": "api_token"}),
    "grok": (".config/grok-search/config.json", "json", {"apiKey": "api_key", "tavilyApiKey": "tavily_api_key"}),
    "windsurf": (".config/windsurf-code-search/config.json", "json", {"apiKey": "api_key"}),
}
ALIAS = re.compile(r"^[A-Za-z0-9_.@:-]+$")


def _fetch_remote(alias: str, remote_path: str) -> bytes:
    if not ALIAS.fullmatch(alias):
        raise CredentialImportError("SSH alias 只能包含主机别名字符")
    with tempfile.TemporaryDirectory(prefix="pennix-credential-") as directory:
        destination = Path(directory) / "source"
        batch = f"get ~/{remote_path} {destination}\n"
        try:
            result = subprocess.run(
                ["sftp", "-q", "-b", "-", alias],
                input=batch,
                text=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise CredentialImportError("SSH 凭据来源不可用") from error
        if result.returncode or not destination.is_file():
            raise CredentialImportError("SSH 凭据来源不可用")
        return destination.read_bytes()


def _extract(service: str, raw: bytes) -> dict[str, dict[str, str]]:
    remote_path, kind, fields = SOURCES[service]
    if kind == "text":
        value = raw.decode("utf-8").strip()
        values = {"": value}
    else:
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as error:
            raise CredentialImportError(f"{service} 凭据文件格式无效") from error
        if not isinstance(value, dict):
            raise CredentialImportError(f"{service} 凭据文件格式无效")
        values = value
    updates = {}
    for source_field, target_field in fields.items():
        candidate = values.get(source_field, "")
        if isinstance(candidate, str) and candidate and "\x00" not in candidate and "\n" not in candidate and "\r" not in candidate:
            updates[target_field] = candidate
    if not updates:
        raise CredentialImportError(f"SSH 来源缺少 {service} 凭据")
    return {service: updates}


def import_from_ssh(
    path: Path,
    alias: str,
    services: list[str],
    *,
    fetch: Callable[[str, str], bytes] = _fetch_remote,
) -> set[str]:
    selected = list(dict.fromkeys(services))
    if not selected or any(service not in SOURCES for service in selected):
        raise CredentialImportError("必须选择已登记的凭据来源")
    updates: dict[str, dict[str, str]] = {}
    for service in selected:
        remote_path = SOURCES[service][0]
        extracted = _extract(service, fetch(service, remote_path))
        updates.setdefault(service, {}).update(extracted[service])
    return secret_store.update(path, updates)
