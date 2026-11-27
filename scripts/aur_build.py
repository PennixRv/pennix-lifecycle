"""Freeze and build registered AUR inputs without installing packages."""
import hashlib
import os
from pathlib import Path
import platform
import re
import shutil
import tempfile
from urllib.parse import urlsplit

import arch_policy
import arch_recovery
import candidates
import owned_files
import update_inventory


MAKEPKG_CONFIG = Path("/etc/makepkg.conf")


def _text(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 128 * 1024:
        raise candidates.CandidateError("AUR 配方或元数据不是受控的小文件")
    return path.read_text()


def _makepkg_config():
    arch_policy.trusted_directory(MAKEPKG_CONFIG.parent)
    if not arch_recovery._trusted_file(MAKEPKG_CONFIG):
        raise candidates.CandidateError("makepkg 配置不是系统所有的常规文件")
    return _text(MAKEPKG_CONFIG).encode()


def inspect_sources(text, architecture, checkout):
    fields = {}
    for line in text.splitlines():
        key, found, value = line.strip().partition(" = ")
        if found:
            fields.setdefault(key, []).append(value)
    sources = {}
    for suffix in ("", "_" + architecture):
        urls = fields.get("source" + suffix, [])
        sums = fields.get("sha256sums" + suffix, [])
        if len(urls) != len(sums) or len(urls) + len(sources) > 64:
            raise candidates.CandidateError("AUR 来源与 SHA-256 清单不完整")
        for value, digest in zip(urls, sums):
            alias, separator, url = value.partition("::")
            url = url if separator else value
            uri = urlsplit(url)
            name = alias if separator else uri.path.rsplit("/", 1)[-1]
            if (not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._+-]{0,219}", name)
                    or name in {"PKGBUILD", "src", "pkg"} or name in sources
                    or not re.fullmatch(r"[0-9a-f]{64}", digest)):
                raise candidates.CandidateError("AUR 来源没有唯一安全文件名或实值 SHA-256")
            if uri.scheme == "https" and uri.hostname and not (uri.username or uri.password or uri.fragment):
                if os.path.lexists(checkout / name):
                    raise candidates.CandidateError("AUR 下载目标与已检出的材料冲突")
                sources[name] = {"url": url, "sha256": digest}
            elif value == name:
                path = checkout / name
                if path.is_symlink() or not path.is_file() or candidates._digest(path) != digest:
                    raise candidates.CandidateError("AUR 本地来源与冻结内容不一致")
                sources[name] = {"sha256": digest}
            else:
                raise candidates.CandidateError("当前构建仅支持 HTTPS 或冻结提交内的来源文件")
    return sources


def inspect(component, workspace):
    if os.geteuid() == 0:
        raise candidates.CandidateError("AUR 构建只能由普通用户准备")
    selected = candidates.resolve_aur(component, workspace)
    checkout = Path(selected["path"])
    recipe, metadata = (_text(checkout / name) for name in ("PKGBUILD", ".SRCINFO"))
    architecture = platform.machine()
    plan = {"package": component["package"]["name"], "source": candidates.lock_entry(selected),
            "architecture": architecture, "recipe": recipe, "metadata": metadata,
            "makepkg_config_sha256": hashlib.sha256(_makepkg_config()).hexdigest(),
            "sources": inspect_sources(metadata, architecture, checkout),
            "installed_sha256": hashlib.sha256(owned_files.json_bytes(
                update_inventory.installed_arch(pacman="/usr/bin/pacman"))).hexdigest()}
    return checkout, plan


def _inputs_current(plan, db):
    return (arch_recovery._db_lock() == db and arch_recovery._lock_absent(db)
            and hashlib.sha256(owned_files.json_bytes(
                update_inventory.installed_arch(pacman="/usr/bin/pacman"))).hexdigest() == plan["installed_sha256"])


def build(checkout, plan):
    db = arch_recovery._db_lock()
    arch_policy.trusted_directory(db.parent / "local")
    if not _inputs_current(plan, db):
        raise candidates.CandidateError("AUR 构建前包库或依赖环境已变化")
    for name, source in plan["sources"].items():
        if "url" in source:
            owned_files.download(source["url"], checkout / name)
        if candidates._digest(checkout / name) != source["sha256"]:
            raise candidates.CandidateError("AUR 来源下载内容与审阅摘要不一致")

    config = checkout.parent / "pacman.conf"
    config.write_text("[options]\nArchitecture = auto\nDBPath = /native-db\n")
    makepkg_config = checkout.parent / "makepkg.conf"
    content = _makepkg_config()
    if hashlib.sha256(content).hexdigest() != plan["makepkg_config_sha256"]:
        raise candidates.CandidateError("makepkg 配置在构建前已变化")
    makepkg_config.write_bytes(content)
    sandbox = ["/usr/bin/bwrap", "--unshare-all", "--unshare-user", "--disable-userns", "--cap-drop", "ALL", "--new-session",
               "--die-with-parent", "--ro-bind", "/usr", "/usr", "--symlink", "usr/lib", "/lib",
               "--symlink", "usr/lib", "/lib64", "--symlink", "usr/bin", "/bin",
               "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--ro-bind", makepkg_config, "/etc/makepkg.conf",
               "--ro-bind", config, "/etc/pacman.conf", "--ro-bind", db.parent / "local", "/native-db/local"]
    command = ["--chdir", "/work", "--clearenv", "--setenv", "HOME", "/tmp",
               "--setenv", "PATH", "/usr/bin", "--setenv", "LC_ALL", "C.UTF-8"]
    makepkg = ["--", "/usr/bin/makepkg", "--config", "/etc/makepkg.conf"]
    scratch = [part for variable in ("BUILDDIR", "PKGDEST", "SRCDEST", "SRCPKGDEST", "LOGDEST")
               for part in ("--setenv", variable, "/tmp")]
    metadata = update_inventory.run([*sandbox, "--ro-bind", checkout, "/work", *command, *scratch,
                                     *makepkg, "--printsrcinfo"],
                                    noninteractive=True)
    if metadata.returncode or metadata.stdout.split() != plan["metadata"].split():
        raise candidates.CandidateError("AUR 沙箱或实际配方元数据与审阅内容不一致")
    result = update_inventory.run([*sandbox, "--bind", checkout, "/work", *command, *makepkg,
                                   "--cleanbuild", "--holdver", "--noconfirm"],
                                  timeout=1800, noninteractive=True)
    if result.returncode or not _inputs_current(plan, db):
        raise candidates.CandidateError("AUR 隔离构建失败或包基线已变化；未安装依赖或软件")
    name, version = plan["package"], plan["source"]["version"]
    observed = []
    for path in checkout.glob("*.pkg.tar.*"):
        if path.is_symlink() or not path.is_file() or path.name.endswith(".sig"):
            continue
        identity = update_inventory.run(["/usr/bin/pacman", "-Qp", "--", str(path)], noninteractive=True)
        if identity.stdout.split() == [name, version] and identity.returncode == 0 and not identity.stderr:
            observed.append(path)
    if len(observed) != 1:
        raise candidates.CandidateError("AUR 隔离构建未产生唯一、精确身份的包")
    (artifact,) = observed
    destination = Path(tempfile.mkdtemp(prefix="pennix-aur-artifact-")) / artifact.name
    with artifact.open("rb") as source, destination.open("xb") as sink:
        shutil.copyfileobj(source, sink)
    return {"path": str(destination), "version": version, "ref": plan["source"]["ref"],
            "filename": destination.name, "sha256": candidates._digest(destination)}
