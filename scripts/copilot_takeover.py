"""Only the reviewed Copilot CLI AUR handover; not a generic adoption engine."""

from fnmatch import fnmatchcase
import gzip
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

import apply_journal
import candidates
import component_lock
import owned_files
import _engine as engine


COMPONENT = "copilot-cli"
PACKAGE = "github-copilot-cli-bin"
SOURCE = f"aur:{PACKAGE}"
OLD = {"source": SOURCE, "version": "1.0.114-1", "ref": "7f2559037b3a6aab1e0e30c2bfd9826c5b57f045"}
NEW = {"source": SOURCE, "version": "1.0.116-1", "ref": "e0101262368e566478bba801f6fca115567591fa"}
UPDATE_HOOK = "1090b7b1edba2042298b609a77bbe122982ca936208408fb79d77b33a2f3c27a"
UPDATE_HELPER = "90e00dd01359565be85a8cdd93b424b4c1c89563300d16eaf0e7eedc349bf9fc"
FILES = {"usr/bin/copilot", "usr/share/bash-completion/completions/copilot", "usr/share/zsh/site-functions/_copilot",
         "usr/share/fish/vendor_completions.d/copilot.fish", f"usr/share/doc/{PACKAGE}/README.md",
         f"usr/share/doc/{PACKAGE}/CHANGELOG.md", f"usr/share/licenses/{PACKAGE}/LICENSE"}
EMPTY_DIRS = {"usr/src/", "usr/src/debug/", f"usr/src/debug/{PACKAGE}/"}


def fail(message):
    raise engine.BootstrapError(message)


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def run(command, *, timeout=60, **kwargs):
    try:
        return subprocess.run(command, check=True, capture_output=True, timeout=timeout, **kwargs).stdout
    except (OSError, subprocess.SubprocessError) as error:
        raise engine.BootstrapError(f"Copilot 接管的原生前置失败：{command[0]}") from error


def payload(archive):
    paths = run(["bsdtar", "-tf", str(archive)], text=True).splitlines()
    if any(path not in {".BUILDINFO", ".PKGINFO", ".MTREE", *FILES, *EMPTY_DIRS} and
           not (path.endswith("/") and any(item.startswith(path) for item in FILES))
           for path in paths):
        fail("Copilot AUR 载荷包含非预期路径或安装脚本")
    entries = {}
    raw = run(["bsdtar", "-xOf", str(archive), ".MTREE"])
    for line in gzip.decompress(raw).decode().splitlines():
        fields = line.split()
        if fields and fields[0].startswith("./usr/") and "type=dir" not in fields:
            sha = next((part.removeprefix("sha256digest=") for part in fields if part.startswith("sha256digest=")), None)
            if not sha or not re.fullmatch(r"[0-9a-f]{64}", sha):
                fail("Copilot 载荷包含不可验证的文件类型")
            entries["/" + fields[0][2:]] = sha
    if set(entries) != {"/" + path for path in FILES} or not FILES <= set(paths):
        fail("Copilot AUR 载荷文件清单不完整")
    return entries, paths


def hooks(paths):
    dirs = [Path("/usr/share/libalpm/hooks"), *map(Path, run(["pacman-conf", "HookDir"], text=True).splitlines())]
    selected = {file.name: file for directory in dirs for file in sorted(directory.glob("*.hook"))}
    matched = {}
    for name, hook in selected.items():
        if hook.is_symlink() and hook.resolve() == Path("/dev/null"):
            continue
        if hook.is_symlink() or not hook.is_file():
            fail("存在无法核验的包管理 Hook")
        text = hook.read_text()
        trigger = text.split("[Action]", 1)[0]
        selectors = [line.split("=", 1)[1].strip() for line in trigger.splitlines()
                     if line.split("=", 1)[0].strip() == "Target" and "=" in line]
        if not selectors or any(not re.fullmatch(r"[!-~]+", selector) or "\\" in selector
                                for selector in selectors):
            fail("包管理 Hook 缺少可核验的目标或使用了未核验的转义")
        # Include both scopes; a false match stops rather than allowing a restart.
        if not any(fnmatchcase(path.rstrip("/"), selector.rstrip("/"))
                   for selector in selectors if not selector.startswith("!")
                   for path in (*paths, PACKAGE)):
            continue
        sound = (name == "35-systemd-update.hook" and digest(hook) == UPDATE_HOOK
                 and digest("/usr/share/libalpm/scripts/systemd-hook") == UPDATE_HELPER)
        if not sound:
            fail(f"Copilot 包事务可能命中未经审阅的 Hook：{name}")
        matched[name] = "touch /usr; no daemon reload or restart"
    return matched


