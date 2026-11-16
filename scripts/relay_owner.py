"""The fixed owner contract for the existing NAS/router Paseo relay."""

from __future__ import annotations

import json
import hashlib
import os
import re
import stat
import shlex
import subprocess
import tempfile
from pathlib import Path

import owned_files


class RelayError(RuntimeError):
    """A relay registration cannot be changed safely."""


SERVER_ID = re.compile(r"^[A-Za-z0-9._:-]{1,256}$")
ENDPOINT = re.compile(r"^(?:\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9._-]+):[1-9][0-9]{0,4}$")
SSH_ALIAS = re.compile(r"^[A-Za-z0-9_.@:-]+$")
MAP_PATH = "/etc/nginx/conf.d/snippets/paseo-relay-allowed-servers.map"
_MISSING = object()


def _state_path() -> Path:
    base = owned_files.xdg_home("XDG_STATE_HOME", Path.home() / ".local/state")
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
    if not isinstance(value, dict) or value.get("schema") not in {1, 2} or not isinstance(value.get("server_id"), str):
        raise RelayError("relay 归属记录版本不受支持")
    if not SERVER_ID.fullmatch(value["server_id"]):
        raise RelayError("relay 归属记录身份无效")
    if value["schema"] == 1:
        if set(value) != {"schema", "server_id"}:
            raise RelayError("旧 relay 归属记录字段无效")
        return value
    if set(value) != {"schema", "server_id", "target", "adopted_current"} or type(value["adopted_current"]) is not bool:
        raise RelayError("relay 归属记录字段无效")
    _validate_target(value["target"])
    return value


def _validate_target(target):
    if (not isinstance(target, dict) or set(target) != {"nas_id", "router_id", "uid", "resource"}
            or any(not isinstance(target[k], str) or not re.fullmatch(r"[0-9a-f]{64}", target[k]) for k in ["nas_id", "router_id"])
            or target["uid"] != "0" or target["resource"] != MAP_PATH):
        raise RelayError("relay 实际远端身份或资源无效")


def local_owned() -> bool:
    return _record() is not None


def _daemon_receipt(paseo_home: Path) -> tuple[Path, dict | None]:
    path = owned_files.state_root() / "paseo-relay-daemon.json"
    record = owned_files.read_json(path)
    keys = {"daemon.relay.enabled", "daemon.relay.endpoint", "daemon.relay.publicEndpoint",
            "daemon.relay.useTls", "daemon.relay.publicUseTls"}
    if record is not None and (not isinstance(record, dict) or set(record) != {"schema", "home", "before", "desired"}
            or record["schema"] != 1 or record["home"] != str(paseo_home)
            or not isinstance(record["before"], dict) or not isinstance(record["desired"], dict)):
        raise RelayError("本地 relay 配置归属记录不匹配")
    if record is not None and (set(record["before"]) != keys or set(record["desired"]) != keys
            or any(not isinstance(previous, dict) or set(previous) != {"present", "value"}
                   or type(previous["present"]) is not bool
                   or previous["value"] is not None and not isinstance(previous["value"], (str, bool))
                   for previous in record["before"].values())):
        raise RelayError("本地 relay 配置归属字段不匹配")
    return path, record


def daemon_owned(paseo_home: Path) -> bool:
    return _daemon_receipt(paseo_home)[1] is not None


def unconfigure_daemon(paseo: Path, paseo_home: Path) -> str:
    path, record = _daemon_receipt(paseo_home)
    if record is None:
        return "preserved external local relay configuration"
    raw = owned_files.read(path, private=True)
    if _daemon_values(paseo, paseo_home) != record["desired"]:
        raise RelayError("本地 relay 配置已漂移，未撤回")
    for key, previous in reversed(list(record["before"].items())):
        args = [str(paseo), "daemon", "config"]
        if not previous["present"]:
            args += ["unset", key, "--json"]
        else:
            value = previous["value"]
            args += ["set", key, value if isinstance(value, str) else json.dumps(value)]
            if isinstance(value, str):
                args.append("--string")
        result = subprocess.run([*args, "--home", str(paseo_home)], capture_output=True, timeout=60, check=False)
        if result.returncode:
            raise RelayError("本地 relay 撤回未完成，保留事务和归属记录")
    observed = _daemon_values(paseo, paseo_home)
    if any((observed[key] is not _MISSING if not previous["present"] else observed[key] != previous["value"])
           for key, previous in record["before"].items()):
        raise RelayError("本地 relay 撤回后置条件不符")
    owned_files.replace(path, None, raw)
    return "changed local relay configuration"


