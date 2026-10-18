"""Idempotent user shell PATH entry for npm's user prefix."""

from __future__ import annotations

import shlex
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
        if block.splitlines()[1] in contents:
            return contents
        return contents.rstrip("\n") + "\n\n" + block + "\n"
    start = contents.index(BEGIN)
    finish = contents.index(END, start) + len(END)
    return contents[:start] + block + contents[finish:]


def apply(prefix: str) -> str:
    path = _target()
    raw = files.read(path)
    before = raw.decode("utf-8") if raw is not None else ""
    after = _replace(before, _block(prefix))
    if after == before:
        return "no-op"
    mode = path.stat().st_mode & 0o777 if raw is not None else 0o600
    files.replace(path, after.encode(), raw, mode=mode)
    return "changed"


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
