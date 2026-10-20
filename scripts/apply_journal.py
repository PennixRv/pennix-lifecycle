"""Small durable journal for lifecycle mutations and safe apply resumption."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

import owned_files


class JournalError(ValueError):
    pass


SCHEMA = 1
STATUSES = {"pending", "started", "verified", "unknown"}
STEP_ID = re.compile(r"[A-Za-z0-9_.:-]+")


def path() -> Path:
    return owned_files.state_root() / "apply-journal.json"


def digest(pathname: Path) -> str | None:
    raw = owned_files.read(pathname)
    return None if raw is None else hashlib.sha256(raw).hexdigest()


def _validate(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != {
            "schema", "command", "component", "config_digest", "lock_digest", "steps"}:
        raise JournalError("生命周期 journal 格式无效")
    if (value["schema"] != SCHEMA or value["command"] not in {"apply", "update"}
            or (value["component"] is not None and not isinstance(value["component"], str))
            or not re.fullmatch(r"[0-9a-f]{64}", value["config_digest"])
            or (value["lock_digest"] is not None and not re.fullmatch(r"[0-9a-f]{64}", value["lock_digest"]))
            or not isinstance(value["steps"], list) or not value["steps"]):
        raise JournalError("生命周期 journal 元数据无效")
    seen = set()
    for step in value["steps"]:
        if (not isinstance(step, dict)
                or set(step) != {"id", "component", "action", "target", "status", "observed"}
                or not isinstance(step["id"], str) or not STEP_ID.fullmatch(step["id"])
                or step["id"] in seen or not isinstance(step["component"], str)
                or not isinstance(step["action"], str) or not isinstance(step["target"], dict)
                or step["status"] not in STATUSES
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
    value = {"schema": SCHEMA, "command": command, "component": component,
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
    for step in updated["steps"]:
        if step["id"] == step_id:
            if status is not None:
                step["status"] = status
            if target is not None:
                step["target"] = dict(target)
            if observed is not None:
                step["observed"] = dict(observed)
            _write(updated, owned_files.read(path(), private=True))
            return updated
    raise JournalError(f"生命周期 journal 缺少步骤: {step_id}")


def set_target(value: dict, step_id: str, target: dict) -> dict:
    return _replace(value, step_id, target=target)


def started(value: dict, step_id: str) -> dict:
    return _replace(value, step_id, status="started")


def verified(value: dict, step_id: str, observed: dict | None = None) -> dict:
    return _replace(value, step_id, status="verified", observed=observed or {})


def unknown(value: dict, step_id: str) -> dict:
    return _replace(value, step_id, status="unknown")


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
        owned_files.replace(journal_path, None, before)


def complete(value: dict) -> bool:
    return all(item["status"] == "verified" for item in value["steps"])


def plan_ids(value: dict) -> tuple[str, ...]:
    return tuple(item["id"] for item in value["steps"])