def _write_record(server_id: str, target: dict, *, adopted=False) -> None:
    _validate_target(target)
    path = _state_path()
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump({"schema": 2, "server_id": server_id, "target": target, "adopted_current": adopted}, handle)
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
    *, expected: dict | None = None,
) -> str | dict:
    if not SERVER_ID.fullmatch(server_id):
        raise RelayError("server-id 格式不受支持")
    if not SSH_ALIAS.fullmatch(nas_ssh) or not SSH_ALIAS.fullmatch(router_ssh):
        raise RelayError("SSH 目标别名格式不受支持")
    if expected is not None:
        _validate_target(expected)
    script = r'''#!/bin/sh
set -eu
action=$1
server_id=$2
expected_router=$3
map=/etc/nginx/conf.d/snippets/paseo-relay-allowed-servers.map
if [ -r /etc/ssh/ssh_host_ed25519_key.pub ]; then
  public=$(awk 'NR == 1 { print $1 " " $2 }' /etc/ssh/ssh_host_ed25519_key.pub)
elif command -v dropbearkey >/dev/null 2>&1 && [ -r /etc/dropbear/dropbear_ed25519_host_key ]; then
  public=$(dropbearkey -y -f /etc/dropbear/dropbear_ed25519_host_key | awk '$1 == "ssh-ed25519" { print $1 " " $2 }')
else
  echo no-host-identity >&2; exit 49
fi
test -n "$public" || exit 49
router_id=$(printf '%s' "$public" | sha256sum | cut -d ' ' -f 1)
uid=$(id -u)
test "$uid" = 0 || exit 49
[ "$expected_router" = - ] || test "$expected_router" = "$router_id" || exit 49
printf 'router:%s:%s\n' "$router_id" "$uid"
if [ "$action" = identity ]; then echo identity; exit 0; fi
escaped=$(printf '%s' "$server_id" | sed 's/\./\\./g')
plain_line="$server_id 1;"
regex_line="~^${escaped}$ 1;"
lock="$map.pennix-lock"
if ! mkdir "$lock" 2>/dev/null; then
  echo busy >&2
  exit 47
fi
trap 'rmdir "$lock" 2>/dev/null || true' EXIT
test -f "$map" && test ! -L "$map" || { echo missing; exit 43; }
plain_count=$(grep -F -x -- "$plain_line" "$map" | wc -l)
regex_count=$(grep -F -x -- "$regex_line" "$map" | wc -l)
count=$((plain_count + regex_count))
test "$count" -le 1 || { echo duplicate; exit 44; }
if [ "$action" = inspect ]; then
  [ "$count" -eq 1 ] && echo present || echo absent
  exit 0
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
    remote = 'set -eu; test -r /etc/machine-id; nas_id=$(sha256sum /etc/machine-id | cut -d " " -f 1); '
    if expected is not None:
        remote += 'test "$nas_id" = ' + shlex.quote(expected["nas_id"]) + '; '
    remote += 'printf "nas:%s\\n" "$nas_id"; exec ' + shlex.join([
        "ssh", "-o", "BatchMode=yes", router_ssh, "sh", "-s", "--", action, server_id,
        expected["router_id"] if expected else "-"])
    result = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", nas_ssh, remote],
        input=script,
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
    )
    lines = result.stdout.strip().splitlines()
    output = lines[-1] if lines else ""
    if result.returncode:
        if output == "duplicate":
            raise RelayError("远端 relay 登记包含重复身份")
        raise RelayError("远端 NAS/router relay owner 不可用")
    if len(lines) != 3 or not lines[0].startswith("nas:") or not lines[1].startswith("router:"):
        raise RelayError("远端 relay 身份回执无效")
    router = lines[1].split(":")
    if len(router) != 3:
        raise RelayError("远端 router 身份回执无效")
    target = {"nas_id": lines[0][4:], "router_id": router[1], "uid": router[2], "resource": MAP_PATH}
    _validate_target(target)
    if expected is not None and target != expected:
        raise RelayError("relay 实际远端发生变化；未接受替代目标")
    if action == "identity" and output == "identity":
        return target
    if output not in {"present", "absent", "added", "removed"}:
        raise RelayError("远端 relay owner 回执无效")
    return output


def inspect(
    paseo_home: Path,
    *,
    nas_ssh: str = "nas",
    router_ssh: str = "home-main-router",
) -> str:
    record = _record()
    if record and record["schema"] == 1:
        return "adoption-required"
    server_id = _server_id(paseo_home)
    if record and record["server_id"] != server_id:
        raise RelayError("relay 归属身份与当前 Paseo home 不匹配")
    return _ssh("inspect", server_id, nas_ssh, router_ssh, expected=record["target"] if record else None)


def review(paseo_home: Path, *, nas_ssh="nas", router_ssh="home-main-router") -> dict:
    server_id = _server_id(paseo_home)
    target = _ssh("identity", server_id, nas_ssh, router_ssh)
    state = _ssh("inspect", server_id, nas_ssh, router_ssh, expected=target)
    value = {"server_id": server_id, "target": target, "state": state}
    return {**value, "review_sha256": hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()}


def adopt_current(paseo_home: Path, expected_sha256: str, *, nas_ssh="nas", router_ssh="home-main-router") -> str:
    record = _record()
    value = review(paseo_home, nas_ssh=nas_ssh, router_ssh=router_ssh)
    if (not record or record["schema"] != 1 or record["server_id"] != value["server_id"]
            or value["state"] != "present" or value["review_sha256"] != expected_sha256):
        raise RelayError("当前 relay 登记审阅不匹配；未迁移旧归属")
    _write_record(value["server_id"], value["target"], adopted=True)
    return "adopted reviewed current registration; historical remote remains unproven"


def ensure(
    paseo_home: Path,
    *,
    nas_ssh: str = "nas",
    router_ssh: str = "home-main-router",
) -> str:
    server_id = _server_id(paseo_home)
    record = _record()
    if record and (record["schema"] != 2 or record["server_id"] != server_id):
        raise RelayError("relay 既有归属不匹配或未绑定远端；先审阅修复")
    target = record["target"] if record else _ssh("identity", server_id, nas_ssh, router_ssh)
    result = _ssh("add", server_id, nas_ssh, router_ssh, expected=target)
    if result == "added":
        try:
            _write_record(server_id, target)
        except BaseException as error:
            try:
                _ssh("remove", server_id, nas_ssh, router_ssh, expected=target)
            except RelayError as rollback:
                raise RelayError("relay 已登记但本地归属记录写入失败，且撤回也失败") from rollback
            raise RelayError("relay 登记成功但本地归属记录写入失败，已撤回远端登记") from error
        return "owned"
    if result == "present":
        return "owned" if record else "external"
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
    if record["schema"] != 2:
        raise RelayError("旧 relay receipt 不证明原远端；拒绝撤回，先审阅当前目标")
    server_id = _server_id(paseo_home)
    if record["server_id"] != server_id:
        raise RelayError("relay 归属身份与当前 Paseo home 不匹配")
    result = _ssh("remove", server_id, nas_ssh, router_ssh, expected=record["target"])
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
        live = _live_relay(paseo, paseo_home)
        if live is not None and live != expected:
            return "configuration-required"
        return "current"
    _, receipt = _daemon_receipt(paseo_home)
    if receipt and values == receipt["desired"]:
        return "configuration-required"
    # Native first start persists enabled=false before any relay endpoint is set.
    if receipt is None and values.get("daemon.relay.enabled") is False and all(
            value is _MISSING for key, value in values.items() if key != "daemon.relay.enabled"):
        return "missing"
    if any(value is not _MISSING for value in values.values()):
        raise RelayError("本地 relay 配置缺少当前归属证据；未修改外部配置")
    return "missing" if all(value is _MISSING for value in values.values()) else "drifted"


def _live_relay(paseo: Path, home: Path) -> dict | None:
    result = subprocess.run([str(paseo), "daemon", "status", "--home", str(home), "--json"],
                            capture_output=True, text=True, check=False, timeout=30)
    try:
        value = json.loads(result.stdout)
        if result.returncode or not isinstance(value, dict) or value.get("home") != str(home) or "pid" not in value:
            raise ValueError()
        if value["pid"] is None:
            return None
        if (type(value["pid"]) is not int or value["pid"] <= 0 or value.get("localDaemon") != "running"
                or value.get("connectedDaemon") != "reachable" or not isinstance(value.get("relay"), dict)):
            raise ValueError()
        relay = value["relay"]
        return {"daemon.relay." + key: relay[key] for key in ["enabled", "endpoint", "publicEndpoint", "useTls", "publicUseTls"]}
    except (ValueError, KeyError, TypeError) as error:
        raise RelayError("Paseo relay 实际运行态无法核验；保留现场") from error


def configure_daemon(paseo: Path, paseo_home: Path, endpoint: str, public_endpoint: str, tls: bool, public_tls: bool) -> str:
    validate_config(endpoint, public_endpoint)
    if inspect_daemon(paseo, paseo_home, endpoint, public_endpoint, tls, public_tls) == "current":
        return "no-op"

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
    receipt_path, receipt = _daemon_receipt(paseo_home)
    before_receipt = owned_files.read(receipt_path, private=True)
    if receipt and baseline != receipt["desired"]:
        raise RelayError("本地 relay 配置与受管记录不符")
    live_before = _live_relay(paseo, paseo_home)
    if live_before is not None:
        import paseo_service
        paseo_service.assert_no_active_work(paseo, paseo_home)
    restart_attempted = False
    try:
        set_value("daemon.relay.enabled", "false", False)
        if live_before is not None and _live_relay(paseo, paseo_home)["daemon.relay.enabled"] is not False:
            raise RelayError("Paseo relay 未实际禁用；未继续改 endpoint")
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
        if live_before is not None and _live_relay(paseo, paseo_home) != expected:
            restart_attempted = True
            restart_daemon(paseo, paseo_home)
            if _live_relay(paseo, paseo_home) != expected:
                raise RelayError("Paseo relay 准备配置在重启后未实际生效")
        set_value("daemon.relay.enabled", "true", False)
        if _daemon_values(paseo, paseo_home)["daemon.relay.enabled"] is not True:
            raise RelayError("Paseo relay enabled 配置回读不一致")
        desired = {**expected, "daemon.relay.enabled": True}
        if live_before is not None and _live_relay(paseo, paseo_home) != desired:
            raise RelayError("Paseo relay 启用后的实际运行态不符")
        record = {"schema": 1, "home": str(paseo_home), "desired": desired,
                  "before": receipt["before"] if receipt else {
                      key: {"present": value is not _MISSING, "value": None if value is _MISSING else value}
                      for key, value in baseline.items()}}
        owned_files.replace(receipt_path, owned_files.json_bytes(record), before_receipt)
    except BaseException as error:
        try:
            set_value("daemon.relay.enabled", "false", False)
            for path in reversed(paths[1:]):
                restore_value(path, baseline[path])
            if restart_attempted:
                restart_daemon(paseo, paseo_home)
            restore_value(paths[0], baseline[paths[0]])
            if live_before is not None and _live_relay(paseo, paseo_home) != live_before:
                raise RelayError("Paseo relay 原运行态未恢复")
        except BaseException as rollback:
            raise RelayError("Paseo relay 配置失败且恢复失败") from rollback
        raise RelayError("Paseo relay 配置失败，已恢复原配置") from error
    return "changed"


def restart_daemon(paseo: Path, paseo_home: Path) -> None:
    result = subprocess.run([str(paseo), "daemon", "restart", "--home", str(paseo_home), "--json"], capture_output=True, text=True, check=False, timeout=600)
    if result.returncode:
        raise RelayError("Paseo worker 重启失败")
