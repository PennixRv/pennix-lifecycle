"""The development base owner; package databases keep ownership of software."""
from __future__ import annotations

import base64
import grp
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import shlex
import subprocess
import urllib.parse
import urllib.request

import host
import owned_files as files
import path_env
import apply_journal

ROOT = Path(__file__).resolve().parents[1]
DOCUMENTS = {"markitdown", "pandoc", "libreoffice", "poppler", "tesseract",
             "tesseract-eng", "tesseract-chi-sim", "noto-fonts-cjk"}
SHARED = {"pnpm", "uv", "tmux", "tmux-config"}
SCOPE = {"base-environment", *SHARED, *DOCUMENTS}


class BaseError(RuntimeError):
    pass


def _run(command, **kwargs):
    result = subprocess.run(command, capture_output=True, text=True, check=False,
                            env={**os.environ, "LC_ALL": "C"}, **kwargs)
    if result.returncode:
        raise BaseError(f"原生动作失败: {shlex.join(command[:4])}: {result.stderr[-1200:]}")
    return result.stdout.strip()


def receipt_path():
    return files.state_root() / "base-environment.json"


def receipt():
    value = files.read_json(receipt_path())
    if value is not None and (set(value) != {"schema", "target", "files", "native", "version"}
                              or value["schema"] != 1 or value["target"] != files.target_identity()
                              or not isinstance(value["files"], dict) or not isinstance(value["native"], dict)):
        raise BaseError("基础环境 receipt 无效或属于其他目标")
    if value is not None:
        home = Path.home()
        config = files.xdg_home("XDG_CONFIG_HOME", home / ".config")
        allowed = {home / x for x in [".p10k.zsh", ".zshrc", ".zshenv", ".bashrc", ".bash_profile", ".gitconfig"]}
        allowed |= {config / x for x in ["environment.d/60-pennix-base.conf", "nvim/init.lua",
            "pennix/base/env.sh", "pennix/base/zshrc.zsh", "pennix/base/nvim.lua", "pennix/base/git.config", "pennix/base/git-identity.config"]}
        if any(Path(path) not in allowed or not isinstance(entry, dict)
               or set(entry) - {"before", "mode", "applied", "previous_applied", "replaced"}
               or not {"before", "mode", "applied"} <= set(entry)
               or entry["mode"] not in range(0o600, 0o701)
               or entry["mode"] & 0o022
               for path, entry in value["files"].items()):
            raise BaseError("基础环境 receipt 包含非法文件或权限")
    return value


def _save(value):
    previous = files.read(receipt_path(), private=True)
    files.replace(receipt_path(), files.json_bytes(value), previous)


def _config(kconf, key):
    return kconf.syms[key].str_value


def adoption(kconf):
    try:
        value = json.loads(_config(kconf, "PENNIX_BASE_ADOPT_FILES") or "{}", object_pairs_hook=files.unique_pairs)
    except ValueError as error:
        raise BaseError("基础文件接管必须是目标绝对路径到已审阅 SHA-256 的 JSON 映射") from error
    if (not isinstance(value, dict) or any(not isinstance(k, str) or not Path(k).is_absolute()
            or not Path(k).is_relative_to(Path.home()) or not isinstance(v, str)
            or not re.fullmatch(r"[0-9a-f]{64}", v) for k, v in value.items())):
        raise BaseError("基础文件接管映射的路径或摘要无效")
    return value


def _template(name):
    return (ROOT / "templates" / name).read_bytes()


def _hook(path, body, *, replace_external=False, before=None):
    start, end = "# pennix-lifecycle:base:begin", "# pennix-lifecycle:base:end"
    text = "" if replace_external else (before or b"").decode()
    count = text.count(start), text.count(end)
    if count not in {(0, 0), (1, 1)} or count == (1, 1) and text.index(end) < text.index(start):
        raise BaseError(f"基础加载标记不完整: {path}")
    block = f"{start}\n{body.rstrip()}\n{end}\n"
    if count == (1, 1):
        a, b = text.index(start), text.index(end) + len(end)
        return (text[:a] + block.rstrip() + text[b:]).encode()
    # Bash's interactive guard must come after the shared environment.
    return (block + text).encode()


