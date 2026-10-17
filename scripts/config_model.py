"""Strict Kconfig-backed workflow configuration."""

from __future__ import annotations

import os
import re
import stat
import tempfile
from pathlib import Path

import kconfiglib


ROOT = Path(__file__).resolve().parents[1]
KCONFIG_FILE = ROOT / "config" / "Kconfig"
FORMAT_VERSION = "1"
HEADER = f"# Pennix workflow.config format: {FORMAT_VERSION}\n"
COMPONENT_PREFIX = "PENNIX_COMPONENT_"
COMPONENT_IDS = {
    "PENNIX_COMPONENT_CODEX_CLI": "codex-cli",
    "PENNIX_COMPONENT_TRELLIS_CLI": "trellis-cli",
    "PENNIX_COMPONENT_PNPM": "pnpm",
    "PENNIX_COMPONENT_FASTCTX": "fastctx",
    "PENNIX_COMPONENT_CODEGRAPH_CLI": "codegraph-cli",
    "PENNIX_COMPONENT_PASEO_CLI": "paseo-cli",
    "PENNIX_COMPONENT_PONYTAIL_PLUGIN": "ponytail-plugin",
    "PENNIX_COMPONENT_CCH_STATUS": "cch-status",
    "PENNIX_COMPONENT_TMUX": "tmux",
    "PENNIX_COMPONENT_TMUX_CONFIG": "tmux-config",
    "PENNIX_COMPONENT_AOE": "aoe",
    "PENNIX_COMPONENT_TAVILY_HIKARI": "tavily-hikari",
    "PENNIX_COMPONENT_CODEX_CONFIG": "codex-config",
    "PENNIX_COMPONENT_CODEX_AGENTS": "codex-agents",
    "PENNIX_COMPONENT_PENNIX_SKILLS_EXTRA": "pennix-skills-extra",
    "PENNIX_COMPONENT_PENNIX_SKILLS": "pennix-skills",
}
PRESERVE_HIDDEN_VALUES = {
    "PENNIX_PASEO_RELAY_ENDPOINT",
    "PENNIX_PASEO_RELAY_PUBLIC_ENDPOINT",
    "PENNIX_PASEO_RELAY_TLS",
    "PENNIX_PASEO_RELAY_PUBLIC_TLS",
}


class ConfigError(ValueError):
    """The workflow configuration is absent, unsafe, or invalid."""


class StrictKconfig(kconfiglib.Kconfig):
    def __init__(self) -> None:
        self.diagnostics: list[str] = []
        super().__init__(str(KCONFIG_FILE), warn=True, warn_to_stderr=False)

    def _warn(self, msg, *args, **kwargs):  # noqa: N802 - kconfiglib hook
        detail = " ".join(str(arg) for arg in args)
        self.diagnostics.append(f"{msg} {detail}".strip())


def config_path(explicit: str | None = None) -> Path:
    if explicit:
        path = Path(os.path.expandvars(os.path.expanduser(explicit)))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")).expanduser()
        path = base / "pennix" / "workflow.config"
    if not path.is_absolute():
        raise ConfigError("workflow.config 路径必须是绝对路径")
    return path


def _safe_parent(path: Path) -> None:
    if not path.parent.exists():
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    if path.parent.is_symlink() or not path.parent.is_dir():
        raise ConfigError("配置目录不是安全的普通目录")
    if hasattr(os, "getuid") and path.parent.stat().st_uid != os.getuid():
        raise ConfigError("配置目录不属于当前用户")


def _atomic_write(path: Path, text: str) -> None:
    _safe_parent(path)
    mode = 0o600
    try:
        mode = stat.S_IMODE(path.lstat().st_mode)
    except FileNotFoundError:
        pass
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ConfigError("配置目标不是安全的普通文件")
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode & 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _validate_header(text: str) -> None:
    if not text.startswith(HEADER):
        raise ConfigError("workflow.config 格式主版本缺失或不兼容")


def _new() -> StrictKconfig:
    kconf = StrictKconfig()
    if kconf.diagnostics:
        raise ConfigError("Kconfig 定义无效: " + kconf.diagnostics[0])
    return kconf


