"""Paseo's optional user-level systemd service owner."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import owned_files


class PaseoServiceError(RuntimeError):
    pass


LISTEN = re.compile(r"^(?:\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9._-]+):[1-9][0-9]{0,4}$")
OFFICIAL_SKILLS = ("paseo", "paseo-advisor", "paseo-committee", "paseo-handoff", "paseo-help", "paseo-plugin")


def unit_path() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")).expanduser() / "systemd" / "user" / "pennix-paseo.service"


def _receipt_path() -> Path:
    return owned_files.state_root() / "paseo-service.json"


def _unit_digest(paseo: Path, home: Path) -> str:
    return hashlib.sha256(_unit(paseo, home).encode()).hexdigest()


def _receipt() -> dict | None:
    value = owned_files.read_json(_receipt_path())
    if value is None:
        return None
    if (not isinstance(value, dict) or set(value) != {"schema", "paseo", "home", "unit_digest"}
            or value.get("schema") != 1
            or not all(isinstance(value.get(key), str) for key in ("paseo", "home", "unit_digest"))):
        raise PaseoServiceError("Paseo 服务归属记录无效")
    return value


def _owned(paseo: Path, home: Path) -> bool:
    receipt = _receipt()
    return receipt == {
        "schema": 1,
        "paseo": str(paseo),
        "home": str(home),
        "unit_digest": _unit_digest(paseo, home),
    }


def _write_receipt(paseo: Path, home: Path) -> None:
    path = _receipt_path()
    owned_files.replace(
        path,
        owned_files.json_bytes({
            "schema": 1,
            "paseo": str(paseo),
            "home": str(home),
            "unit_digest": _unit_digest(paseo, home),
        }),
        owned_files.read(path, private=True),
    )


def _unit(paseo: Path, home: Path) -> str:
    def quote(value: Path) -> str:
        raw = str(value)
        if re.fullmatch(r"[A-Za-z0-9_./:@+-]+", raw):
            return raw.replace("%", "%%")
        return '"' + raw.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"') + '"'

    return """[Unit]
Description=Pennix Paseo daemon
After=default.target

