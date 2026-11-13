"""A bounded npm -> Arch pnpm transition, using the existing apply journal."""
from __future__ import annotations

import hashlib
from pathlib import Path
from copy import copy
import subprocess

import candidates
import owned_files as files

LEGACY_SOURCE = "npm:pnpm:latest"


def old_component():
    return {"id": "pnpm", "delivery": "package", "lock_source": LEGACY_SOURCE,
            "probe": "pnpm", "version_args": ["--version"],
            "package": {"source": "npm", "name": "pnpm", "registry": "https://registry.npmjs.org/",
                        "tag": "latest", "prefix": "$HOME/.local", "install_owner": "user"}}


def directory():
    return files.state_root() / "pnpm-source-migration"


def pending():
    return files.read_json(directory() / "record.json")


def needed():
    import _engine as engine
    raw = files.read_json(engine._package_receipt_path("pnpm"))
    return bool(raw and raw.get("source") == "npm") or pending() is not None


def inspect(lock):
    import _engine as engine
    record = pending()
    if record is not None:
        if (set(record) != {"schema", "target", "old_lock", "old_receipt", "archive_sha256", "new_target"}
                or record["schema"] != 1 or record["target"] != files.target_identity()
                or files.read(directory()/"package.tgz", private=True) is None
                or hashlib.sha256(files.read(directory()/"package.tgz", private=True)).hexdigest() != record["archive_sha256"]
                or lock not in [record["old_lock"], record["new_target"]]):
            raise engine.BootstrapError("pnpm 迁移恢复材料与目标不匹配")
        return record
    old = old_component()
    collision = subprocess.run(['pacman', '-Q', 'pnpm'], capture_output=True, text=True, check=False)
    if collision.returncode == 0:
        raise engine.BootstrapError("旧 npm pnpm 与未知系统 pnpm 同时存在；未接管或覆盖")
    if collision.returncode != 1:
        raise engine.BootstrapError("不能核验系统 pnpm 冲突")
    receipt = engine._package_receipt("pnpm", old)
    if not receipt or not lock or lock.get("source") != LEGACY_SOURCE:
        raise engine.BootstrapError("pnpm 迁移需要可信 npm receipt 与旧锁")
    identity = engine.package_identity(old)
    if identity["version"] != receipt["version"] or identity["version"] != lock["version"]:
        raise engine.BootstrapError("pnpm 的 npm 实际身份、receipt 和旧锁不一致")
    return {"old_receipt": receipt, "old_lock": lock}


def apply(args, catalog, component, candidate):
    import _engine as engine
    lock = args.component_lock["components"].get("pnpm")
    record = inspect(lock)
    target = candidates.lock_entry(candidate)
    if pending() is None:
        with __import__('tempfile').TemporaryDirectory(prefix="pennix-pnpm-old-") as tmp:
            old = old_component()
            archive = candidates.resolve_npm(old, Path(tmp), lock)
            old['_lock_target'] = lock
            engine.recover_npm_receipt('pnpm', old, lock, Path(archive['path']))
            content = Path(archive["path"]).read_bytes()
            files.replace(directory()/"package.tgz", content, None)
            record.update(schema=1, target=files.target_identity(), archive_sha256=hashlib.sha256(content).hexdigest(),
                          new_target=target)
            files.replace(directory()/"record.json", files.json_bytes(record), None)
    elif record["new_target"] != target:
        raise engine.BootstrapError("pnpm 迁移候选与原事务不同")
    old = old_component()
    if not engine.package_absent(old):
        engine.component_operation(args, catalog, "pnpm", old, "uninstall")
    if not engine.package_absent(old):
        raise engine.BootstrapError("旧 pnpm 入口尚未退出")
    current = files.read_json(engine._package_receipt_path("pnpm"))
    if current and current.get("source") == "npm":
        raise engine.BootstrapError("旧 pnpm receipt 尚未由原 owner 撤回")
    return engine.component_operation(args, catalog, "pnpm", component, "install")


def finish(component, lock):
    import _engine as engine
    record = pending()
    if record is None:
        return
    inspect(lock)
    if lock != record["new_target"] or not engine.package_owned("pnpm", component):
        raise engine.BootstrapError("pnpm 新归属尚未核验，保留恢复材料")
    _cleanup()


def restore(args, catalog, component):
    import _engine as engine
    record = inspect(args.component_lock['components'].get('pnpm'))
    if pending() is None:
        raise engine.BootstrapError("没有绑定本次 pnpm 迁移的恢复材料")
    current = copy(component)
    current['_lock_target'] = record['new_target']
    if not engine.package_absent(current):
        if not engine.package_owned('pnpm', current):
            raise engine.BootstrapError("系统 pnpm 不属于本次迁移；未撤回")
        engine.component_operation(args, catalog, 'pnpm', current, 'uninstall')
    old = old_component()
    old['_lock_target'] = record['old_lock']
    recovery_args = copy(args)
    recovery_args.candidate = {'path': str(directory()/'package.tgz')}
    recovery_args.explicit_update = False
    engine.component_operation(recovery_args, catalog, 'pnpm', old, 'install')
    if not engine.package_owned('pnpm', old) or engine.package_identity(old)['version'] != record['old_lock']['version']:
        raise engine.BootstrapError("旧 pnpm 原生归属未恢复，保留材料")
    return record['old_lock']


def finish_restore(lock):
    record = inspect(lock)
    if lock != record['old_lock']:
        raise ValueError("pnpm 旧锁尚未恢复")
    _cleanup()


def _cleanup():
    import _engine as engine
    if {p.name for p in directory().iterdir()} != {"record.json", "package.tgz"}:
        raise engine.BootstrapError("pnpm 迁移目录含未知内容，未清理")
    for name in ["record.json", "package.tgz"]:
        path = directory()/name
        files.replace(path, None, files.read(path, private=True))
    directory().rmdir()
