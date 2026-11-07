"""Paseo's optional user-level systemd service owner."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import shutil
import subprocess
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

[Service]
Type=oneshot
ExecStart={paseo} daemon start --home {home}
RemainAfterExit=yes
Restart=no
KillMode=process

[Install]
WantedBy=default.target
""".format(paseo=quote(paseo), home=quote(home))


def _legacy_unit(paseo: Path, home: Path) -> str:
    return _unit(paseo, home).replace("Description=Pennix Paseo daemon\n", "Description=Pennix Paseo daemon\nAfter=default.target\n").replace("Type=oneshot\n", "").replace(
        " daemon start --home ", " daemon run --home ").replace(
        "RemainAfterExit=yes\nRestart=no\nKillMode=process", "Restart=on-failure\nRestartSec=5")


def inspect(paseo: Path, home: Path) -> str:
    path = unit_path()
    if not path.exists():
        return "drifted" if _receipt() else "absent"
    if path.is_symlink() or not path.is_file():
        return "blocked"
    content = path.read_text(encoding="utf-8")
    receipt = _receipt()
    if content == _legacy_unit(paseo, home) and receipt == {
            "schema": 1, "paseo": str(paseo), "home": str(home),
            "unit_digest": hashlib.sha256(content.encode()).hexdigest()}:
        return "migration-required"
    if content != _unit(paseo, home):
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
        result["pid"] = pid
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
            unit = subprocess.run(["systemctl", "--user", "show", unit_path().stem,
                                   "--property=LoadState", "--property=UnitFileState", "--property=ActiveState", "--property=Result"],
                                  capture_output=True, text=True, timeout=15, check=False)
            fields = dict(line.split("=", 1) for line in unit.stdout.splitlines() if "=" in line)
            if (unit.returncode or fields.get("LoadState") not in {"loaded", "not-found"}
                    or fields.get("ActiveState") not in {"active", "inactive", "failed"}
                    or fields.get("UnitFileState") not in {"enabled", "disabled", "static", ""}
                    or "Result" not in fields):
                raise PaseoServiceError("Paseo user unit 运行态不可核验")
            result["enabled"] = fields["UnitFileState"] == "enabled"
            result["active"] = fields["ActiveState"] == "active"
            result["unit_failed"] = fields["ActiveState"] == "failed" or fields["Result"] != "success"
        if mode == "linger":
            linger = subprocess.run(["loginctl", "show-user", str(os.getuid()), "--property=Linger", "--value"],
                                    capture_output=True, text=True, timeout=15, check=False)
            if linger.returncode or linger.stdout.strip() not in {"yes", "no"}:
                raise PaseoServiceError("Paseo linger 不可核验")
            result["linger"] = linger.stdout.strip() == "yes"
    except (OSError, ValueError, PaseoServiceError, subprocess.TimeoutExpired):
        result["state"] = "unknown"
        return result
    if ((run and not result["healthy"]) or (result["running"] and not result["healthy"])
            or mode != "off" and (not result["enabled"] or result.get("unit_failed"))
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
    if not runtime_state.get("running") and runtime_state.get("state") != "unknown":
        result["versions"]["state"] = "not_run"
    elif cli_version and daemon_version:
        result["versions"]["state"] = "current" if cli_version == daemon_version else "mismatch"
    result["restart_required"] = bool(runtime_state.get("running")) and (
        result["listen"]["state"] != "current"
        or result["skills_selection"]["state"] != "current"
        or result["skills_sync"]["state"] == "incomplete"
        or result["versions"]["state"] == "mismatch"
    )
    if (result["listen"]["state"] == "current" and result["skills_selection"]["state"] == "current"
            and result["skills_sync"]["state"] == "current" and result["versions"]["state"] in {"current", "not_run"}
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


def apply(
    mode: str, paseo: Path, home: Path, listen: str, run: bool, *,
    restart_daemon: bool = False,
) -> str:
    if mode not in {"off", "login", "linger"}:
        raise PaseoServiceError(f"未知 Paseo 自启动模式: {mode}")
    path = unit_path()
    state = inspect(paseo, home)
    if state in {"blocked", "drifted", "external"}:
        raise PaseoServiceError(f"Paseo unit 状态为 {state}")
    current = runtime(paseo, home, mode, run)
    if current["state"] == "unknown":
        raise PaseoServiceError("Paseo daemon 或 user manager 不可核验；未修改服务")
    if state == "absent" and (current.get("enabled") or current.get("active")):
        raise PaseoServiceError("存在未由 Lifecycle 管理的同名 Paseo user unit")
    if run or restart_daemon:
        if (_config_get(paseo, home, "daemon.listen") != (True, listen)
                or _config_get(paseo, home, "agents.skills.selection") != (True, {"mode": "all"})):
            raise PaseoServiceError("Paseo 目标配置尚未由独立配置步骤提交")
    legacy_active = state == "migration-required" and current.get("active", False)
    if legacy_active:
        group = subprocess.run(
            ["systemctl", "--user", "show", path.stem, "--property=ControlGroup", "--value"],
            capture_output=True, text=True, timeout=15, check=False)
        pid = current.get("pid")
        if (group.returncode or not group.stdout.strip() or not pid
                or not any(line.split(":", 2)[-1] == group.stdout.strip()
                           for line in Path(f"/proc/{pid}/cgroup").read_text().splitlines())):
            raise PaseoServiceError("旧前台 unit 与 daemon 归属不可核验；拒绝中断性迁移")
    if current["running"] and (restart_daemon or legacy_active):
        assert_no_active_work(paseo, home)
    if mode != "off" and not paseo.is_file():
        raise PaseoServiceError(f"Paseo 可执行文件不存在: {paseo}")
    before = owned_files.read(path)
    before_receipt = owned_files.read(_receipt_path(), private=True)
    changed = False
    stopped_legacy = False
    linger_added = False
    try:
        if legacy_active:
            stopped_legacy = True
            _systemctl("stop", path.stem)
        if mode == "off":
            if state != "absent":
                # Disabling autostart never uses --now on a native daemon launcher.
                _systemctl("disable", path.stem)
                owned_files.replace(path, None, before)
                _systemctl("daemon-reload")
                owned_files.replace(_receipt_path(), None, before_receipt)
                changed = True
        else:
            desired = _unit(paseo, home).encode()
            if before != desired:
                owned_files.replace(path, desired, before)
                _systemctl("daemon-reload")
                changed = True
            if not current["enabled"]:
                _systemctl("enable", path.stem)
                changed = True
            if mode == "linger" and not current.get("linger"):
                result = subprocess.run(["loginctl", "enable-linger", str(os.getuid())],
                                        capture_output=True, text=True, timeout=60, check=False)
                if result.returncode:
                    raise PaseoServiceError("无法启用当前用户 linger")
                linger_added = True
                changed = True
            _write_receipt(paseo, home)
        if stopped_legacy or (restart_daemon and current["running"]):
            _run_daemon(paseo, home, "start" if stopped_legacy else "restart")
            changed = True
        elif run and not current["healthy"]:
            _run_daemon(paseo, home, "start")
            changed = True
    except BaseException as error:
        try:
            actual = owned_files.read(path)
            expected = None if mode == "off" else _unit(paseo, home).encode()
            if actual not in (before, expected):
                raise PaseoServiceError("Paseo unit 并发变化，拒绝覆盖恢复")
            if before is None and actual is not None:
                _systemctl("disable", path.stem)
            owned_files.replace(path, before, actual)
            if mode != "off" or state != "absent":
                _systemctl("daemon-reload")
                if before is not None:
                    _systemctl("enable" if current["enabled"] else "disable", path.stem)
            receipt_now = owned_files.read(_receipt_path(), private=True)
            if receipt_now != before_receipt:
                expected_receipt = None if mode == "off" else owned_files.json_bytes({
                    "schema": 1, "paseo": str(paseo), "home": str(home),
                    "unit_digest": _unit_digest(paseo, home),
                })
                if receipt_now != expected_receipt:
                    raise PaseoServiceError("Paseo receipt 并发变化，拒绝覆盖恢复")
                owned_files.replace(_receipt_path(), before_receipt, receipt_now)
            if linger_added:
                result = subprocess.run(["loginctl", "disable-linger", str(os.getuid())],
                                        capture_output=True, text=True, timeout=60, check=False)
                if result.returncode:
                    raise PaseoServiceError("无法恢复当前用户 linger")
            if stopped_legacy:
                _systemctl("start", path.stem)
        except BaseException as rollback:
            raise PaseoServiceError("Paseo 自启配置失败且恢复失败") from rollback
        raise error
    return "changed" if changed else "no-op"


def restart(paseo: Path, home: Path, mode: str) -> dict:
    current = runtime(paseo, home, mode, False)
    if current["state"] == "unknown":
        raise PaseoServiceError("Paseo daemon 运行态不可核验")
    if current["running"]:
        assert_no_active_work(paseo, home)
        _run_daemon(paseo, home, "restart")
    else:
        return current
    state = runtime(paseo, home, mode, False)
    if not state.get("healthy"):
        raise PaseoServiceError("Paseo daemon 重启后未达到期望运行态")
    return state