[Service]
ExecStart={paseo} daemon run --home {home}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
""".format(paseo=quote(paseo), home=quote(home))


def inspect(paseo: Path, home: Path) -> str:
    path = unit_path()
    if not path.exists():
        return "drifted" if _receipt() else "absent"
    if path.is_symlink() or not path.is_file():
        return "blocked"
    if path.read_text(encoding="utf-8") != _unit(paseo, home):
        return "drifted" if _receipt() else "external"
    return "current" if _owned(paseo, home) else "external"


def runtime(paseo: Path, home: Path, mode: str, run: bool) -> dict:
    """Keep unit ownership separate from enablement and the actual daemon."""
    result = {"state": "current", "running": False, "healthy": False,
              "enabled": False, "linger": False, "daemon_version": None}
    try:
        status = subprocess.run([str(paseo), "daemon", "status", "--home", str(home), "--json"],
                                capture_output=True, text=True, timeout=15, check=False)
        if status.returncode:
            raise PaseoServiceError("Paseo daemon status 不可核验")
        value = json.loads(status.stdout)
        if not isinstance(value, dict):
            raise PaseoServiceError("Paseo daemon status 回执无效")
        pid = value.get("pid")
        if pid is not None and (type(pid) is not int or pid <= 0):
            raise PaseoServiceError("Paseo daemon PID 回执无效")
        result["running"] = pid is not None and value.get("home") == str(home)
        result["healthy"] = (
            result["running"] and value.get("localDaemon") == "running"
            and isinstance(value.get("listen"), str) and bool(value["listen"])
            and value.get("connectedDaemon") == "reachable"
        )
        daemon_version = value.get("daemonVersion")
        if daemon_version is not None and not isinstance(daemon_version, str):
            raise PaseoServiceError("Paseo daemon version 回执无效")
        result["daemon_version"] = daemon_version
        if result["running"]:
            os.kill(pid, 0)
        if mode != "off" or unit_path().is_file():
            enabled = subprocess.run(["systemctl", "--user", "is-enabled", unit_path().stem],
                                     capture_output=True, text=True, timeout=15, check=False)
            active = subprocess.run(["systemctl", "--user", "is-active", unit_path().stem],
                                    capture_output=True, text=True, timeout=15, check=False)
            result["enabled"] = enabled.returncode == 0 and enabled.stdout.strip() == "enabled"
            result["active"] = active.returncode == 0 and active.stdout.strip() == "active"
        if mode == "linger":
            linger = subprocess.run(["loginctl", "show-user", str(os.getuid()), "--property=Linger", "--value"],
                                    capture_output=True, text=True, timeout=15, check=False)
            result["linger"] = linger.returncode == 0 and linger.stdout.strip() == "yes"
    except (OSError, ValueError, PaseoServiceError, subprocess.TimeoutExpired):
        result["state"] = "unknown"
        return result
    if (run or mode != "off") and (
            not result["healthy"]
            or mode != "off" and not (result["enabled"] and result.get("active"))
            or mode == "linger" and not result["linger"]):
        result["state"] = "start-required"
    return result


def _systemctl(*args: str) -> None:
    if not shutil.which("systemctl"):
        raise PaseoServiceError("systemctl 不可用")
    result = subprocess.run(
        ["systemctl", "--user", *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise PaseoServiceError(f"systemctl --user 失败: {detail or result.returncode}")


def _run_daemon(paseo: Path, home: Path, action: str) -> None:
    result = subprocess.run(
        [str(paseo), "daemon", action, "--home", str(home), "--json"],
        check=False,
        capture_output=True,
        text=True,
        timeout=600,
    )
    if result.returncode:
        raise PaseoServiceError(f"Paseo daemon {action} 失败")


def _config_get(paseo: Path, home: Path, key: str) -> tuple[bool, object]:
    result = subprocess.run(
        [str(paseo), "daemon", "config", "get", key, "--home", str(home), "--json"],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode:
        raise PaseoServiceError(f"Paseo 配置读取失败: {key}")
    try:
        value = json.loads(result.stdout)
    except (ValueError, UnicodeError) as error:
        raise PaseoServiceError(f"Paseo 配置回执无效: {key}") from error
    if not isinstance(value, dict) or not isinstance(value.get("set"), bool):
        raise PaseoServiceError(f"Paseo 配置回执无效: {key}")
    previous = value.get("value")
    if value["set"] and not isinstance(previous, (str, dict, list, bool, int, float)):
        raise PaseoServiceError(f"Paseo 配置类型无效: {key}")
    return value["set"], previous


def _restore_config(paseo: Path, home: Path, key: str, previous: tuple[bool, object]) -> None:
    if previous[0]:
        value = (previous[1] if isinstance(previous[1], str)
                 else json.dumps(previous[1], ensure_ascii=False, separators=(",", ":")))
        command = [str(paseo), "daemon", "config", "set", key, value]
        if isinstance(previous[1], str):
            command.append("--string")
        command.extend(["--home", str(home), "--json"])
    else:
        command = [str(paseo), "daemon", "config", "unset", key, "--home", str(home), "--json"]
    result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=60)
    if result.returncode or _config_get(paseo, home, key) != previous:
        raise PaseoServiceError(f"Paseo 配置恢复失败: {key}")


def _set_config(paseo: Path, home: Path, key: str, value: object) -> tuple[tuple[bool, object], bool]:
    previous = _config_get(paseo, home, key)
    if previous == (True, value):
        return previous, False
    encoded = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    command = [str(paseo), "daemon", "config", "set", key, encoded]
    if isinstance(value, str):
        command.append("--string")
    command.extend(["--home", str(home), "--json"])
    result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=60)
    try:
        if result.returncode or _config_get(paseo, home, key) != (True, value):
            raise PaseoServiceError(f"Paseo 配置写入或回读核验失败: {key}")
    except BaseException as error:
        try:
            _restore_config(paseo, home, key, previous)
        except BaseException as restore_error:
            raise PaseoServiceError(f"Paseo 配置写入失败且恢复失败: {key}") from restore_error
        raise error
    return previous, True


def _set_listen(paseo: Path, home: Path, listen: str) -> tuple[tuple[bool, object], bool]:
    if not LISTEN.fullmatch(listen):
        raise PaseoServiceError("Paseo 监听地址必须是 host:port 或 [IPv6]:port")
    return _set_config(paseo, home, "daemon.listen", listen)


def configure(paseo: Path, home: Path, listen: str) -> str:
    """Commit only Lifecycle's target daemon fields, preserving them on partial failure."""
    changes: list[tuple[str, tuple[bool, object]]] = []
    try:
        previous, changed = _set_listen(paseo, home, listen)
        if changed:
            changes.append(("daemon.listen", previous))
        previous, changed = _set_config(paseo, home, "agents.skills.selection", {"mode": "all"})
        if changed:
            changes.append(("agents.skills.selection", previous))
    except BaseException as error:
        try:
            for key, previous in reversed(changes):
                _restore_config(paseo, home, key, previous)
        except BaseException as restore_error:
            raise PaseoServiceError("Paseo 目标配置失败且已写入字段恢复失败") from restore_error
        raise error
    return "changed" if changes else "no-op"


