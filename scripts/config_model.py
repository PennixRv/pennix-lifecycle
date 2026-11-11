"""Strict Kconfig configuration, validation, and interactive editing."""
from __future__ import annotations

import os
from pathlib import Path
import re
import stat
import sys
import tempfile

import kconfiglib
import menuconfig
import owned_files as files

ROOT = Path(__file__).resolve().parents[1]
KCONFIG_FILE = ROOT / "config/Kconfig"
FORMAT_VERSION = "5"
HEADER = f"# Pennix workflow.config format: {FORMAT_VERSION}\n"
PATH_KEYS = {"PENNIX_CODEX_HOME", "PENNIX_COPILOT_HOME", "PENNIX_PASEO_HOME", "PENNIX_SECRETS_FILE"}
ASSIGN = re.compile(r"^\s*(?:CONFIG_)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
UNSET = re.compile(r"^\s*#\s*CONFIG_([A-Za-z_][A-Za-z0-9_]*)\s+is not set\s*$")


class ConfigError(ValueError):
    pass


class StrictKconfig(kconfiglib.Kconfig):
    def __init__(self) -> None:
        self.diagnostics = []
        # Kconfig's environment must not change the public CONFIG_ syntax.
        previous = os.environ.pop("CONFIG_", None)
        try:
            super().__init__(str(KCONFIG_FILE), warn=True, warn_to_stderr=False)
        finally:
            if previous is not None:
                os.environ["CONFIG_"] = previous
        self.snapshot: bytes | None = None

    def _warn(self, msg, *args, **kwargs):
        try:
            detail = msg % args if args else str(msg)
        except (TypeError, ValueError):
            detail = " ".join([str(msg), *(str(arg) for arg in args)])
        self.diagnostics.append(detail)


def _path_value(value: str) -> str:
    variables = re.findall(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?", value)
    if any(name != "HOME" for name in variables):
        raise ConfigError("路径仅允许使用 $HOME 环境引用")
    value = os.path.expanduser(value.replace("${HOME}", os.environ.get("HOME", "")))
    value = value.replace("$HOME", os.environ.get("HOME", ""))
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise ConfigError("路径必须为无穿越绝对路径")
    return str(path)


def config_path(explicit: str | None = None) -> Path:
    base = files.xdg_home("XDG_CONFIG_HOME", Path.home() / ".config")
    value = explicit or str(base / "pennix/workflow.config")
    return Path(_path_value(value))


def _new() -> StrictKconfig:
    kconf = StrictKconfig()
    if kconf.diagnostics:
        raise ConfigError("Kconfig 定义无效: " + kconf.diagnostics[0])
    return kconf


def _parse_assignments(text: str, kconf: StrictKconfig) -> dict[str, str]:
    values = {}
    defined = {symbol.name for symbol in kconf.defined_syms}
    for line in text.splitlines():
        match = ASSIGN.match(line) or UNSET.match(line)
        if not match:
            if line.strip() and not line.lstrip().startswith("#"):
                raise ConfigError("配置包含无效行")
            continue
        name = match[1]
        if name in values:
            raise ConfigError(f"配置包含重复赋值: {name}")
        if name not in defined:
            raise ConfigError(f"配置包含未知选项: {name}")
        values[name] = "n" if UNSET.match(line) else match[2]
    for choice in kconf.unique_choices:
        enabled = [symbol.name for symbol in choice.syms if values.get(symbol.name) in {"y", '"y"'}]
        if len(enabled) > 1:
            raise ConfigError("互斥选项只能选择一个: " + ", ".join(enabled))
    return values


def _validate(kconf: StrictKconfig, raw: str) -> None:
    if kconf.diagnostics:
        raise ConfigError("配置语法无效: " + kconf.diagnostics[0])
    assignments = _parse_assignments(raw, kconf)
    for symbol in kconf.unique_defined_syms:
        if symbol.name.startswith("PENNIX_COMPONENT_") and symbol.visibility and symbol.name not in assignments:
            raise ConfigError(f"组件选择缺失，需核对新配置项: {symbol.name}")
    for name, value in assignments.items():
        symbol = kconf.syms[name]
        if symbol.type in {kconfiglib.BOOL, kconfiglib.TRISTATE}:
            if value not in {"y", "m", "n"} or value != symbol.str_value:
                raise ConfigError(f"配置请求未生效: {name}")
        elif symbol.type == kconfiglib.STRING:
            if not re.fullmatch(r'"(?:[^"\\]|\\.)*"', value):
                raise ConfigError(f"字符串配置必须使用双引号: {name}")
            if kconfiglib.unescape(value[1:-1]) != symbol.str_value:
                raise ConfigError(f"配置请求未生效: {name}")
        if name != "PENNIX_CONFIG_FORMAT" and symbol.visibility == 0 and value not in {"n", '""'}:
            raise ConfigError(f"配置选项依赖未满足或不可见: {name}")
    for key in PATH_KEYS:
        if key in assignments:
            symbol = kconf.syms[key]
            value = symbol.str_value
            _path_value(value)
    if kconf.syms["PENNIX_CONFIG_FORMAT"].str_value != FORMAT_VERSION:
        raise ConfigError("workflow.config 格式主版本不兼容")
    if assignments.get("PENNIX_CONFIG_FORMAT") != f'"{FORMAT_VERSION}"':
        raise ConfigError("workflow.config 必须显式声明格式版本")
    for choice in kconf.unique_choices:
        if choice.visibility and not choice.is_optional and not any(
                assignments.get(symbol.name) == "y" for symbol in choice.syms):
            raise ConfigError("配置中的必选互斥组选项缺失或全为 n")


def _read(path: Path) -> bytes:
    raw = files.read(path, limit=4 * 1024 * 1024)
    if raw is None:
        raise ConfigError(f"配置不存在: {path}")
    if not raw.startswith(HEADER.encode()):
        raise ConfigError("workflow.config 格式主版本缺失或不兼容")
    return raw


def _load_file(path: Path) -> StrictKconfig:
    raw = _read(path)
    kconf = _new()
    try:
        # Parse the same bytes that pass validation, not a second read of the source.
        with tempfile.TemporaryDirectory(prefix="pennix-config-snapshot-") as directory:
            snapshot = Path(directory) / ".config"
            snapshot.write_bytes(raw)
            kconf.load_config(str(snapshot))
        _validate(kconf, raw.decode("utf-8"))
    except (OSError, UnicodeError, kconfiglib.KconfigError) as error:
        raise ConfigError("配置文件无法解析") from error
    kconf.snapshot = raw
    return kconf


def load(explicit: str | None = None):
    path = config_path(explicit)
    return _load_file(path), path


def values(kconf) -> dict[str, str]:
    return {symbol.name: symbol.str_value for symbol in kconf.unique_defined_syms}


def _render(kconf) -> bytes:
    for key in PATH_KEYS:
        symbol = kconf.syms[key]
        if symbol.user_value is None and symbol.str_value.startswith(str(Path.home()) + "/"):
            symbol.set_value("${HOME}" + symbol.str_value[len(str(Path.home())):])
    with tempfile.TemporaryDirectory(prefix="pennix-kconfig-") as directory:
        path = Path(directory) / ".config"
        kconf.write_config(str(path))
        return HEADER.encode() + path.read_bytes()


_UNSPECIFIED = object()


def write(kconf, path: Path, before=_UNSPECIFIED) -> None:
    _validate(kconf, _render(kconf).decode())
    output = _render(kconf)
    previous = files.read(path)
    if before is not _UNSPECIFIED and previous != before:
        raise ConfigError("workflow.config 在编辑期间发生变化")
    mode = 0o600
    if previous is not None:
        mode = stat.S_IMODE(path.stat().st_mode)
    files.replace(path, output, previous, mode=mode & 0o600)


def init(explicit: str | None = None, *, force: bool = False, defconfig: str | None = None) -> Path:
    path = config_path(explicit)
    previous = files.read(path)
    if previous is not None and not force:
        load(str(path))
        return path
    if defconfig:
        profile = Path(defconfig).expanduser()
        if not profile.is_absolute():
            profile = Path.cwd() / profile
        if profile.is_symlink():
            raise ConfigError("defconfig 必须是普通文件")
        try:
            profile = profile.resolve(strict=True)
        except OSError as error:
            raise ConfigError(f"defconfig 不存在: {profile}") from error
        if profile.is_symlink() or not profile.is_file():
            raise ConfigError("defconfig 必须是普通文件")
        kconf = _load_file(profile)
    else:
        kconf = _new()
    write(kconf, path, before=previous)
    return path


def edit(explicit: str | None = None) -> Path:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ConfigError("config edit 需要交互式终端；无交互部署请编辑 workflow.config")
    path = config_path(explicit)
    if files.read(path) is None:
        init(str(path))
    kconf, _ = load(str(path))
    before = kconf.snapshot
    original = Path.cwd()
    with tempfile.TemporaryDirectory(prefix="pennix-menuconfig-") as directory:
        config = Path(directory) / ".config"
        kconf.write_config(str(config))
        os.chdir(directory)
        try:
            menuconfig.menuconfig(kconf)
        finally:
            os.chdir(original)
        edited = _new()
        edited.load_config(str(config))
        _validate(edited, config.read_text(encoding="utf-8"))
        write(edited, path, before=before)
    return path


def selected_components(kconf) -> set[str]:
    return {key.removeprefix("PENNIX_COMPONENT_").lower().replace("_", "-")
            for key in values(kconf) if key.startswith("PENNIX_COMPONENT_") and kconf.syms[key].tri_value}


def config_value(kconf, name: str) -> str:
    symbol = kconf.syms.get(name)
    if symbol is None or not symbol.nodes:
        raise ConfigError(f"未知配置键: {name}")
    value = symbol.str_value
    return _path_value(value) if name in PATH_KEYS else value


def choice_value(kconf, prefix: str) -> str:
    choices = [name.removeprefix(prefix).lower().replace("_", "-")
               for name, value in values(kconf).items() if name.startswith(prefix) and value == "y"]
    if len(choices) != 1:
        raise ConfigError(f"互斥配置必须且只能选择一个: {prefix}")
    return choices[0]