def _validate(kconf: StrictKconfig) -> None:
    if kconf.diagnostics:
        raise ConfigError("配置语法无效: " + kconf.diagnostics[0])
    for symbol in kconf.missing_syms:
        raise ConfigError(f"配置包含未知选项: {symbol.name}")
    for symbol in kconf.unique_defined_syms:
        if symbol.user_value is None:
            continue
        if (
            symbol.visibility == 0
            and symbol.name not in PRESERVE_HIDDEN_VALUES
            and symbol.type in {kconfiglib.BOOL, kconfiglib.TRISTATE}
            and symbol.user_value not in (None, "n")
        ):
            raise ConfigError(f"配置选项依赖未满足: {symbol.name}")
    if kconf.syms["PENNIX_CONFIG_FORMAT"].str_value != FORMAT_VERSION:
        raise ConfigError("workflow.config 格式主版本不兼容")


def _read(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as error:
        raise ConfigError(f"配置不存在: {path}") from error
    except (OSError, UnicodeDecodeError) as error:
        raise ConfigError("配置无法读取") from error
    _validate_header(text)
    assignments: set[str] = set()
    for line in text.splitlines():
        match = re.match(r"^(?:CONFIG_)?([A-Za-z_][A-Za-z0-9_]*)=", line)
        if match:
            name = match.group(1)
            if name in assignments:
                raise ConfigError(f"配置包含重复赋值: {name}")
            assignments.add(name)
    return text


def load(explicit: str | None = None) -> tuple[StrictKconfig, Path]:
    path = config_path(explicit)
    text = _read(path)
    kconf = _new()
    kconf.load_config(str(path))
    _validate(kconf)
    return kconf, path


def write(kconf: StrictKconfig, path: Path) -> None:
    _validate(kconf)
    _safe_parent(path)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        os.close(descriptor)
        kconf.write_config(str(temporary))
        body = temporary.read_text(encoding="utf-8")
        assignments = set(re.findall(r"^(?:CONFIG_)?([A-Za-z_][A-Za-z0-9_]*)=", body, re.MULTILINE))
        preserved: list[str] = []
        for name in PRESERVE_HIDDEN_VALUES:
            symbol = kconf.syms[name]
            if symbol.user_value is None or name in assignments:
                continue
            if symbol.type == kconfiglib.STRING:
                value = symbol.user_value.replace("\\", "\\\\").replace('"', '\\"')
                preserved.append(f'CONFIG_{name}="{value}"')
            else:
                preserved.append(f"CONFIG_{name}={symbol.str_value}")
        if preserved:
            body = body.rstrip("\n") + "\n" + "\n".join(preserved) + "\n"
        _atomic_write(path, HEADER + body)
    finally:
        temporary.unlink(missing_ok=True)


def init(explicit: str | None = None, *, force: bool = False) -> Path:
    path = config_path(explicit)
    if path.exists() and not force:
        load(str(path))
        return path
    kconf = _new()
    write(kconf, path)
    return path


def edit(explicit: str | None = None) -> Path:
    path = config_path(explicit)
    if not path.exists():
        init(str(path))
    _read(path)
    kconf = _new()
    kconf.load_config(str(path))
    _validate(kconf)
    old = Path.cwd()
    with tempfile.TemporaryDirectory(prefix="pennix-menuconfig-") as directory:
        temporary = Path(directory) / ".config"
        kconf.write_config(str(temporary))
        os.chdir(directory)
        try:
            kconfiglib.menuconfig(kconf)
        finally:
            os.chdir(old)
        if not temporary.exists():
            raise ConfigError("menuconfig 未生成配置")
        edited = _new()
        edited.load_config(str(temporary))
        _validate(edited)
        write(edited, path)
    return path


def values(kconf: StrictKconfig) -> dict[str, str]:
    return {symbol.name: _effective_value(symbol) for symbol in kconf.unique_defined_syms}


def selected_components(kconf: StrictKconfig) -> set[str]:
    return {component_id for symbol, component_id in COMPONENT_IDS.items() if kconf.syms[symbol].tri_value}


def config_value(kconf: StrictKconfig, name: str) -> str:
    symbol = kconf.syms.get(name)
    if symbol is None:
        raise ConfigError(f"未知配置键: {name}")
    return _effective_value(symbol)


def _effective_value(symbol: kconfiglib.Symbol) -> str:
    if symbol.name in PRESERVE_HIDDEN_VALUES and symbol.user_value is not None:
        if symbol.type == kconfiglib.STRING:
            return str(symbol.user_value)
        return "y" if symbol.user_value == kconfiglib.y else "n"
    return symbol.str_value