def targets(kconf):
    home = Path.home()
    config = files.xdg_home("XDG_CONFIG_HOME", home / ".config")
    base = config / "pennix/base"
    result = {**path_env.base_targets(), home / ".p10k.zsh": _template("base-p10k.zsh.install"),
              base / "zshrc.zsh": _template("base-zshrc.zsh.install"),
              base / "nvim.lua": _template("base-nvim.lua.install"),
              base / "git.config": _template("base-git.config.install")}
    name, email = _config(kconf, "PENNIX_BASE_GIT_NAME"), _config(kconf, "PENNIX_BASE_GIT_EMAIL")
    if not name or not email or any(c in name + email for c in "\n\r\0"):
        raise BaseError("基础环境需要目标机 Git 姓名与邮箱白名单值")
    result[base / "git-identity.config"] = (f"[user]\n\tname = {json.dumps(name, ensure_ascii=False)}\n"
                                          f"\temail = {json.dumps(email, ensure_ascii=False)}\n").encode()
    adopt = adoption(kconf)
    sources = {home / ".zshenv": '. "${XDG_CONFIG_HOME:-$HOME/.config}/pennix/base/env.sh"',
               home / ".bashrc": '. "${XDG_CONFIG_HOME:-$HOME/.config}/pennix/base/env.sh"',
               home / ".bash_profile": '. "${XDG_CONFIG_HOME:-$HOME/.config}/pennix/base/env.sh"',
               home / ".zshrc": 'source "${XDG_CONFIG_HOME:-$HOME/.config}/pennix/base/zshrc.zsh"',
               config / "nvim/init.lua": f'dofile({json.dumps(str(base / "nvim.lua"))})',
               home / ".gitconfig": f'[include]\n\tpath = {json.dumps(str(base / "git.config"))}\n'
                                      f'\tpath = {json.dumps(str(base / "git-identity.config"))}'}
    record = receipt()
    for path, body in sources.items():
        before = files.read(path)
        entry = (record or {}).get("files", {}).get(str(path))
        if before is None and entry and entry["before"] is not None:
            before = base64.b64decode(entry["before"], validate=True)
        # Explicit adoption is bound to the reviewed bytes, never to a blanket yes.
        replace_external = bool(entry and entry.get("replaced")) or (path.name in {".zshrc", ".zshenv"} and str(path) in adopt and entry is None)
        result[path] = _hook(path, body, replace_external=replace_external, before=before)
    return result


def package_plan():
    spec = json.loads((ROOT / "references/base-packages.json").read_text())
    installed = dict(line.split(maxsplit=1) for line in _run(["pacman", "-Q"]).splitlines())
    official = [name for name in spec["official"] if name not in installed]
    aur = [name for name in spec["aur"] if name not in installed]
    if "nodejs" in installed and "nodejs-lts-krypton" not in official:
        official.append("nodejs-lts-krypton")
    if "rustup" in installed:
        raise BaseError("已存在 rustup；不能未经决策接管另一套 Rust owner")
    versions = {}
    if official:
        raw = _run(["pacman", "-Si", *official])
        current = None
        for line in raw.splitlines():
            if line.startswith("Name "):
                current = line.split(":", 1)[1].strip()
            if line.startswith("Version ") and current:
                versions[current] = line.split(":", 1)[1].strip()
        if set(versions) != set(official):
            raise BaseError("基础包候选不完整，未开始安装")
    if aur:
        url = "https://aur.archlinux.org/rpc/v5/info?" + urllib.parse.urlencode([("arg[]", x) for x in aur])
        with urllib.request.urlopen(url, timeout=30) as response:
            candidates = json.load(response)["results"]
        for item in candidates:
            if not item.get("Maintainer") or item.get("OutOfDate"):
                raise BaseError(f"AUR 候选无维护者或已标记过期: {item['Name']}")
            versions[item["Name"]] = item["Version"]
        if not set(aur) <= set(versions):
            raise BaseError("AUR 候选缺失")
    return {"official": official, "aur": aur, "versions": versions,
            "node_replacement": "nodejs" in installed and "nodejs-lts-krypton" in official}


def _digest(content):
    return hashlib.sha256(content).hexdigest() if content is not None else None


