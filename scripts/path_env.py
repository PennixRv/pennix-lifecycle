"""Idempotent user shell PATH entry for npm's user prefix."""

from __future__ import annotations

import os
import pwd
import shlex
from pathlib import Path
import owned_files as files


BEGIN = "# pennix-lifecycle:npm-path:begin"
END = "# pennix-lifecycle:npm-path:end"


class PathEnvError(RuntimeError):
    pass


def bash_login_file(home: Path) -> Path:
    for name in (".bash_profile", ".bash_login", ".profile"):
        path = home / name
        if files.read(path) is not None:
            return path
    return home / ".bash_profile"


def targets() -> tuple[Path, ...]:
    home = Path.home()
    if Path(pwd.getpwuid(os.getuid()).pw_shell).name == "bash":
        return (home / ".bashrc", bash_login_file(home))
    return (home / ".zshrc",)


def _block(prefix: str, path: Path) -> str:
    bin_path = str(Path(prefix).expanduser() / "bin")
    quoted = shlex.quote(bin_path)
    if path.name == ".zshrc":
        return f"{BEGIN}\npath=({quoted} $path)\n{END}"
    return (f'{BEGIN}\ncase ":${{PATH-}}:" in\n'
            f'    *:{quoted}:*) ;;\n    *) export PATH={quoted}${{PATH:+:"$PATH"}} ;;\nesac\n{END}')


def _replace(contents: str, block: str, *, prepend: bool) -> str:
    count = contents.count(BEGIN), contents.count(END)
    if count != (0, 0) and count != (1, 1):
        raise PathEnvError("启动文件中的生命周期 PATH 标记不完整")
    if count == (0, 0):
        if not prepend and block.splitlines()[1] in contents.splitlines():
            return contents
        if prepend:
            return block + "\n" + contents
        return contents.rstrip("\n") + "\n\n" + block + "\n"
    start = contents.index(BEGIN)
    if contents.index(END) < start:
        raise PathEnvError("启动文件中的生命周期 PATH 标记顺序无效")
    finish = contents.index(END, start) + len(END)
    return contents[:start] + block + contents[finish:]


def _changes(prefix: str):
    for path in targets():
        before = files.read(path)
        after = _replace((before or b"").decode(), _block(prefix, path),
                         prepend=path.name != ".zshrc").encode()
        if after != before:
            yield path, before, after


def inspect(prefix: str) -> str:
    return "apply" if not _base_env().is_file() and list(_changes(prefix)) else "keep"


def plan(prefix: Path, *, managed: bool) -> dict:
    paths = (_base_env(),) if managed else targets()
    return {"path": str(paths[0]), "paths": [str(path) for path in paths],
            "prefix": str(prefix), "action": "keep" if managed else inspect(str(prefix))}


def activate(prefix: Path) -> None:
    target = str(prefix / "bin")
    os.environ["PATH"] = os.pathsep.join([target, *[value for value in os.environ.get("PATH", "").split(os.pathsep)
                                                  if value and value != target]])


def apply(prefix: str) -> str:
    if _base_env().is_file():
        return "no-op"
    changes = list(_changes(prefix))
    for path, before, after in changes:
        mode = path.stat().st_mode & 0o777 if before is not None else 0o600
        files.replace(path, after, before, mode=mode)
    return "changed" if changes else "no-op"


def _base_env() -> Path:
    return files.xdg_home("XDG_CONFIG_HOME", Path.home() / ".config") / "pennix/base/env.sh"


def base_targets() -> dict[Path, bytes]:
    """One PATH rule, expressed separately for Shell and environment.d."""
    config = files.xdg_home("XDG_CONFIG_HOME", Path.home() / ".config")
    root = Path(__file__).resolve().parents[1]
    return {
        _base_env(): (root / "templates/base-env.sh.install").read_bytes(),
        config / "environment.d/60-pennix-base.conf": (
            'PATH=${HOME}/.local/bin:${HOME}/.cargo/bin:${HOME}/.go/bin:/usr/local/sbin:/usr/local/bin:/usr/bin\n'
            'LANG=zh_CN.UTF-8\nLC_MESSAGES=en_US.UTF-8\nLANGUAGE=en_US:en:C\nGOPATH=${HOME}/.go\nGOBIN=${HOME}/.go/bin\n'
            'EDITOR=nvim\nVISUAL=nvim\n'
        ).encode(),
    }


def remove() -> str:
    result = "no-op"
    changes = []
    for path in targets():
        raw = files.read(path)
        if raw is None:
            continue
        result = "preserved"
        before = raw.decode()
        if BEGIN not in before and END not in before:
            continue
        after = _replace(before, "", prepend=False)
        changes.append((path, raw, after.lstrip("\n").encode()))
    for path, raw, after in changes:
        files.replace(path, after, raw, mode=path.stat().st_mode & 0o777)
    return "changed" if changes else result
