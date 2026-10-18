"""The fixed owner contract for the existing NAS/router Paseo relay."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import tempfile
from pathlib import Path


class RelayError(RuntimeError):
    """A relay registration cannot be changed safely."""


SERVER_ID = re.compile(r"^[A-Za-z0-9._:-]{1,256}$")
ENDPOINT = re.compile(r"^(?:\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9._-]+):[1-9][0-9]{0,4}$")
SSH_ALIAS = re.compile(r"^[A-Za-z0-9_.@:-]+$")
MAP_PATH = "/etc/nginx/conf.d/snippets/paseo-relay-allowed-servers.map"
_MISSING = object()


def _state_path() -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")).expanduser()
    return base / "pennix-lifecycle" / "relay.json"


def _server_id(paseo_home: Path) -> str:
    path = paseo_home / "server-id"
    if path.is_symlink() or not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise RelayError("Paseo server-id 不存在或权限不安全")
    value = path.read_text(encoding="utf-8").strip()
    if not SERVER_ID.fullmatch(value):
        raise RelayError("Paseo server-id 格式不受支持")
    return value


def _record() -> dict[str, str] | None:
    path = _state_path()
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise RelayError("relay 归属记录不安全")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RelayError("relay 归属记录无效") from error
    if not isinstance(value, dict) or value.get("schema") != 1 or not isinstance(value.get("server_id"), str):
        raise RelayError("relay 归属记录版本不受支持")
    if not SERVER_ID.fullmatch(value["server_id"]):
        raise RelayError("relay 归属记录身份无效")
    return {"server_id": value["server_id"]}


def local_owned() -> bool:
    return _record() is not None


def _write_record(server_id: str) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump({"schema": 1, "server_id": server_id}, handle)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _ssh(
    action: str,
    server_id: str,
    nas_ssh: str = "nas",
    router_ssh: str = "home-main-router",
) -> str:
    if not SERVER_ID.fullmatch(server_id):
        raise RelayError("server-id 格式不受支持")
    if not SSH_ALIAS.fullmatch(nas_ssh) or not SSH_ALIAS.fullmatch(router_ssh):
        raise RelayError("SSH 目标别名格式不受支持")
    script = r'''#!/bin/sh
set -eu
action=$1
server_id=$2
map=/etc/nginx/conf.d/snippets/paseo-relay-allowed-servers.map
escaped=$(printf '%s' "$server_id" | sed 's/\./\\./g')
plain_line="$server_id 1;"
regex_line="~^${escaped}$ 1;"
test -f "$map" || { echo missing; exit 43; }
plain_count=$(grep -F -x -- "$plain_line" "$map" | wc -l)
regex_count=$(grep -F -x -- "$regex_line" "$map" | wc -l)
count=$((plain_count + regex_count))
test "$count" -le 1 || { echo duplicate; exit 44; }
if [ "$action" = inspect ]; then
  [ "$count" -eq 1 ] && echo present || echo absent
  exit 0
fi
lock="$map.pennix-lock"
if ! mkdir "$lock" 2>/dev/null; then
  echo busy >&2
  exit 47
fi
tmp=$(mktemp "$map.pennix-tmp.XXXXXX")
backup=$(mktemp "$map.pennix-backup.XXXXXX")
trap 'rm -f "$tmp" "$backup"; rmdir "$lock" 2>/dev/null || true' EXIT
cp -p "$map" "$tmp"
cp -p "$map" "$backup"
if [ "$action" = add ]; then
  if [ "$count" -eq 1 ]; then echo present; exit 0; fi
  printf '%s\n' "$regex_line" >> "$tmp"
elif [ "$action" = remove ]; then
  if [ "$count" -eq 0 ]; then echo absent; exit 0; fi
  awk -v plain="$plain_line" -v regex="$regex_line" '$0 != plain && $0 != regex { print }' "$map" > "$tmp"
else
  echo invalid >&2
  exit 45
fi
mv "$tmp" "$map"
if ! nginx -t >/dev/null 2>&1 || ! nginx -s reload >/dev/null 2>&1; then
  if ! cp -p "$backup" "$map"; then
    echo restore-failed >&2
    exit 48
  fi
  nginx -t >/dev/null 2>&1 && nginx -s reload >/dev/null 2>&1 || true
  exit 46
fi
[ "$action" = add ] && echo added || echo removed
'''
    result = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", nas_ssh, "ssh", "-o", "BatchMode=yes", router_ssh, "sh", "-s", "--", action, server_id],
        input=script,
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
    )
    output = result.stdout.strip()
    if result.returncode:
        if output == "duplicate":
            raise RelayError("远端 relay 登记包含重复身份")
        raise RelayError("远端 NAS/router relay owner 不可用")
    if output not in {"present", "absent", "added", "removed"}:
        raise RelayError("远端 relay owner 回执无效")
    return output


def inspect(
    paseo_home: Path,
    *,
    nas_ssh: str = "nas",
    router_ssh: str = "home-main-router",
) -> str:
    return _ssh("inspect", _server_id(paseo_home), nas_ssh, router_ssh)


def ensure(
    paseo_home: Path,
    *,
    nas_ssh: str = "nas",
    router_ssh: str = "home-main-router",
) -> str:
    server_id = _server_id(paseo_home)
    result = _ssh("add", server_id, nas_ssh, router_ssh)
    if result == "added":
        try:
            _write_record(server_id)
        except BaseException as error:
            try:
                _ssh("remove", server_id, nas_ssh, router_ssh)
            except RelayError as rollback:
                raise RelayError("relay 已登记但本地归属记录写入失败，且撤回也失败") from rollback
            raise RelayError("relay 登记成功但本地归属记录写入失败，已撤回远端登记") from error
        return "owned"
    if result == "present":
        return "external"
    raise RelayError("relay 登记未达到预期状态")


def remove(
    paseo_home: Path,
    *,
    nas_ssh: str = "nas",
    router_ssh: str = "home-main-router",
) -> str:
    record = _record()
    if record is None:
        return "preserved"
    server_id = _server_id(paseo_home)
    if record["server_id"] != server_id:
        raise RelayError("relay 归属身份与当前 Paseo home 不匹配")
    result = _ssh("remove", server_id, nas_ssh, router_ssh)
    if result in {"removed", "absent"}:
        _state_path().unlink(missing_ok=True)
        return "changed" if result == "removed" else "no-op"
    raise RelayError("relay 登记未达到预期撤回状态")


def validate_config(endpoint: str, public_endpoint: str) -> None:
    if not ENDPOINT.fullmatch(endpoint) or not ENDPOINT.fullmatch(public_endpoint):
        raise RelayError("relay endpoint 必须是 host:port 或 [IPv6]:port")


def _daemon_values(paseo: Path, paseo_home: Path) -> dict[str, object]:
    if not paseo.is_file():
        raise RelayError("Paseo 可执行文件不存在")
    values = {}
    for path in ("daemon.relay.enabled", "daemon.relay.endpoint", "daemon.relay.publicEndpoint",
                 "daemon.relay.useTls", "daemon.relay.publicUseTls"):
        result = subprocess.run(
            [str(paseo), "daemon", "config", "get", path, "--home", str(paseo_home), "--json"],
            capture_output=True, text=True, check=False, timeout=60,
        )
        if result.returncode:
            raise RelayError(f"Paseo relay 配置读取失败: {path}")
        try:
            response = json.loads(result.stdout)
        except (ValueError, UnicodeError) as error:
            raise RelayError(f"Paseo relay 配置回执无效: {path}") from error
        if not isinstance(response, dict) or not isinstance(response.get("set"), bool):
            raise RelayError(f"Paseo relay 配置回执无效: {path}")
        values[path] = response["value"] if response["set"] else _MISSING
    return values


def inspect_daemon(paseo: Path, paseo_home: Path, endpoint: str, public_endpoint: str,
                   tls: bool, public_tls: bool) -> str:
    validate_config(endpoint, public_endpoint)
    values = _daemon_values(paseo, paseo_home)
    expected = {
        "daemon.relay.enabled": True,
        "daemon.relay.endpoint": endpoint,
        "daemon.relay.publicEndpoint": public_endpoint,
        "daemon.relay.useTls": tls,
        "daemon.relay.publicUseTls": public_tls,
    }
    if values == expected:
        return "current"
    return "missing" if all(value is _MISSING for value in values.values()) else "drifted"


def configure_daemon(paseo: Path, paseo_home: Path, endpoint: str, public_endpoint: str, tls: bool, public_tls: bool) -> str:
    validate_config(endpoint, public_endpoint)
    _daemon_values(paseo, paseo_home)

    def set_value(path: str, value: str, literal: bool) -> None:
        command = [str(paseo), "daemon", "config", "set", path, value, "--home", str(paseo_home)]
        if literal:
            command.append("--string")
        result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=60)
        if result.returncode:
            raise RelayError(f"Paseo relay 配置写入失败: {path}")

    def restore_value(path: str, value) -> None:
        if value is _MISSING:
            command = [str(paseo), "daemon", "config", "unset", path, "--home", str(paseo_home), "--json"]
        else:
            literal = isinstance(value, str)
            raw = value if literal else json.dumps(value)
            command = [str(paseo), "daemon", "config", "set", path, raw, "--home", str(paseo_home)]
            if literal:
                command.append("--string")
        result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=60)
        if result.returncode:
            raise RelayError(f"Paseo relay 配置恢复失败: {path}")

    paths = ["daemon.relay.enabled", "daemon.relay.endpoint", "daemon.relay.publicEndpoint",
             "daemon.relay.useTls", "daemon.relay.publicUseTls"]
    baseline = _daemon_values(paseo, paseo_home)
    try:
        set_value("daemon.relay.enabled", "false", False)
        set_value("daemon.relay.endpoint", endpoint, True)
        set_value("daemon.relay.publicEndpoint", public_endpoint, True)
        set_value("daemon.relay.useTls", "true" if tls else "false", False)
        set_value("daemon.relay.publicUseTls", "true" if public_tls else "false", False)
        expected = {
            "daemon.relay.enabled": False,
            "daemon.relay.endpoint": endpoint,
            "daemon.relay.publicEndpoint": public_endpoint,
            "daemon.relay.useTls": tls,
            "daemon.relay.publicUseTls": public_tls,
        }
        for path, value in expected.items():
            observed = _daemon_values(paseo, paseo_home)[path]
            if observed is _MISSING or observed != value:
                raise RelayError(f"Paseo relay 配置回读不一致: {path}")
        set_value("daemon.relay.enabled", "true", False)
        if _daemon_values(paseo, paseo_home)["daemon.relay.enabled"] is not True:
            raise RelayError("Paseo relay enabled 配置回读不一致")
    except BaseException as error:
        try:
            for path in reversed(paths):
                restore_value(path, baseline[path])
        except BaseException as rollback:
            raise RelayError("Paseo relay 配置失败且恢复失败") from rollback
        raise RelayError("Paseo relay 配置失败，已恢复原配置") from error
    return "changed"


def restart_daemon(paseo: Path, paseo_home: Path) -> None:
    result = subprocess.run([str(paseo), "daemon", "restart", "--home", str(paseo_home), "--json"], capture_output=True, text=True, check=False, timeout=600)
    if result.returncode:
        raise RelayError("Paseo worker 重启失败")
