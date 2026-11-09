"""Small durable journal for lifecycle mutations and safe apply resumption."""
from __future__ import annotations

import hashlib
import re
import os
import uuid
from pathlib import Path

import owned_files


class JournalError(ValueError):
    pass


SCHEMA = 2
STATUSES = {"pending", "started", "verified", "unknown"}
STEP_ID = re.compile(r"[A-Za-z0-9_.:-]+")


def path() -> Path:
    return owned_files.state_root() / "apply-journal.json"


def digest(pathname: Path) -> str | None:
    raw = owned_files.read(pathname)
    return None if raw is None else hashlib.sha256(raw).hexdigest()


def _validate(value: object) -> dict:
    if not isinstance(value, dict):
        raise JournalError("生命周期 journal 格式无效")
    fields = {"schema", "command", "component", "config_digest", "lock_digest", "steps"}
    if set(value) != (fields if value.get("schema") == 1 else fields | {"transaction", "identity"}):
        raise JournalError("生命周期 journal 格式无效")
    if (type(value["schema"]) is not int or value["schema"] not in {1, SCHEMA}
            or not isinstance(value["command"], str) or value["command"] not in {"apply", "update", "reconcile", "repair", "uninstall"}
            or (value["component"] is not None and not isinstance(value["component"], str))
            or not isinstance(value["config_digest"], str) or not re.fullmatch(r"[0-9a-f]{64}", value["config_digest"])
            or (value["lock_digest"] is not None and (not isinstance(value["lock_digest"], str) or not re.fullmatch(r"[0-9a-f]{64}", value["lock_digest"])))
            or not isinstance(value["steps"], list) or not value["steps"]):
        raise JournalError("生命周期 journal 元数据无效")
    if value["schema"] == SCHEMA:
        if (not isinstance(value["transaction"], str) or not re.fullmatch(r"[0-9a-f]{32}", value["transaction"])
                or not isinstance(value["identity"], dict)
                or set(value["identity"]) != {"home", "codex_home", "state_root"}
                or any(not isinstance(path, str) or not Path(path).is_absolute() for path in value["identity"].values())):
            raise JournalError("生命周期 journal 事务或目标身份无效")
    seen = set()
    for step in value["steps"]:
        if (not isinstance(step, dict)
                or set(step) != {"id", "component", "action", "target", "status", "observed"}
                or not isinstance(step["id"], str) or not STEP_ID.fullmatch(step["id"])
                or step["id"] in seen or not isinstance(step["component"], str)
                or not isinstance(step["action"], str) or not isinstance(step["target"], dict)
                or not isinstance(step["status"], str) or step["status"] not in STATUSES
                or (step["observed"] is not None and not isinstance(step["observed"], dict))):
            raise JournalError("生命周期 journal 步骤无效")
        seen.add(step["id"])
    return value


def read() -> dict | None:
    value = owned_files.read_json(path())
    return None if value is None else _validate(value)


def _write(value: dict, before: bytes | None) -> None:
    _validate(value)
    owned_files.replace(path(), owned_files.json_bytes(value), before)


def start(command: str, component: str | None, config_digest: str,
          lock_digest: str | None, steps: list[dict]) -> dict:
    existing = read()
    if existing is not None:
        if not complete(existing):
            raise JournalError("存在未完成生命周期事务；不能覆盖")
        clear()
    value = {"schema": SCHEMA, "command": command, "component": component,
             "transaction": uuid.uuid4().hex,
             "identity": {"home": str(Path.home()), "codex_home": os.environ.get("CODEX_HOME", str(Path.home() / ".codex")),
                          "state_root": str(owned_files.state_root())},
             "config_digest": config_digest, "lock_digest": lock_digest,
             "steps": [{"id": step["id"], "component": step["component"],
                        "action": step["action"], "target": dict(step.get("target", {})),
                        "status": "pending", "observed": None} for step in steps]}
    _write(value, owned_files.read(path(), private=True))
    return value


def _replace(value: dict, step_id: str, *, status: str | None = None,
             target: dict | None = None, observed: dict | None = None) -> dict:
    if status is not None and status not in STATUSES:
        raise JournalError("生命周期 journal 状态无效")
    updated = {**value, "steps": [dict(step) for step in value["steps"]]}
    before = owned_files.read(path(), private=True)
    if before != owned_files.json_bytes(value):
        raise JournalError("生命周期 journal 在步骤更新前发生变化")
    for step in updated["steps"]:
        if step["id"] == step_id:
            if status is not None:
                step["status"] = status
            if target is not None:
                step["target"] = dict(target)
            if observed is not None:
                step["observed"] = dict(observed)
            _write(updated, before)
            return updated
    raise JournalError(f"生命周期 journal 缺少步骤: {step_id}")


def set_target(value: dict, step_id: str, target: dict) -> dict:
    return _replace(value, step_id, target=target)


def checkpoint_lock(lock_digest: str) -> None:
    value = read()
    if value is not None:
        value["lock_digest"] = lock_digest
        _write(value, owned_files.read(path(), private=True))


def replan(value: dict, steps: list[dict]) -> dict:
    if any(step["status"] not in {"pending", "verified"} for step in value["steps"]):
        raise JournalError("不能替换存在未知副作用的计划")
    verified_steps = [step for step in value["steps"] if step["status"] == "verified"]
    verified_ids = {step["id"] for step in verified_steps}
    updated = {**value, "steps": verified_steps + [
        {**step, "status": "pending", "observed": None}
        for step in steps if step["id"] not in verified_ids]}
    _write(updated, owned_files.read(path(), private=True))
    return updated


def started(value: dict, step_id: str) -> dict:
    return _replace(value, step_id, status="started")


def verified(value: dict, step_id: str, observed: dict | None = None) -> dict:
    return _replace(value, step_id, status="verified", observed=observed or {})


def unknown(value: dict, step_id: str) -> dict:
    return _replace(value, step_id, status="unknown")


def rejected(value: dict, step_id: str, effect: str) -> dict:
    if effect not in {"none", "restored"}:
        return _replace(value, step_id, status="unknown", observed={"effect": "unknown"})
    return _replace(value, step_id, status="pending", observed={"effect": effect})


def retry(value: dict, step_id: str) -> dict:
    if step(value, step_id)["status"] != "unknown":
        raise JournalError("only an unknown step can be retried after owner verification")
    return _replace(value, step_id, status="pending")


def step(value: dict, step_id: str) -> dict:
    for item in value["steps"]:
        if item["id"] == step_id:
            return item
    raise JournalError(f"生命周期 journal 缺少步骤: {step_id}")


def clear() -> None:
    journal_path = path()
    before = owned_files.read(journal_path, private=True)
    if before is not None:
        value = read()
        if not complete(value):
            raise JournalError("未知或未完成事务不能清除")
        transaction = value.get("transaction", hashlib.sha256(before).hexdigest()[:32])
        archive = owned_files.state_root() / "transactions" / f"{transaction}.json"
        previous = owned_files.read(archive, private=True)
        if previous is not None and previous != before:
            raise JournalError("事务处置证据已存在且不匹配")
        owned_files.replace(archive, before, previous)
        owned_files.replace(journal_path, None, before)


def complete(value: dict) -> bool:
    return all(item["status"] == "verified" for item in value["steps"])


def plan_ids(value: dict) -> tuple[str, ...]:
    return tuple(item["id"] for item in value["steps"])
