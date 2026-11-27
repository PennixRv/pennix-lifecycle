"""Ponytail's native Codex registration and exact plugin identity."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tomllib

import candidates
import owned_files


KEY = "ponytail-plugin"


class PonytailError(ValueError):
    pass


def selected(component):
    return component.get("id") == KEY


def commit(value):
    return isinstance(value, str) and candidates.SHA.fullmatch(value)


def version(value):
    return isinstance(value, str) and candidates.STABLE_VERSION.fullmatch(value)


def formal(entry, component):
    return (isinstance(entry, dict) and set(entry) == {"source", "version", "ref", "sha256"}
            and entry["source"] == component["lock_source"] and version(entry["version"])
            and commit(entry["ref"]) and isinstance(entry["sha256"], str)
            and re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]))


class Native:
    def __init__(self, component, home):
        self.home = home
        self.plugin = component["plugin"]["id"]
        self.name, self.market = self.plugin.split("@")
        self.url = component["plugin"]["marketplace"]["source"]
        self.root = owned_files.state_root() / "plugins" / KEY
        self.manifest = self.root / ".agents/plugins/marketplace.json"
        self.codex = shutil.which("codex")

    def call(self, *args):
        if not self.codex:
            raise PonytailError("Codex CLI 不可用")
        try:
            result = subprocess.run([self.codex, "plugin", *args, "--json"], check=False,
                                    capture_output=True, text=True, timeout=120 if args[0] == "add" or
                                    args[:2] == ("marketplace", "add") else 30,
                                    env={**os.environ, "CODEX_HOME": str(self.home)})
            if result.returncode == 0:
                value = json.loads(result.stdout)
                if isinstance(value, dict):
                    return value
        except (OSError, ValueError, subprocess.TimeoutExpired) as error:
            raise PonytailError(f"Codex 插件操作结果不可核验：{' '.join(args[:2])}") from error
        raise PonytailError(f"Codex 插件操作未通过：{' '.join(args[:2])}")

    def document(self, ref):
        if not commit(ref):
            raise PonytailError("插件清单必须固定完整提交")
        return owned_files.json_bytes({"name": self.market, "plugins": [{
            "name": self.name, "source": {"source": "url", "url": self.url, "ref": ref}}]})

    def registered_file(self):
        owned_files.safe_path(self.manifest)
        for parent, child in ((self.root, ".agents"), (self.root / ".agents", "plugins"),
                              (self.manifest.parent, self.manifest.name)):
            if parent.exists() and (not parent.is_dir() or any(item.name != child for item in parent.iterdir())):
                raise PonytailError("Ponytail 本机登记目录包含未知内容")
        raw = owned_files.read(self.manifest, private=True)
        if raw is None:
            return None
        try:
            ref = json.loads(raw)["plugins"][0]["source"]["ref"]
            if self.document(ref) == raw:
                return ref
        except (ValueError, TypeError, KeyError, IndexError):
            pass
        raise PonytailError("Ponytail 本机清单内容或归属未知")

    def write(self, ref, before):
        owned_files.replace(self.manifest, None if ref is None else self.document(ref),
                            None if before is None else self.document(before))

    def git(self, root, *args):
        return candidates._run(["git", "-C", str(root), "-c", "core.fsmonitor=false", *args], timeout=30)

    def checkout(self, root):
        owned_files.safe_path(root / ".git")
        if not (root / ".git").is_dir():
            raise PonytailError("原生插件或 Marketplace 缺少 Git checkout")
        location = self.git(root, "rev-parse", "--show-toplevel")
        head = self.git(root, "rev-parse", "HEAD")
        status = self.git(root, "status", "--porcelain", "--untracked-files=all")
        origin = self.git(root, "config", "--local", "--get-all", "remote.origin.url")
        files = self.git(root, "ls-files", "-v")
        if (any(r.returncode for r in (location, head, status, origin, files))
                or location.stdout.rstrip("\n") != str(root) or not commit(head.stdout.strip())
                or origin.stdout.splitlines() != [self.url]
                or any(x != "?? .codex-marketplace-install.json" for x in status.stdout.splitlines())
                or any(x[:1] != "H" for x in files.stdout.splitlines())):
            raise PonytailError("原生 Git checkout 来源、内容或提交不可核验")
        return head.stdout.strip()

    def cached(self, release_version):
        if not version(release_version):
            raise PonytailError("原生插件载荷版本无效")
        root = self.home / "plugins/cache" / self.market / self.name / release_version
        raw = owned_files.read(root / ".codex-plugin/plugin.json")
        if raw is None:
            raise PonytailError("原生插件载荷不存在")
        try:
            manifest = json.loads(raw, object_pairs_hook=owned_files.unique_pairs)
            if manifest["name"] != self.name or manifest["version"] != release_version:
                raise ValueError("identity mismatch")
        except (ValueError, KeyError, TypeError) as error:
            raise PonytailError("原生插件载荷清单身份不匹配") from error
        return {"version": release_version, "ref": self.checkout(root)}

    def empty(self, state):
        if state != {"market": None, "file": None, "payload": None}:
            return False
        cache = self.home / "plugins/cache" / self.market / self.name
        owned_files.safe_path(cache)
        return not cache.exists() or (cache.is_dir() and next(cache.iterdir(), None) is None)

    def observe(self):
        config = owned_files.read(self.home / "config.toml")
        if config is not None:
            try:
                if tomllib.loads(config.decode()).get("plugins", {}).get(self.plugin, {}).get("enabled") is False:
                    raise PonytailError("Ponytail 被用户禁用；保留现场")
            except (AttributeError, UnicodeError, tomllib.TOMLDecodeError) as error:
                raise PonytailError("无法核验 Codex 插件配置") from error
        ref = self.registered_file()
        if not self.codex:
            if ref is not None:
                raise PonytailError("不能在 Codex 不可用时核验现有登记")
            return {"market": None, "file": None, "payload": None}
        data = self.call("marketplace", "list")
        rows = data.get("marketplaces")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise PonytailError("Codex Marketplace 清单无效")
        matches = [row for row in rows if row.get("name") == self.market]
        market = None
        if matches:
            if len(matches) != 1:
                raise PonytailError("存在多份同名 Marketplace 登记")
            row = matches[0]
            source = row.get("marketplaceSource")
            if (source == {"sourceType": "local", "source": str(self.root)}
                    and row.get("root") == str(self.root) and ref is not None):
                market = {"kind": "local", "ref": ref}
            elif (source == {"sourceType": "git", "source": self.url}
                    and row.get("root") == str(self.home / ".tmp/marketplaces" / self.market)):
                market = {"kind": "legacy", "ref": self.checkout(Path(row["root"]))}
            else:
                raise PonytailError("同名 Marketplace 属于外部或无法核验")
        listing = self.call("list", "--marketplace", self.market)
        installed = listing.get("installed")
        for group in (installed, listing.get("available")):
            if (not isinstance(group, list) or len(group) > 1
                    or any(not isinstance(row, dict) or row.get("pluginId") != self.plugin for row in group)):
                raise PonytailError("插件清单未知或 Marketplace 被其他插件共享")
        payload = None
        if installed:
            item = installed[0]
            source = item.get("source")
            if (market is None or not isinstance(source, dict) or source.get("source") != "git"
                    or source.get("url") != self.url or item.get("enabled") is not True
                    or (market["kind"] == "local" and source.get("ref") != ref)):
                raise PonytailError("插件来源未固定、启用状态未知或被用户禁用")
            payload = self.cached(item.get("version"))
        return {"market": market, "file": ref, "payload": payload}

    def install(self, target):
        current = self.observe()
        if current["market"] != {"kind": "local", "ref": target["ref"]} or current["file"] != target["ref"]:
            raise PonytailError("安装前未核验到固定的本机登记；不调用 Codex 安装")
        response = self.call("add", self.plugin)
        expected = self.home / "plugins/cache" / self.market / self.name / target["version"]
        if response.get("pluginId") != self.plugin or response.get("installedPath") != str(expected):
            raise PonytailError("Codex 安装回执未确认目标载荷")
        if not self.matches(self.observe(), target):
            raise PonytailError("实际插件载荷未匹配冻结提交")

    @staticmethod
    def matches(state, target):
        ref = target["ref"]
        return (state["market"] == {"kind": "local", "ref": ref} and state["file"] == ref
                and state["payload"] == {"version": target["version"], "ref": ref})


def prove_retrievable(native, workspace, *refs):
    setup = candidates._run(["git", "init", "--bare", "--quiet", str(workspace)])
    if setup.returncode:
        raise PonytailError("无法建立隔离回退验证目录")
    result = candidates._run(["git", "-C", str(workspace), "fetch", "--depth=1", "--no-tags",
                              native.url, *dict.fromkeys(refs)], timeout=120)
    if result.returncode:
        raise PonytailError("无法从原上游取回全部旧提交；未开始迁移")
    for ref in set(refs):
        if not commit(ref) or native.git(workspace, "cat-file", "-t", ref).stdout.strip() != "commit":
            raise PonytailError("上游旧对象不是预期的完整提交")


def receipt_path():
    return owned_files.state_root() / "plugins" / f"{KEY}.json"


def receipt_record(component, entry):
    if not formal(entry, component):
        raise PonytailError("未核验正式发行；不能写入插件回执")
    return {"schema": 2, "component": KEY, "id": component["plugin"]["id"],
            "target": owned_files.target_identity(), "tag": f"v{entry['version']}", **entry}


def receipt(component):
    path = receipt_path()
    raw = owned_files.read(path, private=True)
    record = owned_files.read_json(path)
    if raw != owned_files.read(path, private=True):
        raise PonytailError("读取期间 Ponytail 回执改变")
    if raw is None:
        return None, None
    identity = owned_files.target_identity()
    if (not isinstance(record, dict) or record.get("component") != KEY or
            record.get("id") != component["plugin"]["id"]
            or ("target" in record and record["target"] != identity)):
        raise PonytailError("Ponytail 回执与当前目标不符")
    entry = {k: record[k] for k in ("source", "version", "ref", "sha256") if k in record}
    if type(record.get("schema")) is int and record["schema"] == 2 and formal(entry, component):
        if record == receipt_record(component, entry):
            return record, raw
    if (type(record.get("schema")) is int and record["schema"] == 1 and
            set(record) - {"target"} == {"schema", "component", "id", "source", "version", "ref"}
            and record["source"] == component["lock_source"] and version(record["version"]) and commit(record["ref"])):
        if "target" not in record:
            if raw != owned_files.json_bytes(record):
                raise PonytailError("无目标的 Ponytail 旧回执格式不一致；保留现场")
            owned_files.bind_target(Path(identity["codex_home"]), require_existing=True)
        return record, raw
    raise PonytailError("Ponytail 回执格式无效")


def receipt_entry(record):
    return None if record is None else {k: record[k] for k in ("source", "version", "ref", "sha256") if k in record}


def matches_receipt(lock, record):
    if not isinstance(lock, dict) or record is None:
        return False
    entry = receipt_entry(record)
    return lock == entry if record["schema"] == 2 else candidates.matches(lock, entry)


def matches_legacy(state, record):
    return (state["market"] == {"kind": "legacy", "ref": record["ref"]}
            and state["file"] is None and state["payload"] is not None
            and ("target" in record or state["payload"] == {"version": record["version"], "ref": record["ref"]}))


def inspect(component, home):
    native = Native(component, home)
    state = native.observe()
    record, _ = receipt(component)
    entry = receipt_entry(record)
    target = component.get("_candidate_target") or component.get("_lock_target")
    result = {"actual_payload": state["payload"], "marketplace": state["market"],
              "locked_identity": target}
    if record and record["schema"] == 2 and native.matches(state, entry):
        return {**result, "state": "match" if entry == target else "outdated", "detail": entry["version"],
                "ownership": "lifecycle", "release_tag": record["tag"]}
    if record and record["schema"] == 1 and matches_legacy(state, record) and matches_receipt(target, record):
        return {**result, "state": "plugin-migration-required",
                "detail": "旧登记待迁移：pennix update ponytail-plugin --json"}
    if record is None and native.empty(state):
        return {**result, "state": "missing", "detail": None}
    return {**result, "state": "blocked", "detail": "Ponytail 登记、实际载荷或 Lifecycle 回执不一致；保留现场"}


def status(component, home):
    result = inspect(component, home)
    return result["state"], result["detail"]


def owned(component, home):
    record, _ = receipt(component)
    return bool(record and record["schema"] == 2 and Native.matches(
        Native(component, home).observe(), receipt_entry(record)))


def operate(component, home, operation, candidate):
    native = Native(component, home)
    before = native.observe()
    record, raw = receipt(component)
    entry = receipt_entry(record)
    managed = record and record["schema"] == 2 and native.matches(before, entry)
    empty = record is None and native.empty(before)
    if operation == "uninstall":
        if empty:
            return "no-op"
        if not managed:
            raise PonytailError("非正式或归属未知的 Ponytail 不能卸载")
        native.call("remove", native.plugin)
        if native.observe() != {**before, "payload": None}:
            raise PonytailError("Codex 未证明插件已移除；保留登记")
        native.call("marketplace", "remove", native.market)
        if native.observe() != {"market": None, "file": entry["ref"], "payload": None}:
            raise PonytailError("Codex 未证明登记已移除；保留清单与回执")
        native.write(None, entry["ref"])
        owned_files.replace(receipt_path(), None, raw)
        return "changed"
    if not (empty or managed):
        raise PonytailError("旧登记须经专用单项迁移；未知归属或禁用状态不变更")
    if managed and operation != "upgrade" and entry == component.get("_lock_target"):
        return "no-op"
    target = candidates.lock_entry(candidate) if candidate else None
    if not candidate or candidate.get("tag") != f"v{candidate.get('version')}" or not formal(target, component):
        raise PonytailError("缺少冻结的 Ponytail 正式发行")
    candidates.verify_plugin_upgrade(entry, target)
    native.write(target["ref"], before["file"])
    if empty:
        native.call("marketplace", "add", str(native.root))
    native.install(target)
    owned_files.replace(receipt_path(), owned_files.json_bytes(receipt_record(component, target)), raw)
    return "changed"


def move(native, before, target):
    """Resume only recognizable forward states captured by the confirmed transaction."""
    current = native.observe()
    if native.matches(current, target):
        return
    if current == before and before["market"]["kind"] == "legacy":
        native.call("marketplace", "remove", native.market)
        current = native.observe()
    ref, old = target["ref"], before["payload"]
    if current["market"] is None and before["market"]["kind"] == "legacy":
        if (current["file"] not in {None, ref} or current["payload"] is not None
                or native.cached(old["version"]) != old):
            raise PonytailError("登记移除后的现场与旧载荷不符；保留事务")
        native.write(ref, current["file"])
        native.call("marketplace", "add", str(native.root))
        current = native.observe()
    elif current == before and before["market"]["kind"] == "local":
        native.write(ref, before["file"])
        current = native.observe()
    if native.matches(current, target):
        return
    if (current["market"] != {"kind": "local", "ref": ref} or current["file"] != ref
            or (current["payload"] != old and
                (current["payload"] is not None or native.cached(old["version"]) != old))):
        raise PonytailError("待安装状态不匹配事务；不重试未知副作用")
    native.install(target)


def restore(native, before, target):
    current = native.observe()
    old, ref = before["payload"], target["ref"]
    new = {"version": target["version"], "ref": ref}
    if current == before:
        return
    if current["market"] and current["market"]["kind"] == "local":
        if (current["file"] not in {old["ref"], ref} or
                (current["payload"] not in (old, new) and
                 (current["payload"] is not None or native.cached(old["version"]) != old))):
            raise PonytailError("本机登记或载荷不是事务记录的前后值")
        native.write(old["ref"], current["file"])
        if current["payload"] != old:
            native.install(old)
        if not native.matches(native.observe(), old):
            raise PonytailError("旧载荷恢复未通过核验")
        if before["market"]["kind"] == "legacy":
            native.call("marketplace", "remove", native.market)
        current = native.observe()
    if before["market"]["kind"] == "legacy":
        if current["market"] is None:
            if (current["file"] not in {None, old["ref"], ref} or current["payload"] is not None
                    or native.cached(old["version"]) != old):
                raise PonytailError("不能证明原生撤回后的旧载荷")
            # Restore the old outer registration without reinstalling its moving plugin ref.
            native.call("marketplace", "add", native.url, "--ref", before["market"]["ref"])
            current = native.observe()
        if (current["market"] != before["market"] or current["payload"] != old
                or current["file"] not in {None, old["ref"], ref}):
            raise PonytailError("原 Git 登记或旧载荷未能精确恢复")
        if current["file"] is not None:
            native.write(None, current["file"])
    if native.observe() != before:
        raise PonytailError("旧登记、启用状态及载荷未能精确恢复")
