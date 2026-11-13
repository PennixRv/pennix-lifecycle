"""Private, hash-pinned tool dependencies; deliberately uses only the stdlib."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess

import owned_files

DIRECTORY = ".runtime"
MANIFEST = ".runtime.json"


class RuntimeError(ValueError):
    pass


def digest(root: Path) -> str:
    owned_files.safe_path(root)
    result = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if "__pycache__" in relative.parts or path.suffix == ".pyc":
            continue
        info = path.lstat()
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise RuntimeError("工具运行环境归属或权限不安全")
        if path.is_symlink():
            if not path.resolve(strict=True).is_relative_to(root):
                raise RuntimeError("工具运行环境包含外部链接")
            value = b"L" + os.fsencode(path.readlink())
        elif path.is_file():
            value = b"F" + str(stat.S_IMODE(info.st_mode)).encode() + hashlib.sha256(path.read_bytes()).digest()
        elif path.is_dir():
            value = b"D"
        else:
            raise RuntimeError("工具运行环境包含特殊文件")
        result.update(os.fsencode(relative) + b"\0" + value + b"\0")
    return result.hexdigest()


def verify(release: Path) -> str:
    record = owned_files.read_json(release / MANIFEST)
    expected_keys = {"schema", "digest", "requirements", "python"}
    if not isinstance(record, dict) or set(record) != expected_keys or record["schema"] != 1:
        raise RuntimeError("工具运行环境记录无效")
    requirements = release / "references/runtime-requirements.txt"
    actual = digest(release / DIRECTORY)
    if record["digest"] != actual or record["requirements"] != hashlib.sha256(requirements.read_bytes()).hexdigest():
        raise RuntimeError("工具运行环境或依赖锁已发生变化")
    return hashlib.sha256(owned_files.json_bytes(record)).hexdigest()


def prepare(release: Path) -> None:
    requirements = release / "references/runtime-requirements.txt"
    if not requirements.is_file():
        return  # Historical payloads and isolated owner fixtures have no private runtime.
    environment = release / DIRECTORY
    manifest = release / MANIFEST
    python_digest = hashlib.sha256(Path("/usr/bin/python3").read_bytes()).hexdigest()
    before = owned_files.read(manifest, private=True)
    if environment.exists() or before is not None:
        verify(release)
        if owned_files.read_json(manifest)["python"] == python_digest:
            return
    backup = release / ".runtime-previous"
    if backup.exists() or backup.is_symlink():
        raise RuntimeError("存在未完成的工具运行环境切换，保留现场")
    if environment.exists():
        os.replace(environment, backup)
    env = {key: value for key, value in os.environ.items() if not key.startswith("UV_")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        # --copies avoids executable links outside the owned tree; uv installs locked dependencies.
        subprocess.run(["/usr/bin/python3", "-m", "venv", "--copies", "--without-pip", str(environment)], check=True, env=env)
        subprocess.run(["/usr/bin/uv", "--no-config", "pip", "install", "--no-python-downloads",
                        "--python", str(environment / "bin/python"), "--default-index", "https://pypi.org/simple",
                        "--require-hashes", "--no-deps", "--only-binary", ":all:", "-r", str(requirements)], check=True, env=env)
        subprocess.run([str(environment / "bin/python"), "-I", "-B", "-c",
                        "import kconfiglib,menuconfig,tomlkit,yaml"], check=True, env=env)
        record = {"schema": 1, "digest": digest(environment), "python": python_digest,
                  "requirements": hashlib.sha256(requirements.read_bytes()).hexdigest()}
        owned_files.replace(manifest, owned_files.json_bytes(record), before)
    except BaseException:
        if environment.exists():
            shutil.rmtree(environment)
        if backup.exists():
            os.replace(backup, environment)
        current = owned_files.read(manifest, private=True)
        if current != before:
            owned_files.replace(manifest, before, current)
        raise
    if backup.exists():
        shutil.rmtree(backup)
