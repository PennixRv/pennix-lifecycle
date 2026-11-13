"""Idempotent user shell PATH entry for npm's user prefix."""

from __future__ import annotations

import shlex
import os
from pathlib import Path
import owned_files as files


BEGIN = "# pennix-lifecycle:npm-path:begin"
END = "# pennix-lifecycle:npm-path:end"


class PathEnvError(RuntimeError):
    pass


def _target() -> Path:
    return Path.home() / ".zshrc"


def _block(prefix: str) -> str:
    bin_path = str(Path(prefix).expanduser() / "bin")
    return f"{BEGIN}\npath=({shlex.quote(bin_path)} $path)\n{END}"


def _replace(contents: str, block: str) -> str:
    count = contents.count(BEGIN), contents.count(END)
    if count != (0, 0) and count != (1, 1):
        raise PathEnvError("zshrc 中的生命周期 PATH 标记不完整")
    if count == (0, 0):
        if block.splitlines()[1] in contents.splitlines():
            return contents
        return contents.rstrip("\n") + "\n\n" + block + "\n"
    start = contents.index(BEGIN)
    if contents.index(END) < start:
        raise PathEnvError("zshrc 中的生命周期 PATH 标记顺序无效")
    finish = contents.index(END, start) + len(END)
    return contents[:start] + block + contents[finish:]


def inspect(prefix: str) -> str:
    if _base_env().is_file():
        return "keep"
    raw = files.read(_target())
    before = raw.decode("utf-8") if raw is not None else ""
    return "keep" if _replace(before, _block(prefix)) == before else "apply"


def activate(prefix: Path) -> None:
    target = str(prefix / "bin")
    os.environ["PATH"] = os.pathsep.join([target, *[value for value in os.environ.get("PATH", "").split(os.pathsep)
                                                  if value and value != target]])


def apply(prefix: str) -> str:
    if _base_env().is_file():
        return "no-op"
    path = _target()
    raw = files.read(path)
    before = raw.decode("utf-8") if raw is not None else ""
    after = _replace(before, _block(prefix))
    if after == before:
        return "no-op"
    mode = path.stat().st_mode & 0o777 if raw is not None else 0o600
    files.replace(path, after.encode(), raw, mode=mode)
    return "changed"


def _base_env() -> Path:
    return files.xdg_home("XDG_CONFIG_HOME", Path.home() / ".config") / "pennix/base/env.sh"


def base_targets() -> dict[Path, bytes]:
    """One PATH rule, expressed separately for Shell and environment.d."""
    config = files.xdg_home("XDG_CONFIG_HOME", Path.home() / ".config")
    root = Path(__file__).resolve().parents[1]
    return {
        _base_env(): (root / "templates/base-env.sh.install").read_bytes(),
        config / "environment.d/60-pennix-base.conf": (
            'PATH=${HOME}/.local/bin:${HOME}/.cargo/bin:${HOME}/go/bin:/usr/local/sbin:/usr/local/bin:/usr/bin\n'
            'LANG=zh_CN.UTF-8\nLC_MESSAGES=en_US.UTF-8\nLANGUAGE=en_US:en:C\nGOBIN=${HOME}/go/bin\n'
        ).encode(),
    }


def remove() -> str:
    path = _target()
    raw = files.read(path)
    if raw is None:
        return "no-op"
    before = raw.decode("utf-8")
    if BEGIN not in before and END not in before:
        return "preserved"
    if before.count(BEGIN) != 1 or before.count(END) != 1:
        raise PathEnvError("zshrc 中的生命周期 PATH 标记不完整")
    start = before.index(BEGIN)
    finish = before.index(END, start) + len(END)
    after = before[:start].rstrip("\n") + before[finish:]
    files.replace(path, after.lstrip("\n").encode(), raw, mode=path.stat().st_mode & 0o777)
    return "changed"
