"""Paseo's optional user-level systemd service owner."""

from __future__ import annotations

import os
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import owned_files


class PaseoServiceError(RuntimeError):
    pass


LISTEN = re.compile(r"^(?:\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9._-]+):[1-9][0-9]{0,4}$")


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
    result = {"state": "current", "running": False, "enabled": False, "linger": False}
    if not run and mode == "off":
        return result
    try:
        status = subprocess.run([str(paseo), "daemon", "status", "--home", str(home), "--json"],
                                capture_output=True, text=True, timeout=15, check=False)
        value = json.loads(status.stdout) if status.returncode == 0 else {}
        result["running"] = (isinstance(value.get("pid"), int) and value["pid"] > 0
                             and value.get("home") == str(home))
        if result["running"]:
            os.kill(value["pid"], 0)
        if mode != "off":
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
    except (OSError, ValueError, subprocess.TimeoutExpired):
        result["state"] = "unknown"
        return result
    if not result["running"] or mode != "off" and not (result["enabled"] and result.get("active")) or mode == "linger" and not result["linger"]:
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


def _config_get(paseo: Path, home: Path) -> tuple[bool, str | None]:
    result = subprocess.run(
        [str(paseo), "daemon", "config", "get", "daemon.listen", "--home", str(home), "--json"],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode:
        raise PaseoServiceError("Paseo 监听地址读取失败")
    try:
        value = json.loads(result.stdout)
    except (ValueError, UnicodeError) as error:
        raise PaseoServiceError("Paseo 监听地址回执无效") from error
    if not isinstance(value, dict) or not isinstance(value.get("set"), bool):
        raise PaseoServiceError("Paseo 监听地址回执无效")
    previous = value.get("value")
    if value["set"] and not isinstance(previous, str):
        raise PaseoServiceError("Paseo 监听地址类型无效")
    return value["set"], previous


def _set_listen(paseo: Path, home: Path, listen: str) -> tuple[bool, str | None]:
    if not LISTEN.fullmatch(listen):
        raise PaseoServiceError("Paseo 监听地址必须是 host:port 或 [IPv6]:port")
    previous = _config_get(paseo, home)
    result = subprocess.run(
        [str(paseo), "daemon", "config", "set", "daemon.listen", listen,
         "--string", "--home", str(home), "--json"],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode:
        raise PaseoServiceError("Paseo 监听地址写入失败")
    return previous


def _restore_listen(paseo: Path, home: Path, previous: tuple[bool, str | None]) -> None:
    if previous[0]:
        command = [str(paseo), "daemon", "config", "set", "daemon.listen", previous[1] or "", "--string", "--home", str(home), "--json"]
    else:
        command = [str(paseo), "daemon", "config", "unset", "daemon.listen", "--home", str(home), "--json"]
    result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise PaseoServiceError("Paseo 监听地址恢复失败")


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
                    _systemctl("enable", "--now", path.stem)
                    if owned_files.read(_receipt_path(), private=True) != before_receipt:
                        owned_files.replace(_receipt_path(), before_receipt, owned_files.read(_receipt_path(), private=True))
                except BaseException as rollback:
                    raise PaseoServiceError("Paseo unit 卸载失败且恢复失败") from rollback
                raise error
            changed = True
        if run:
            if not paseo.is_file():
                raise PaseoServiceError(f"Paseo 可执行文件不存在: {paseo}")
            _set_listen(paseo, home, listen)
            _run_daemon(paseo, home, "restart" if restart_daemon else "start")
            return "changed" if changed else "started"
        return "changed" if changed else "no-op"
    if not paseo.is_file():
        raise PaseoServiceError(f"Paseo 可执行文件不存在: {paseo}")
    if mode == "linger" and not shutil.which("loginctl"):
        raise PaseoServiceError("loginctl 不可用，无法启用 linger")
    previous_listen = _set_listen(paseo, home, listen)
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
        if restart_daemon and state == "current":
            _systemctl("enable", path.stem)
            _systemctl("restart", path.stem)
        else:
            _systemctl("enable", "--now", path.stem)
        if mode == "linger":
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
            _restore_unit(path, before)
            _systemctl("daemon-reload")
            _restore_listen(paseo, home, previous_listen)
            if owned_files.read(_receipt_path(), private=True) != before_receipt:
                owned_files.replace(_receipt_path(), before_receipt, owned_files.read(_receipt_path(), private=True))
        except BaseException as rollback:
            raise PaseoServiceError("Paseo unit 启用失败且恢复失败") from rollback
        raise error
    return "changed"
