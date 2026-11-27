"""Transport and read-only recovery of an exact native Arch package step."""
import hashlib
import os
from pathlib import Path
import re
import stat
import subprocess

import arch_policy
import candidates
import owned_files
import update_inventory


EXECUTOR = Path("/usr/lib/pennix-lifecycle/arch-executor")
COMMITTER = EXECUTOR.with_name("arch-commit")


class ArchCommitError(ValueError):
    def __init__(self, message, *, effect="unknown"):
        super().__init__(message)
        self.effect = effect


def _trusted_file(path):
    info = path.lstat()
    return (stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_nlink == 1
            and not info.st_mode & 0o6022)


def _trusted_observer():
    return (arch_policy.trusted_directory(EXECUTOR.parent) and _trusted_file(EXECUTOR)
            and bool(EXECUTOR.lstat().st_mode & stat.S_IXUSR))


def request_bytes(transaction, target):
    request = {"protocol": "pennix-arch-v1", "step": "system-upgrade", "uid": os.getuid(),
               "transaction": transaction, "target": target}
    return owned_files.json_bytes(request)


def receipt_digest(transaction, target):
    return hashlib.sha256(request_bytes(transaction, target)).hexdigest()


def _file_sha256(path):
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def executor_identity():
    files = [EXECUTOR, COMMITTER, EXECUTOR.with_name("arch-transaction.so"), EXECUTOR.with_name("arch_policy.py")]
    if not _trusted_observer() or not all(_trusted_file(path) for path in files) or not COMMITTER.lstat().st_mode & stat.S_IXUSR:
        raise ArchCommitError("固定 Arch 执行器载荷归属不可核验")
    return {path.name: _file_sha256(path) for path in files}


def aur_candidate(name, source):
    try:
        directory, approved = arch_policy.local_package(name)
        if any(approved[field] != source[field] for field in ("ref", "version")):
            raise ArchCommitError("系统审批的 AUR 工件与登记来源的提交或版本不一致")
        artifact = directory / approved["filename"]
        signature = artifact.with_name(artifact.name + ".sig")
        if any(not _trusted_file(path) or _file_sha256(path) != approved[field]
               for path, field in ((artifact, "sha256"), (signature, "signature_sha256"))):
            raise ArchCommitError("审批的 AUR 工件或分离签名发生变化")
        identity = update_inventory.run(["/usr/bin/pacman", "-Qp", "--", str(artifact)], noninteractive=True)
        info, dependencies = candidates._arch_pkginfo(artifact)
        if (identity.returncode or identity.stderr or identity.stdout.split() != [name, approved["version"]]
                or info.get("pkgname") != name or info.get("pkgver") != approved["version"] or "replaces" in info):
            raise ArchCommitError("AUR 工件包身份不同或声明替换；未进入升级")
        if dependencies:
            status = update_inventory.run(["/usr/bin/pacman", "-T", "--", *dependencies], noninteractive=True)
            if status.returncode or status.stdout or status.stderr:
                raise ArchCommitError("AUR 依赖未满足；先单独以原生审阅路径准备依赖，再重新审阅")
        return approved
    except ArchCommitError:
        raise
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise ArchCommitError("缺少管理员审批的 AUR 包及分离签名；先单独准备工件，未安装软件") from error


def review_executor(target):
    """The public review never refreshes the live database or writes its cache."""
    try:
        identity = executor_identity()
        native = target["native_review"]
        config = update_inventory.run(["/usr/bin/pacman-conf", "--verbose"], timeout=15, noninteractive=True)
        if config.returncode or config.stderr or arch_policy.snapshot(config.stdout) != native["policy"]:
            raise ArchCommitError("本机 Arch 原生配置或 Hook 与审阅不一致")
        lock = _db_lock()
        sync = lock.parent / "sync"
        if (not arch_policy.trusted_directory(sync) or not _lock_absent(lock)
                or update_inventory.sync_digest(sync) != target["real_sync_sha256"]):
            raise ArchCommitError("Arch 包锁或实库状态已变化；请重新审阅")
        if update_inventory._arch_transaction(lock.parent, target["installed"], target["upgrades"],
                                               native["requested"], exact=True) != [
                                                   row for row in native["transaction"] if row["repo"] is not None]:
            raise ArchCommitError("本机 Arch 同步库尚不具备已审阅的精确包集；未执行升级")
        options = config.stdout.split("\n[", 1)[0].splitlines()
        caches = [Path(line.removeprefix("CacheDir = ")) for line in options if line.startswith("CacheDir = ")]
        for cache in caches:
            arch_policy.trusted_directory(cache)
        for row in native["transaction"]:
            if row["repo"] is None:
                if aur_candidate(row["name"], native["local"][row["name"]]) != native["local"][row["name"]]:
                    raise ArchCommitError("AUR 审批记录已变化；请重新审阅")
                continue
            name = row["filename"]
            if not re.fullmatch(r"[a-zA-Z0-9@._+-]+\.pkg\.tar(?:\.[a-z0-9]+)?", name):
                raise ArchCommitError("Arch 工件没有可信文件名")
            for cache in caches:
                artifact, signature = cache / name, cache / (name + ".sig")
                try:
                    artifact_trusted, signature_trusted = _trusted_file(artifact), _trusted_file(signature)
                except FileNotFoundError:
                    continue
                if not artifact_trusted or not signature_trusted or _file_sha256(artifact) != row["sha256"]:
                    raise ArchCommitError("Arch 缓存优先工件的归属或摘要不符；未执行升级")
                break
            else:
                raise ArchCommitError("本机 Arch 缓存缺少已审阅包工件或分离签名；未执行升级")
        return identity
    except ArchCommitError:
        raise
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise ArchCommitError("固定 Arch 执行器或本机原生提交前置条件不可核验") from error