def staged(component, parent, pin):
    parent.mkdir()
    candidate = candidates.resolve_aur(component, parent, pin)
    home = parent / "home"
    home.mkdir(mode=0o700)
    environment = {"HOME": str(home), "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
    environment.update({key: os.environ[key] for key in ("https_proxy", "http_proxy", "no_proxy")
                        if key in os.environ})
    archive = engine.build_aur_package(COMPONENT, PACKAGE, pin["version"], Path(candidate["path"]),
                                       environment=environment)
    entries, paths = payload(archive)
    check_transaction(archive, pin)
    return archive, entries, paths


def check_transaction(archive, pin):
    info = run(["bsdtar", "-xOf", str(archive), ".PKGINFO"], text=True)
    for key, expected in (("pkgname", PACKAGE), ("pkgver", pin["version"]), ("arch", "x86_64"),
                          ("replaces", None), ("backup", None)):
        actual = [line.split(" = ", 1)[1] for line in info.splitlines() if line.startswith(f"{key} = ")]
        if actual != ([] if expected is None else [expected]):
            fail(f"Copilot AUR 候选元数据超出已审阅范围：{key}")
    conflicts = {line.split(" = ", 1)[1] for line in info.splitlines() if line.startswith("conflict = ")}
    if not conflicts <= {"github-copilot-cli", "github-copilot"}:
        fail("Copilot AUR 候选的包冲突关系超出已审阅范围")
    installed_names = set(run(["pacman", "-Qq"], text=True).splitlines())
    if conflicts & installed_names:
        fail("Copilot 接管会影响其他已装包")
    transaction = run(["pacman", "-Up", "--print-format", "%n %v", "--", str(archive)], text=True)
    if transaction.strip() != f"{PACKAGE} {pin['version']}":
        fail("Copilot 接管要求原生事务仅处理此包；额外依赖必须另行审阅整批升级")


def installed(component, pin, entries):
    identity = engine.package_identity(component)
    if (identity != {"command": "/usr/bin/copilot", "manager": "pacman", "name": PACKAGE, "version": pin["version"]}
            or not all(Path(path).is_file() and Path(path).resolve() == Path(path) and digest(path) == sha
                       for path, sha in entries.items())):
        return False
    # The CLI may discover another release through a previously used HOME.
    with tempfile.TemporaryDirectory(prefix="pennix-copilot-version-") as home:
        raw = run([identity["command"], "--version"], text=True, cwd=home,
                  env={"HOME": home, "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"})
    return engine.normalize_version(raw) == pin["version"].rsplit("-", 1)[0]


def reserve_record(args, component, write_lock):
    receipt = engine._package_receipt(COMPONENT, component)
    if receipt is None:
        engine._write_package_receipt(COMPONENT, component, NEW["version"])
    elif receipt["version"] != NEW["version"] or receipt.get("target") != owned_files.target_identity():
        fail("Copilot 回执与受审阅的接管不符")
    args.component_lock = component_lock.set_entry(args.component_lock, COMPONENT, NEW, args.component_lock_sources)
    write_lock(args)


def resume(args, component, active, emit, verify, write_lock):
    step_id = f"{COMPONENT}:takeover"
    if active["command"] != "update" or active["component"] != COMPONENT or len(active["steps"]) != 1:
        fail("存在另一项生命周期事务，不能接管 Copilot")
    step = apply_journal.step(active, step_id)
    plan = step["target"]
    confirmation = hashlib.sha256(owned_files.json_bytes(plan)).hexdigest()
    if args.confirm != confirmation:
        fail("Copilot 接管事务仍保留；核对 recover 后只可用原确认摘要复核确切后置条件")
    if (plan.get("candidate") != NEW or plan.get("previous") != OLD
            or plan.get("identity") != owned_files.target_identity() or step["status"] == "pending"
            or plan.get("config") != apply_journal.digest(args.config_path)):
        fail("Copilot 接管记录与当前目标不符，保留现场")
    before_lock = component_lock.remove_entry(args.component_lock, COMPONENT)
    lock = args.component_lock["components"].get(COMPONENT)
    if (lock not in (None, NEW)
            or hashlib.sha256(owned_files.json_bytes(before_lock)).hexdigest() != plan.get("other_locks")
            or (lock is None and apply_journal.digest(args.component_lock_path) != plan["lock"])):
        fail("Copilot 接管之外的锁已变化，保留现场")
    db = Path(run(["pacman-conf", "DBPath"], text=True).strip())
    if not db.is_absolute() or (db / "db.lck").exists() or not installed(component, NEW, plan["target_files"]):
        fail("Copilot 接管尚未证明完整后置条件；不会重放包写入")
    verify(args)
    reserve_record(args, component, write_lock)
    apply_journal.verified(apply_journal.read(), step_id, {"package": "verified-without-replay"})
    apply_journal.clear()
    emit({"operation": "copilot-takeover", "state": "recovered", "candidate": NEW}, args.json)
    return 0


def command(args, component, emit, verify, start, mutate, write_lock):
    if component.get("lock_source") != SOURCE or component.get("package") != {"source": "aur", "name": PACKAGE}:
        fail("Copilot 接管来源与审核合同不符")
    active = apply_journal.read()
    if active is not None:
        return resume(args, component, active, emit, verify, write_lock)
    lock = args.component_lock["components"].get(COMPONENT)
    receipt = engine._package_receipt(COMPONENT, component)
    if lock is not None or receipt is not None:
        fail("Copilot 接管仅适用于无锁、无回执的外部安装")
    if engine.package_identity(component)["version"] != OLD["version"]:
        fail("外部 Copilot 版本不是已审阅的退回版本，不执行接管")
    with tempfile.TemporaryDirectory(prefix="pennix-copilot-takeover-") as temporary:
        root = Path(temporary)
        previous_archive, previous, prev_paths = staged(component, root / "old", OLD)
        archive, target, new_paths = staged(component, root / "new", NEW)
        if not installed(component, OLD, previous):
            fail("已安装 Copilot 不匹配经核验的退回载荷，不签发纳管回执")
        effects = hooks(set(prev_paths + new_paths))
        plan = {"component": COMPONENT, "identity": owned_files.target_identity(),
                "config": apply_journal.digest(args.config_path), "lock": apply_journal.digest(args.component_lock_path),
                "other_locks": hashlib.sha256(owned_files.json_bytes(args.component_lock)).hexdigest(),
                "previous": OLD, "candidate": NEW, "previous_files": previous, "target_files": target, "hooks": effects}
        confirmation = hashlib.sha256(owned_files.json_bytes(plan)).hexdigest()
        if not args.confirm:
            emit({"operation": "copilot-takeover", "candidate": NEW, "rollback": OLD, "hooks": effects,
                  "confirm": confirmation}, args.json)
            return 0
        if args.confirm != confirmation:
            fail("Copilot 接管确认与当前候选或目标不符，重新预览")
        verify(args)
        # Retain only the two exact package artefacts for a failed native transaction.
        store = owned_files.state_root() / "copilot-takeover" / confirmation
        owned_files.safe_path(store)
        native_install = ["/usr/bin/pacman", "-U", "--noconfirm", "--", str(store / "candidate.pkg.tar.zst")]
        run(["sudo", "-n", "-l", "--", *native_install])
        store.mkdir(mode=0o700, parents=True, exist_ok=False)
        for side, package in (("previous.pkg.tar.zst", previous_archive), ("candidate.pkg.tar.zst", archive)):
            with (store / side).open("xb") as output, package.open("rb") as original:
                shutil.copyfileobj(original, output)
            if digest(store / side) != digest(package):
                fail("Copilot 接管包复制后无法核验")
        step = f"{COMPONENT}:takeover"
        active = start(args, "update", COMPONENT,
                       [{"id": step, "component": COMPONENT, "action": "takeover", "target": plan}])

        def replace():
            verify(args)
            if (not installed(component, OLD, previous) or hooks(set(prev_paths + new_paths)) != effects
                    or digest(store / "candidate.pkg.tar.zst") != digest(archive)):
                fail("Copilot 或包管理 Hook 自确认后变化，未调用包管理器")
            check_transaction(store / "candidate.pkg.tar.zst", NEW)
            run(["sudo", "-n", *native_install], timeout=180)
            if not installed(component, NEW, target):
                fail("未核验实际 Copilot 替换；保留事务和退回包")
            reserve_record(args, component, write_lock)
            return "verified"

        mutate(active, step, replace)
        apply_journal.clear()
        emit({"operation": "copilot-takeover", "state": "verified", "candidate": NEW}, args.json)
        return 0
