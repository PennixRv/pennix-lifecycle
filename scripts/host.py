"""Detect the supported Arch Linux host and its package installers."""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Callable


WSL_PATTERN = re.compile(r"(?:microsoft|wsl)", re.IGNORECASE)
WSL2_PATTERN = re.compile(r"(?:wsl2|microsoft-standard-wsl2)", re.IGNORECASE)
PACKAGE_MANAGERS = ("pacman", "paru", "yay", "npm")
PACKAGE_NAME = re.compile(r"^[A-Za-z0-9@._+:/-]+$")
PACKAGE_TAG = re.compile(r"^[A-Za-z0-9._-]+$")


def expand_path(value: str) -> str:
    return os.path.expanduser(os.path.expandvars(value))


def read_os_release(path: Path = Path("/etc/os-release")) -> dict[str, str]:
    """Read the small KEY=VALUE format used by os-release."""
    values: dict[str, str] = {}
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return values
    for line in content.splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value.strip().strip('"')
    return values


def read_kernel_markers(
    osrelease_path: Path = Path("/proc/sys/kernel/osrelease"),
    version_path: Path = Path("/proc/version"),
) -> str:
    markers: list[str] = []
    for path in (osrelease_path, version_path):
        try:
            markers.append(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
    return "\n".join(markers)


def detect_environment(markers: str) -> tuple[str, str | None]:
    if not WSL_PATTERN.search(markers):
        return "native", None
    if WSL2_PATTERN.search(markers):
        return "wsl", "2"
    return "wsl", "unknown"


def package_managers(which: Callable[[str], str | None] = shutil.which) -> dict[str, bool]:
    return {name: which(name) is not None for name in PACKAGE_MANAGERS}


def select_installer(source: str, available: dict[str, bool]) -> str | None:
    if source == "official":
        for installer in ("yay", "paru", "pacman"):
            if available.get(installer, False):
                return installer
        return None
    if source == "aur":
        for helper in ("yay", "paru"):
            if available.get(helper, False):
                return helper
        return None
    if source == "npm":
        return "npm" if available.get("npm", False) else None
    return None


def package_info_command(
    installer: str,
    package: str,
    registry: str | None = None,
    tag: str | None = None,
) -> list[str]:
    if installer not in PACKAGE_MANAGERS or not PACKAGE_NAME.fullmatch(package):
        raise ValueError("invalid package installer or package name")
    if installer == "npm":
        if tag is not None and not PACKAGE_TAG.fullmatch(tag):
            raise ValueError("invalid npm package tag")
        package_ref = f"{package}@{tag}" if tag else package
        command = [installer, "view", package_ref, "version", "--json", "--loglevel", "error"]
        return command + (["--registry", registry] if registry else [])
    return [installer, "-Si", package]


def package_install_command(
    installer: str,
    package: str,
    registry: str | None = None,
    prefix: str | None = None,
    allow_scripts: list[str] | None = None,
) -> list[str]:
    if installer not in PACKAGE_MANAGERS or not PACKAGE_NAME.fullmatch(package):
        raise ValueError("invalid package installer or package name")
    if installer == "npm":
        script_option = (
            f"--allow-scripts={','.join(allow_scripts)}"
            if allow_scripts
            else "--ignore-scripts"
        )
        command = [installer, "install", "--global", script_option, "--include=optional"]
        if prefix is not None:
            command += ["--prefix", expand_path(prefix)]
        return command + (["--registry", registry] if registry else []) + [package]
    if installer == "pacman":
        return ["sudo", installer, "-S", "--needed", "--noconfirm", package]
    return [installer, "-S", "--needed", "--noconfirm", package]


def package_remove_command(installer: str, package: str, prefix: str | None = None) -> list[str]:
    if installer not in PACKAGE_MANAGERS or not PACKAGE_NAME.fullmatch(package):
        raise ValueError("invalid package installer or package name")
    if installer == "npm":
        command = [installer, "uninstall", "--global"]
        if prefix is not None:
            command += ["--prefix", expand_path(prefix)]
        return command + [package]
    if installer == "pacman":
        return ["sudo", installer, "-R", "--noconfirm", package]
    return [installer, "-R", "--noconfirm", package]


def detect_host(
    os_release_path: Path = Path("/etc/os-release"),
    osrelease_path: Path = Path("/proc/sys/kernel/osrelease"),
    version_path: Path = Path("/proc/version"),
    which: Callable[[str], str | None] = shutil.which,
) -> dict[str, object]:
    release = read_os_release(os_release_path)
    environment, wsl_version = detect_environment(read_kernel_markers(osrelease_path, version_path))
    available = package_managers(which)
    supported = release.get("ID") == "arch" and (
        environment == "native" or wsl_version == "2"
    )
    if release.get("ID") != "arch":
        reason = "lifecycle currently supports Arch Linux only"
    elif environment == "wsl" and wsl_version != "2":
        reason = "Arch Linux on WSL requires WSL2"
    else:
        reason = None
    return {
        "supported": supported,
        "reason": reason,
        "os": {"id": release.get("ID"), "version_id": release.get("VERSION_ID")},
        "environment": environment,
        "wsl_version": wsl_version,
        "package_managers": available,
        "installers": {
            "official": select_installer("official", available),
            "aur": select_installer("aur", available),
            "npm": select_installer("npm", available),
        },
    }