def commit(transaction, target, identity):
    try:
        trusted = executor_identity() == identity
    except (OSError, ValueError):
        trusted = False
    if not trusted:
        raise ArchCommitError("固定 Arch 执行器身份已变；未发送提交请求", effect="none")
    data = request_bytes(transaction, target)
    if len(data) > 8 * 1024 * 1024:
        raise ArchCommitError("Arch 审阅请求超过上限；未发送提交请求", effect="none")
    digest = hashlib.sha256(data).hexdigest()
    try:
        result = subprocess.run(["/usr/bin/sudo", "-n", "--", str(COMMITTER), "commit", transaction, digest],
                                input=data, capture_output=True, start_new_session=True, timeout=1800, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        raise ArchCommitError("固定 Arch 提交没有可核验结果；保留事务，不重放") from error
    if result.returncode == 1 and result.stdout == f"pennix-arch-refused-v1 {digest}\n".encode():
        raise ArchCommitError("固定 Arch 执行器在写包前拒绝了请求；保留事务", effect="none")
    if result.returncode != 0 or (evidence := observe(transaction, target)) is None:
        raise ArchCommitError("Arch 提交结果或实装后置条件无法核验；保留事务，不重放")
    return evidence


def _post_install_set(target):
    before = target.get("installed")
    review = target.get("native_review")
    if not isinstance(before, dict) or not isinstance(review, dict):
        return None
    rows = review.get("transaction")
    if not isinstance(rows, list) or not rows:
        return None
    expected, seen = dict(before), set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"repo", "name", "installed", "target", "filename", "sha256"}:
            return None
        name, version = row["name"], row["target"]
        if (not isinstance(name, str) or not name or name in seen
                or not isinstance(version, str) or not version or before.get(name) != row["installed"]):
            return None
        expected[name] = version
        seen.add(name)
    return expected


def _db_lock():
    info = update_inventory.run(["/usr/bin/pacman-conf", "DBPath"], timeout=15, noninteractive=True)
    (dbpath, *tail) = info.stdout.splitlines() or [""]
    path = Path(dbpath)
    if info.returncode or info.stderr or tail or not path.is_absolute() or ".." in path.parts:
        raise update_inventory.UpdateError("无法确认 Arch 包数据库路径")
    return path / "db.lck"


def _lock_absent(path):
    try:
        path.lstat()
    except FileNotFoundError:
        return True
    return False


def observe(transaction, target):
    """Missing or uncertain evidence never makes a step replayable."""
    expected = _post_install_set(target)
    if expected is None or not re.fullmatch(r"[0-9a-f]{32}", transaction):
        return None
    digest = receipt_digest(transaction, target)
    try:
        if not _trusted_observer():
            return None
        receipt = update_inventory.run(
            ["/usr/bin/sudo", "-n", "--", str(EXECUTOR), "observe", transaction, digest],
            timeout=15, noninteractive=True)
        if receipt.returncode or receipt.stdout or receipt.stderr:
            return None
        lock = _db_lock()
        if not _lock_absent(lock) or update_inventory.installed_arch(pacman="/usr/bin/pacman") != expected:
            return None
        check = update_inventory.run(["/usr/bin/pacman", "-Dk"], timeout=60, noninteractive=True)
        if (check.returncode or check.stderr
                or update_inventory.installed_arch(pacman="/usr/bin/pacman") != expected
                or _db_lock() != lock or not _lock_absent(lock)):
            return None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    return {"recovered": True, "state": "native-commit-verified", "receipt_sha256": digest}
