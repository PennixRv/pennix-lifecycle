"""Paseo's optional user-level systemd service owner."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path


class PaseoServiceError(RuntimeError):
    pass


def unit_path() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")).expanduser() / "systemd" / "user" / "pennix-paseo.service"


def _unit(paseo: Path, home: Path) -> str:
    return """[Unit]
Description=Pennix Paseo daemon
After=default.target

[Service]
ExecStart={paseo} daemon run --home {home}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
""".format(paseo=paseo, home=home)


def inspect(paseo: Path, home: Path) -> str:
    path = unit_path()
    if not path.exists():
        return "absent"
    if path.is_symlink() or not path.is_file():
        return "blocked"
    return "current" if path.read_text(encoding="utf-8") == _unit(paseo, home) else "drifted"


def _systemctl(*args: str) -> None:
    if not shutil.which("systemctl"):
        raise PaseoServiceError("systemctl 不可用")
    result = subprocess.run(["systemctl", "--user", *args], check=False, capture_output=True, text=True)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise PaseoServiceError(f"systemctl --user 失败: {detail or result.returncode}")


def apply(mode: str, paseo: Path, home: Path) -> str:
    path = unit_path()
    state = inspect(paseo, home)
    if state == "blocked" or state == "drifted":
        raise PaseoServiceError(f"Paseo unit 状态为 {state}")
    if mode == "off":
        if state == "absent":
            return "no-op"
        _systemctl("disable", "--now", path.stem)
        path.unlink()
        _systemctl("daemon-reload")
        return "changed"
    if not paseo.is_file():
        raise PaseoServiceError(f"Paseo 可执行文件不存在: {paseo}")
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
    _systemctl("daemon-reload")
    _systemctl("enable", "--now", path.stem)
    if mode == "linger":
        if not shutil.which("loginctl"):
            raise PaseoServiceError("loginctl 不可用，无法启用 linger")
        result = subprocess.run(["loginctl", "enable-linger", str(os.getuid())], check=False)
        if result.returncode:
            raise PaseoServiceError("无法启用当前用户 linger")
    return "changed"