def _managed_skills() -> list[str]:
    root = Path.home() / ".agents" / "skills"
    if root.is_symlink() or not root.is_dir():
        return []
    managed = []
    for name in OFFICIAL_SKILLS:
        skill = root / name
        manifest = skill / ".paseo-managed-files.json"
        if skill.is_symlink() or not skill.is_dir() or manifest.is_symlink() or not manifest.is_file():
            continue
        try:
            value = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError):
            continue
        files = value.get("files") if isinstance(value, dict) and value.get("version") == 1 else None
        if not isinstance(files, dict) or not files:
            continue
        skill_root = skill.resolve()
        for relative, expected_hash in files.items():
            if not isinstance(relative, str) or not relative or not isinstance(expected_hash, str):
                break
            try:
                target = (skill / relative).resolve(strict=True)
                target.relative_to(skill_root)
                if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != expected_hash:
                    break
            except (OSError, ValueError):
                break
        else:
            managed.append(name)
    return managed


def observe(paseo: Path, home: Path, listen: str, cli_version: str | None, runtime_state: dict) -> dict:
    result = {
        "state": "unknown",
        "listen": {"state": "unknown", "expected": listen},
        "skills_selection": {"state": "unknown", "expected": {"mode": "all"}},
        "skills_sync": {"state": "unknown", "managed_skills": _managed_skills(),
                        "expected": list(OFFICIAL_SKILLS)},
        "versions": {"cli": cli_version, "daemon": runtime_state.get("daemon_version"), "state": "unknown"},
        "restart_required": False,
    }
    try:
        set_listen, actual_listen = _config_get(paseo, home, "daemon.listen")
        set_selection, actual_selection = _config_get(paseo, home, "agents.skills.selection")
    except (OSError, PaseoServiceError, subprocess.TimeoutExpired):
        return result
    result["listen"]["state"] = "current" if set_listen and actual_listen == listen else "mismatch"
    result["skills_selection"]["state"] = (
        "current" if set_selection and actual_selection == {"mode": "all"} else "mismatch"
    )
    synced = set(result["skills_sync"]["managed_skills"])
    if synced == set(OFFICIAL_SKILLS):
        result["skills_sync"]["state"] = "current"
    elif not synced:
        result["skills_sync"]["state"] = "not-installed"
    else:
        result["skills_sync"]["state"] = "incomplete"
    daemon_version = result["versions"]["daemon"]
    if cli_version and daemon_version:
        result["versions"]["state"] = "current" if cli_version == daemon_version else "mismatch"
    result["restart_required"] = (
        result["listen"]["state"] != "current"
        or result["skills_selection"]["state"] != "current"
        or result["skills_sync"]["state"] == "incomplete"
        or result["versions"]["state"] == "mismatch"
    )
    if (result["listen"]["state"] == "current" and result["skills_selection"]["state"] == "current"
            and result["skills_sync"]["state"] == "current" and result["versions"]["state"] == "current"
            and runtime_state.get("state") == "current"):
        result["state"] = "current"
    else:
        result["state"] = "pending"
    return result


def preflight(home: Path, listen: str, mode: str, run: bool, *, needs_systemd: bool | None = None) -> None:
    if mode not in {"off", "login", "linger"}:
        raise PaseoServiceError(f"未知 Paseo 自启动模式: {mode}")
    if not home.is_absolute():
        raise PaseoServiceError("PASEO_HOME 必须是绝对路径")
    owned_files.safe_path(home)
    parent = next((path for path in (home, *home.parents) if path.exists()), None)
    if parent is None or not parent.is_dir() or not os.access(parent, os.W_OK | os.X_OK):
        raise PaseoServiceError("PASEO_HOME 目标路径不可用")
    if not LISTEN.fullmatch(listen):
        raise PaseoServiceError("Paseo 监听地址格式无效")
    raw_host, raw_port = listen.rsplit(":", 1)
    host = raw_host.strip("[]")
    if int(raw_port) > 65535:
        raise PaseoServiceError("Paseo 监听端口必须在 1 到 65535 之间")
    try:
        if host.lower() != "localhost" and not ipaddress.ip_address(host).is_loopback:
            raise PaseoServiceError("Paseo 监听地址必须绑定 loopback")
    except ValueError as error:
        raise PaseoServiceError("Paseo 监听地址必须是 loopback IP") from error
    if needs_systemd is None:
        needs_systemd = mode != "off"
    if needs_systemd:
        if not shutil.which("systemctl"):
            raise PaseoServiceError("systemctl 不可用")
        manager = subprocess.run(["systemctl", "--user", "show-environment"],
                                 check=False, capture_output=True, text=True, timeout=15)
        if manager.returncode:
            raise PaseoServiceError("systemd user manager 不可用；未执行组件变更")
    if mode == "linger" and not shutil.which("loginctl"):
        raise PaseoServiceError("loginctl 不可用")


