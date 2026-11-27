"""Host requirements for the selected systems-development Skill collection."""

import hashlib
import json
import os
from pathlib import Path
import re
import shutil

import owned_files
import update_inventory


SCOPE = "system-devel-tools"
STEP = f"{SCOPE}:packages"
MANIFEST = Path(__file__).resolve().parents[1] / "references" / f"{SCOPE}.json"
NAME = re.compile(r"[a-z0-9][a-z0-9_.+-]*")
COMMAND = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]*")


class ToolError(ValueError):
    pass


def requirements():
    raw = owned_files.read(MANIFEST, limit=64 * 1024)
    try:
        data = json.loads(raw or b"null", object_pairs_hook=owned_files.unique_pairs)
    except (ValueError, UnicodeError) as error:
        raise ToolError("系统开发工具声明不是有效 JSON") from error
    if (not isinstance(data, dict) or set(data) != {"schema", "packages", "conditional"}
            or type(data["schema"]) is not int or data["schema"] != 1
            or not isinstance(data["packages"], dict) or not data["packages"]
            or not isinstance(data["conditional"], dict)):
        raise ToolError("系统开发工具声明格式无效")
    commands = set()
    for package, names in data["packages"].items():
        if (not NAME.fullmatch(package) or not isinstance(names, list) or not names
                or any(not isinstance(name, str) or not COMMAND.fullmatch(name) for name in names)
                or len(names) != len(set(names)) or commands.intersection(names)):
            raise ToolError("系统开发工具声明含无效或重复入口")
        commands.update(names)
    if any(name not in commands or not isinstance(value, str) or not value.strip()
           for name, value in data["conditional"].items()):
        raise ToolError("系统开发工具的条件能力声明无效")
    return data


def installed_packages():
    result = update_inventory.run(["/usr/bin/pacman", "-Q"], noninteractive=True)
    if result.returncode:
        raise ToolError("无法核验 Arch 软件包数据库")
    rows = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2:
            raise ToolError("Arch 软件包数据库返回了无法核验的记录")
        name, version = parts
        if name in rows:
            raise ToolError("Arch 软件包数据库出现重复包名")
        rows[name] = version
    return rows


def _owner(path):
    result = update_inventory.run(["/usr/bin/pacman", "-Qqo", "--", str(path)], noninteractive=True)
    return result.stdout.strip() if result.returncode == 0 else None


def inspect(selected, installed=None):
    if not selected:
        return {"selected": False, "state": "not-selected", "missing": [], "packages": {},
                "conditional": {}, "functional": "not-run"}
    data = requirements()
    installed = installed_packages() if installed is None else installed
    packages, missing = {}, []
    for package, names in data["packages"].items():
        version = installed.get(package)
        entries = {}
        if version is None:
            missing.append(package)
        else:
            for name in names:
                path = Path("/usr/bin") / name
                visible = shutil.which(name)
                if not path.is_file() or not os.access(path, os.X_OK) or _owner(path) != package:
                    state = "unverified-system-command"
                elif not visible:
                    state = "not-on-path"
                elif Path(visible).resolve() != path.resolve():
                    state = "path-override"
                else:
                    state = "system-command"
                entries[name] = {"state": state, "system": str(path), "visible": visible}
        package_state = ("missing" if version is None else "entry-mismatch"
                         if any(item["state"] != "system-command" for item in entries.values()) else "present")
        packages[package] = {"state": package_state, "installed": version, "commands": names, "entries": entries}
    state = ("entry-mismatch" if any(row["state"] == "entry-mismatch" for row in packages.values())
             else "missing" if missing else "ready")
    return {"selected": True, "state": state, "missing": sorted(missing), "packages": packages,
            "conditional": data["conditional"], "functional": "not-run"}


def review(missing):
    report = update_inventory.arch(include_packages=tuple(missing))
    if report["state"] != "current":
        raise ToolError(report.get("detail", "Arch 官方包计划不可用"))
    names = sorted(missing)
    if names != sorted(set(requirements()["packages"]) - report["installed"].keys()):
        raise ToolError("软件包在预览期间变化；请重新审阅")
    return {"requested": names, "before": report["installed"],
            "transaction": report["transaction"], "real_sync_sha256": report["real_sync_sha256"],
            "manifest_sha256": manifest_digest()}


def manifest_digest():
    raw = owned_files.read(MANIFEST, limit=64 * 1024)
    if raw is None:
        raise ToolError("系统开发工具声明缺失")
    return hashlib.sha256(raw).hexdigest()


def expected_packages(target):
    return {**target["before"], **{row["name"]: row["target"] for row in target["transaction"]}}