def inspect(kconf):
    try:
        record, adopt, desired = receipt(), adoption(kconf), targets(kconf)
        changes, blocked = [], []
        for path, output in desired.items():
            before = files.read(path)
            old = (record or {}).get("files", {}).get(str(path))
            actual = _digest(before)
            if old and actual not in {old["applied"], old.get("previous_applied"), None}:
                blocked.append(str(path))
            elif not old and before is not None and before != output:
                # Hooks preserve external text; whole configuration adoption needs a digest.
                hook = b"# pennix-lifecycle:base:begin" in output
                shell_conflict = path.name == ".zshrc" and any(x in before for x in [b"oh-my-zsh", b"powerlevel10k", b"ZSH_THEME"])
                if (not hook or shell_conflict) and adopt.get(str(path)) != actual:
                    blocked.append(str(path))
            if before != output or not old:
                changes.append({"path": str(path), "before_sha256": actual, "after_sha256": _digest(output),
                                "action": "adopt" if before is not None and not old else "write",
                                "private": path.name == "git-identity.config"})
        if (files.read(files.xdg_home("XDG_CONFIG_HOME", Path.home()/'.config')/'nvim/init.vim') is not None):
            blocked.append("nvim/init.vim 与 init.lua 入口冲突")
        packages = package_plan()
        native = native_state()
        current = not changes and not packages["versions"] and native_ready(native)
        return {"state": "blocked" if blocked else "current" if current else "missing",
                "ownership": "lifecycle" if record else "external", "files": changes,
                "conflicts": blocked, "packages": packages,
                "native": native, "detail": "基础模板、软件和原生设置" if not blocked else "用户修改或外部配置需审阅摘要接管"}
    except (OSError, ValueError, files.OwnershipError, BaseError) as error:
        return {"state": "blocked", "detail": str(error)}


def native_state():
    user = pwd.getpwuid(os.getuid())
    groups = _run(["id", "-nG", user.pw_name]).split()
    locale = _run(["locale", "-a"]).lower().splitlines()
    prefix = _run(["npm", "config", "get", "prefix"]) if host.package_managers().get("npm") else None
    return {"user": user.pw_name, "shell": user.pw_shell, "docker_group": "docker" in groups,
            "docker_enabled": subprocess.run(["systemctl", "is-enabled", "docker.service"], capture_output=True).returncode == 0,
            "docker_active": subprocess.run(["systemctl", "is-active", "docker.service"], capture_output=True).returncode == 0,
            "locales": [x for x in ["zh_cn.utf8", "en_us.utf8"] if x in locale], "npm_prefix": prefix,
            "node24": subprocess.run(["node", "-e", "process.exit(process.versions.node.startsWith('24.')?0:1)"], capture_output=True).returncode == 0 if host.package_managers().get("npm") else False}


def native_ready(state):
    return (state["shell"] == "/usr/bin/zsh" and state["docker_group"] and state["docker_enabled"]
            and state["docker_active"] and len(state["locales"]) == 2 and state["node24"]
            and state["npm_prefix"] == str(Path.home()/'.local'))


def _install_packages(plan, catalog):
    if plan["official"]:
        command = ["sudo", "pacman", "-S", "--needed", "--noconfirm"]
        # The approved Node 26 -> 24 replacement is the only planned conflict.
        if plan["node_replacement"]:
            command += ["--ask", "4"]
        _run(command + [f"{name}={plan['versions'][name]}" for name in plan["official"]], timeout=1800)
    if plan["aur"]:
        host.ensure_aur_helper(catalog["aur_helper"])
        _run(["yay", "-S", "--needed", "--noconfirm", *plan["aur"]], timeout=1800)
        for name in plan["aur"]:
            observed = _run(["pacman", "-Q", name]).split()[1]
            # VCS pkgver is computed by makepkg; RPC reports the recipe's last submitted version.
            if name != "oh-my-zsh-git" and observed != plan["versions"][name]:
                raise BaseError("AUR 候选在计划后发生变化；保留现场")


def _native_apply():
    state = native_state()
    if len(state["locales"]) != 2:
        # Modify only the two explicitly selected locale.gen entries.
        script = ("from pathlib import Path; p=Path('/etc/locale.gen'); s=p.read_text(); "
                  "s=s.replace('#zh_CN.UTF-8 UTF-8','zh_CN.UTF-8 UTF-8').replace('#en_US.UTF-8 UTF-8','en_US.UTF-8 UTF-8'); "
                  "assert 'zh_CN.UTF-8 UTF-8' in s and 'en_US.UTF-8 UTF-8' in s; p.write_text(s)")
        _run(["sudo", "/usr/bin/python3", "-c", script])
        _run(["sudo", "locale-gen"], timeout=180)
    _run(["npm", "config", "set", "prefix", str(Path.home()/'.local'), "--location=user"])
    if not state["docker_group"]:
        _run(["sudo", "usermod", "-aG", "docker", state["user"]])
    _run(["sudo", "systemctl", "enable", "--now", "docker.service"], timeout=120)
    if state["shell"] != "/usr/bin/zsh":
        _run(["zsh", "-n", str(Path.home()/'.zshrc')])
        _run(["sudo", "chsh", "-s", "/usr/bin/zsh", state["user"]])