def assert_no_active_work(paseo: Path, home: Path) -> dict[str, int]:
    try:
        workspaces = subprocess.run([str(paseo), "workspace", "ls", "--background", "--json", "--home", str(home)],
                                    check=False, capture_output=True, text=True, timeout=30)
        agents = subprocess.run([str(paseo), "ls", "-g", "--background", "--json", "--home", str(home)],
                                check=False, capture_output=True, text=True, timeout=30)
        if workspaces.returncode or agents.returncode:
            raise PaseoServiceError("Paseo workspace/agent 运行态不可核验；拒绝 daemon restart")
        workspace_values = json.loads(workspaces.stdout)
        agent_values = json.loads(agents.stdout)
        if (not isinstance(workspace_values, list) or not isinstance(agent_values, list)
                or any(not isinstance(item, dict) for item in workspace_values + agent_values)
                or any(not isinstance(item.get("status"), str) for item in agent_values)):
            raise PaseoServiceError("Paseo workspace/agent 回执无效；拒绝 daemon restart")
        active_agents = sum(1 for item in agent_values
                            if item["status"] in {"initializing", "idle", "running"})
        counts = {"workspaces": len(workspace_values), "agents": active_agents}
    except (OSError, ValueError, UnicodeError, subprocess.TimeoutExpired) as error:
        raise PaseoServiceError("Paseo workspace/agent 运行态不可核验；拒绝 daemon restart") from error
    if counts["workspaces"] or counts["agents"]:
        raise PaseoServiceError(
            f"存在活动 Paseo workspace/agent（workspace={counts['workspaces']}，agent={counts['agents']}）；拒绝 daemon restart"
        )
    return counts


def _restore_unit(path: Path, before: bytes | None) -> None:
    if before is None:
        path.unlink(missing_ok=True)
        return
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.restore.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(before)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _restore_activation(unit: str, enabled: bool, active: bool) -> None:
    _systemctl("enable" if enabled else "disable", unit)
    _systemctl("start" if active else "stop", unit)


