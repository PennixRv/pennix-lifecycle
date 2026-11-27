"""Shared read-only representation of the native Arch configuration and Hooks."""
import hashlib
import json
import os
from pathlib import Path
import re
import stat


class PolicyError(ValueError):
    pass


HOOKS = "/usr/share/libalpm/hooks"
LOCAL_PACKAGES = Path("/var/lib/pennix-aur")


def trusted_directory(path, *, missing=False):
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts or path.anchor != "/":
        raise PolicyError("Arch 系统目录不是固定的绝对路径")
    for parent in (*reversed(path.parents), path):
        try:
            info = parent.lstat()
        except FileNotFoundError:
            if missing:
                return False
            raise
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise PolicyError("Arch 系统目录或其上级目录的归属不可核验")
    return True


def local_package(name):
    """Read one administrator-approved artifact record, never a caller path."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9@._+-]{0,254}", name):
        raise PolicyError("AUR 包名不符合固定目录规则")
    directory = LOCAL_PACKAGES / name
    trusted_directory(directory)
    path = directory / "approved.json"
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as source:
        info = os.fstat(source.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o6022
                or info.st_nlink != 1 or not 0 < info.st_size <= 4096):
            raise PolicyError("AUR 审批记录不是可信系统文件")
        data = source.read(4097)
    value = json.loads(data)
    fields = {"version": r"[a-zA-Z0-9][a-zA-Z0-9@._+~:-]{0,254}", "ref": r"[0-9a-f]{40}",
              "sha256": r"[0-9a-f]{64}", "signature_sha256": r"[0-9a-f]{64}", "signer": r"[A-F0-9]{40}",
              "filename": re.escape(name) + r"-[a-zA-Z0-9@._+-]+\.(?:pkg\.tar)(?:\.[a-z0-9]{1,12})?"}
    if (len(data) > 4096 or not isinstance(value, dict) or set(value) != fields.keys()
            or any(not isinstance(value[key], str) or not re.fullmatch(pattern, value[key])
                   for key, pattern in fields.items())):
        raise PolicyError("AUR 审批记录的身份字段无效")
    return directory, value


def effective_hooks(directories):
    hooks = {}
    for raw in directories:
        directory = Path(raw)
        if not trusted_directory(directory, missing=True):
            continue
        for hook in sorted(path for path in directory.iterdir() if path.name.endswith(".hook")):
            info = hook.lstat()
            if info.st_uid != 0:
                raise PolicyError("Arch Hook 的系统归属不可核验")
            if stat.S_ISLNK(info.st_mode) and os.readlink(hook) == "/dev/null":
                hooks[hook.name] = {"masked": True}
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022 or info.st_size > 128 * 1024:
                raise PolicyError("Arch Hook 不是可核验的系统文件")
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            with os.fdopen(os.open(hook, flags), "rb") as stream:
                opened = os.fstat(stream.fileno())
                if (not stat.S_ISREG(opened.st_mode) or opened.st_uid != 0 or opened.st_mode & 0o022
                        or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)):
                    raise PolicyError("Arch Hook 在读取前发生变化")
                data = stream.read(128 * 1024 + 1)
            if len(data) > 128 * 1024:
                raise PolicyError("Arch Hook 超过审阅上限")
            hooks[hook.name] = {"sha256": hashlib.sha256(data).hexdigest()}
    return hooks


def snapshot(text):
    if not text.startswith("[options]\n"):
        raise PolicyError("Arch 原生配置无法完整解析")
    options = text.split("\n[", 1)[0].splitlines()
    if [line for line in options if line.startswith("RootDir = ")] != ["RootDir = /"]:
        raise PolicyError("固定 Arch 执行器尚不支持非标准 RootDir")
    directories = [HOOKS, *(line.removeprefix("HookDir = ") for line in options if line.startswith("HookDir = "))]
    # The last definition of a hook name takes precedence.
    return {"pacman_conf_sha256": hashlib.sha256(text.encode()).hexdigest(), "hooks": effective_hooks(directories)}
