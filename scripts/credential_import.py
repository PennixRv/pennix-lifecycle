"""Import only known credential fields from fixed files over an existing SSH alias."""
from __future__ import annotations

import json
from pathlib import Path
import re
import os
import selectors
import shlex
import signal
import subprocess
import tempfile
import time
import tomllib
from collections.abc import Callable

import secret_store
import owned_files


class CredentialImportError(RuntimeError):
    pass


SOURCES = {
    "codex": (".codex/auth.json", "json", {"OPENAI_API_KEY": "api_key"}),
    "siyuan": (".config/pennix-siyuan/connection.json", "json", {"api_token": "api_token"}),
    "cch": (".config/cch-codex-tmux-status/cch-admin-token", "text", {"": "admin_token"}),
    "windsurf": (".config/windsurf-code-search/config.json", "json", {"apiKey": "api_key"}),
}
ALIAS = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.@:-]*$")
LIMIT = 1024 * 1024


def _fetch_remote(alias: str, remote_path: str) -> bytes:
    if not ALIAS.fullmatch(alias):
        raise CredentialImportError("SSH alias 只能包含主机别名字符")
    with tempfile.TemporaryDirectory(prefix="pennix-credential-") as directory:
        destination = Path(directory) / "source"
        # SFTP starts in the login home; '~' needs an optional server extension.
        batch = f"get {remote_path} {destination}\n"
        try:
            result = subprocess.run(
                ["sftp", "-q", "-oBatchMode=yes", "-oStrictHostKeyChecking=yes", "-b", "-", alias],
                input=batch,
                text=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise CredentialImportError("SSH 凭据来源不可用") from error
        if result.returncode or not destination.is_file() or destination.stat().st_size > LIMIT:
            raise CredentialImportError("SSH 凭据来源不可用")
        return destination.read_bytes()


def _extract(service: str, raw: bytes) -> dict[str, dict[str, str]]:
    remote_path, kind, fields = SOURCES[service]
    if kind == "text":
        try:
            value = raw.decode("utf-8").strip()
        except UnicodeError as error:
            raise CredentialImportError(f"{service} 凭据文件编码无效") from error
        values = {"": value}
    else:
        try:
            value = json.loads(raw.decode("utf-8"), object_pairs_hook=owned_files.unique_pairs)
        except (UnicodeError, ValueError, owned_files.OwnershipError) as error:
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


def read_from_ssh(
    alias: str,
    services: list[str],
    *,
    fetch: Callable[[str, str], bytes] = _fetch_remote,
) -> dict[str, dict[str, str]]:
    if not ALIAS.fullmatch(alias):
        raise CredentialImportError("SSH alias 只能包含主机别名字符")
    selected = list(dict.fromkeys(services))
    if not selected or any(service not in SOURCES for service in selected):
        raise CredentialImportError("必须选择已登记的凭据来源")
    updates: dict[str, dict[str, str]] = {}
    for service in selected:
        remote_path = SOURCES[service][0]
        extracted = _extract(service, fetch(alias, remote_path))
        updates.setdefault(service, {}).update(extracted[service])
    return updates


def import_from_ssh(path: Path, alias: str, services: list[str], *, fetch=_fetch_remote) -> set[str]:
    return secret_store.update(path, read_from_ssh(alias, services, fetch=fetch))


def required_fields(kconf, catalog: dict) -> list[str]:
    import config_model
    fields = set()
    selected = config_model.selected_components(kconf)
    if "codex-config" in selected and config_model.config_value(kconf, "PENNIX_CODEX_AUTH_API_KEY") == "y":
        fields.add("codex.api_key")
    elif "codex-config" in selected and config_model.config_value(kconf, 'PENNIX_CODEX_PROVIDER_CUSTOM') != 'y':
        raw = owned_files.read(Path(config_model.config_value(kconf, 'PENNIX_CODEX_HOME')) / 'config.toml')
        try:
            doc = tomllib.loads(raw.decode()) if raw else {}
        except (ValueError, UnicodeError) as error:
            raise secret_store.SecretError('保留的 Codex 配置无法解析；不能推断秘密需求') from error
        providers = doc.get('model_providers', {})
        if not isinstance(providers, dict) or not isinstance(doc.get('model_provider', ''), str):
            raise secret_store.SecretError('保留的 Codex provider 格式无效')
        provider = providers.get(doc.get('model_provider'), {})
        auth = provider.get('auth', {}) if isinstance(provider, dict) else {}
        if not isinstance(auth, dict):
            raise secret_store.SecretError('保留的 Codex auth 格式无效')
        if (auth.get('command') == str(Path.home()/'.local/bin/pennix') and auth.get('args') ==
                ['secrets', 'codex-token', '--secrets-file', config_model.config_value(kconf, 'PENNIX_SECRETS_FILE')]):
            fields.add('codex.api_key')
    for name, spec in catalog["configuration_targets"].items():
        if config_model.config_value(kconf, spec["select"]) != "y":
            continue
        if name == "windsurf" and config_model.config_value(kconf, "PENNIX_WINDSURF_OWNER_CREDENTIAL") == "y":
            continue
        fields.update(spec.get("secrets", {}).values())
    return sorted(fields)


def _fetch_lifecycle(alias: str, fields: list[str]) -> bytes:
    if not ALIAS.fullmatch(alias):
        raise CredentialImportError("SSH alias 格式无效")
    names = secret_store.field_names(fields)
    command = 'exec "$HOME/.local/bin/pennix" secrets export --private-stdout'
    command += ''.join(' --field ' + shlex.quote(name) for name in names)
    process = subprocess.Popen(["ssh", "-oBatchMode=yes", "-oStrictHostKeyChecking=yes",
                                "-oControlMaster=no", "-oControlPath=none", "--", alias, command],
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               start_new_session=True)
    chunks = bytearray()
    deadline = time.monotonic() + 60
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CredentialImportError("SSH 私有导出超时；未写入秘密")
                for key, _ in selector.select(remaining):
                    block = os.read(key.fd, min(65536, LIMIT + 1 - len(chunks)))
                    if not block:
                        selector.unregister(key.fileobj)
                    else:
                        chunks.extend(block)
                        if len(chunks) > LIMIT:
                            raise CredentialImportError("SSH 私有导出超过大小限制；未写入秘密")
        if process.wait(timeout=max(0.1, deadline - time.monotonic())):
            raise CredentialImportError("SSH 私有导出失败；未写入秘密")
        return bytes(chunks)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CredentialImportError("SSH 私有导出未完成；未写入秘密") from error
    finally:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait()
        process.stdout.close()


def read_from_lifecycle(alias: str, fields: list[str], *, fetch=_fetch_lifecycle) -> dict:
    if not ALIAS.fullmatch(alias):
        raise CredentialImportError("SSH alias 格式无效")
    names = secret_store.field_names(fields)
    try:
        raw = fetch(alias, names)
        if len(raw) > LIMIT:
            raise ValueError()
        value = json.loads(raw, object_pairs_hook=owned_files.unique_pairs)
        if (not isinstance(value, dict) or set(value) != {"schema", "fields"}
                or type(value["schema"]) is not int or value["schema"] != 1):
            raise ValueError()
        values = secret_store._validate(value["fields"])
        received = {f"{table}.{key}" for table, row in values.items() for key in row}
        if received != set(names) or any(not item or "\n" in item or "\r" in item
                                       for row in values.values() for item in row.values()):
            raise ValueError()
        return values
    except (ValueError, TypeError, UnicodeError, secret_store.SecretError, owned_files.OwnershipError) as error:
        raise CredentialImportError("SSH 私有导出格式或字段集合无效；未写入秘密") from error