def apply(
    mode: str,
    paseo: Path,
    home: Path,
    listen: str,
    run: bool,
    *,
    restart_daemon: bool = False,
) -> str:
    if mode not in {"off", "login", "linger"}:
        raise PaseoServiceError(f"未知 Paseo 自启动模式: {mode}")
    if mode != "off" and not run:
        raise PaseoServiceError("Paseo 自启动需要同时启用保持运行")
    path = unit_path()
    state = inspect(paseo, home)
    if state in {"blocked", "drifted", "external"}:
        raise PaseoServiceError(f"Paseo unit 状态为 {state}")
    current_runtime = runtime(paseo, home, mode, run)
    if current_runtime["state"] == "unknown" and (run or mode != "off" or state == "current"):
        raise PaseoServiceError("Paseo daemon 运行态不可核验；未修改服务")
    if mode != "off" and state == "absent" and (
            current_runtime.get("enabled") or current_runtime.get("active")):
        raise PaseoServiceError("存在未由 Lifecycle 管理的同名 Paseo user unit")
    if run:
        if _config_get(paseo, home, "daemon.listen") != (True, listen):
            raise PaseoServiceError("Paseo daemon.listen 尚未由独立配置步骤提交")
        if _config_get(paseo, home, "agents.skills.selection") != (True, {"mode": "all"}):
            raise PaseoServiceError("Paseo Skills selection 尚未由独立配置步骤提交")
    if current_runtime["running"] and (restart_daemon or state == "current" and mode == "off"
                                        or mode != "off" and state == "absent"):
        assert_no_active_work(paseo, home)
    if mode == "off":
        changed = False
        if state == "current":
            before = path.read_bytes()
            before_receipt = owned_files.read(_receipt_path(), private=True)
            try:
                _systemctl("disable", "--now", path.stem)
                path.unlink()
                _systemctl("daemon-reload")
                owned_files.replace(_receipt_path(), None, before_receipt)
            except BaseException as error:
                try:
                    _restore_unit(path, before)
                    _systemctl("daemon-reload")
                    _restore_activation(
                        path.stem, current_runtime["enabled"], current_runtime.get("active", False)
                    )
                    if owned_files.read(_receipt_path(), private=True) != before_receipt:
                        owned_files.replace(_receipt_path(), before_receipt, owned_files.read(_receipt_path(), private=True))
                except BaseException as rollback:
                    raise PaseoServiceError("Paseo unit 卸载失败且恢复失败") from rollback
                raise error
            changed = True
        if run:
            if not paseo.is_file():
                raise PaseoServiceError(f"Paseo 可执行文件不存在: {paseo}")
            _run_daemon(paseo, home, "restart" if restart_daemon else "start")
            return "changed" if changed else "started"
        if restart_daemon and state == "absent" and current_runtime["running"]:
            _run_daemon(paseo, home, "restart")
            return "restarted"
        return "changed" if changed else "no-op"
    if not paseo.is_file():
        raise PaseoServiceError(f"Paseo 可执行文件不存在: {paseo}")
    if mode == "linger" and not shutil.which("loginctl"):
        raise PaseoServiceError("loginctl 不可用，无法启用 linger")
    daemon_was_running = current_runtime["running"] and not current_runtime.get("active", False)
    daemon_stop_attempted = False
    activation_attempted = False
    linger_attempted = False
    before = path.read_bytes() if path.exists() else None
    before_receipt = owned_files.read(_receipt_path(), private=True)
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(_unit(paseo, home))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    try:
        _systemctl("daemon-reload")
        if daemon_was_running:
            daemon_stop_attempted = True
            _run_daemon(paseo, home, "stop")
        if restart_daemon and current_runtime["running"] and state == "current":
            activation_attempted = True
            _systemctl("enable", path.stem)
            _systemctl("restart", path.stem)
        else:
            activation_attempted = True
            _systemctl("enable", "--now", path.stem)
        if mode == "linger":
            linger_attempted = True
            result = subprocess.run(
                ["loginctl", "enable-linger", str(os.getuid())],
                check=False,
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode:
                raise PaseoServiceError("无法启用当前用户 linger")
        _write_receipt(paseo, home)
    except BaseException as error:
        try:
            if activation_attempted and before is None:
                _restore_activation(path.stem, False, False)
            _restore_unit(path, before)
            _systemctl("daemon-reload")
            if activation_attempted and before is not None:
                _restore_activation(
                    path.stem, current_runtime["enabled"], current_runtime.get("active", False)
                )
            if owned_files.read(_receipt_path(), private=True) != before_receipt:
                owned_files.replace(_receipt_path(), before_receipt, owned_files.read(_receipt_path(), private=True))
            if linger_attempted and not current_runtime.get("linger", False):
                result = subprocess.run(
                    ["loginctl", "disable-linger", str(os.getuid())],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                if result.returncode:
                    raise PaseoServiceError("无法恢复当前用户 linger")
            if daemon_stop_attempted:
                restored_runtime = runtime(paseo, home, mode, run)
                if restored_runtime["state"] == "unknown":
                    raise PaseoServiceError("无法核验 Paseo daemon 是否需要恢复")
                if not restored_runtime["running"]:
                    _run_daemon(paseo, home, "start")
        except BaseException as rollback:
            raise PaseoServiceError("Paseo unit 启用失败且恢复失败") from rollback
        raise error
    return "changed"


def restart(paseo: Path, home: Path, mode: str) -> dict:
    assert_no_active_work(paseo, home)
    current = runtime(paseo, home, mode, True)
    if current["state"] == "unknown":
        raise PaseoServiceError("Paseo daemon 运行态不可核验")
    if mode == "off":
        _run_daemon(paseo, home, "restart" if current["running"] else "start")
    else:
        if current.get("active"):
            _systemctl("restart", unit_path().stem)
        else:
            _systemctl("enable", "--now", unit_path().stem)
    state = runtime(paseo, home, mode, True)
    if state["state"] != "current":
        raise PaseoServiceError("Paseo daemon 重启后未达到期望运行态")
    return state