def apply(kconf, catalog):
    state = inspect(kconf)
    if state["state"] == "blocked":
        raise BaseError(state.get("detail", "基础预检失败") + ": " + ", ".join(state.get("conflicts", [])))
    if state["packages"]["versions"]:
        journal = apply_journal.read()
        if not journal or not any(step['id']=='system-upgrade' and step['status']=='verified' for step in journal['steps']):
            raise BaseError("缺失的软件须经 apply --scope base 的同次完整系统升级，repair 不绕过此门禁")
    record = receipt()
    if record is None:
        record = {"schema": 1, "target": files.target_identity(), "files": {},
                  "native": native_state(), "version": (ROOT/'VERSION').read_text().strip()}
        _save(record)
    _install_packages(state["packages"], catalog)
    for path, output in targets(kconf).items():
        before = files.read(path)
        entry = record["files"].get(str(path))
        if entry is None:
            entry = {"before": base64.b64encode(before).decode() if before is not None else None,
                     "mode": path.stat().st_mode & 0o777 if before is not None else 0o600,
                     "applied": _digest(before),
                     "replaced": path.name in {".zshrc", ".zshenv"} and str(path) in adoption(kconf)}
            record["files"][str(path)] = entry
        if _digest(before) not in {entry["applied"], entry.get("previous_applied"), None}:
            raise BaseError(f"配置在应用期间变化: {path}")
        entry["previous_applied"], entry["applied"] = _digest(before), _digest(output)
        _save(record)
        files.replace(path, output, before, mode=entry["mode"])
        entry.pop("previous_applied", None)
        _save(record)
    _native_apply()
    record["version"] = (ROOT/'VERSION').read_text().strip()
    _save(record)
    if inspect(kconf)["state"] != "current":
        raise BaseError("基础环境后置条件未通过；保留 journal/receipt")
    return "changed"


def uninstall():
    record = receipt()
    if record is None:
        return "no-op"
    for value, entry in record["files"].items():
        if _digest(files.read(Path(value))) != entry["applied"]:
            raise BaseError(f"配置已由用户修改，未撤回: {value}")
    old = record["native"]
    state = native_state()
    if old["npm_prefix"] and state["npm_prefix"] == str(Path.home()/'.local') and old["npm_prefix"] != state["npm_prefix"]:
        _run(["npm", "config", "set", "prefix", old["npm_prefix"], "--location=user"])
    elif old['npm_prefix'] is None and state['npm_prefix'] == str(Path.home()/'.local'):
        _run(['npm', 'config', 'delete', 'prefix', '--location=user'])
    if state["shell"] == "/usr/bin/zsh" and old["shell"] != state["shell"]:
        _run(["sudo", "chsh", "-s", old["shell"], state["user"]])
    # Container data is shared; only reverse Docker settings when the native owner proves no workload.
    containers = subprocess.run(["sudo", "docker", "ps", "-aq"], capture_output=True, text=True)
    images = subprocess.run(["sudo", "docker", "images", "-q"], capture_output=True, text=True)
    if containers.returncode == images.returncode == 0 and not containers.stdout.strip() and not images.stdout.strip():
        if not old["docker_group"] and state["docker_group"]:
            _run(["sudo", "gpasswd", "-d", state["user"], "docker"])
        if not old["docker_active"]:
            _run(["sudo", "systemctl", "stop", "docker.service"])
        if not old["docker_enabled"]:
            _run(["sudo", "systemctl", "disable", "docker.service"])
    for value, entry in reversed(list(record["files"].items())):
        path = Path(value)
        original = base64.b64decode(entry["before"], validate=True) if entry["before"] is not None else None
        files.replace(path, original, files.read(path), mode=entry["mode"])
    files.replace(receipt_path(), None, files.read(receipt_path(), private=True))
    return "changed; 软件、locale、文档与共享依赖保留；有容器负载时保留 Docker 服务和用户组"
