"""Idempotent user shell PATH entry for npm's user prefix."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


BEGIN = "# pennix-lifecycle:npm-path:begin"
END = "# pennix-lifecycle:npm-path:end"


class PathEnvError(RuntimeError):
    pass


def _target() -> Path:
    return Path.home() / ".zshrc"


def _block(prefix: str) -> str:
    bin_path = str(Path(prefix).expanduser() / "bin")
    return f'{BEGIN}\npath=("{bin_path}" $path)\n{END}'


def _replace(contents: str, block: str) -> str:
    count = contents.count(BEGIN), contents.count(END)
    if count != (0, 0) and count != (1, 1):
        raise PathEnvError("zshrc 中的生命周期 PATH 标记不完整")
    if count == (0, 0):
        bin_path = block.splitlines()[1].split('"')[1]
        if bin_path in contents:
            return contents
        return contents.rstrip("\n") + "\n\n" + block + "\n"
    start = contents.index(BEGIN)
    finish = contents.index(END, start) + len(END)
    return contents[:start] + block + contents[finish:]


def apply(prefix: str) -> str:
    path = _target()
    before = path.read_text(encoding="utf-8") if path.exists() else ""
    after = _replace(before, _block(prefix))
    if after == before:
        return "no-op"
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(after)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return "changed"


def remove() -> str:
    path = _target()
    if not path.exists():
        return "no-op"
    before = path.read_text(encoding="utf-8")
    if BEGIN not in before and END not in before:
        return "preserved"
    if before.count(BEGIN) != 1 or before.count(END) != 1:
        raise PathEnvError("zshrc 中的生命周期 PATH 标记不完整")
    start = before.index(BEGIN)
    finish = before.index(END, start) + len(END)
    after = before[:start].rstrip("\n") + before[finish:]
    path.write_text(after.lstrip("\n"), encoding="utf-8")
    return "changed"
