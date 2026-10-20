"""Strict per-config lock for exact external component deliveries."""
from __future__ import annotations

import re
from pathlib import Path

import owned_files


class LockError(ValueError):
    pass


FIELDS = {"source", "version", "ref", "sha256"}
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def path_for(config: Path) -> Path:
    return config.with_name("components.lock.json")


def validate(value: object, sources: dict[str, str | tuple[str, ...]]) -> dict:
    if (not isinstance(value, dict) or set(value) != {"schema", "components"}
            or type(value["schema"]) is not int or value["schema"] != 1
            or not isinstance(value["components"], dict)):
        raise LockError("components.lock.json schema is invalid")
    components = value["components"]
    for key, entry in components.items():
        if key not in sources or not isinstance(entry, dict) or not {"source"} <= set(entry) <= FIELDS:
            raise LockError(f"components.lock.json entry is invalid: {key}")
        expected_sources = sources[key]
        if isinstance(expected_sources, str):
            expected_sources = (expected_sources,)
        if (entry["source"] not in expected_sources
                or not any(field in entry for field in ("version", "ref", "sha256"))):
            raise LockError(f"components.lock.json source or target identity differs: {key}")
        if any(field in entry and (not isinstance(entry[field], str) or not entry[field])
               for field in ("source", "version", "ref", "sha256")):
            raise LockError(f"components.lock.json identity field is invalid: {key}")
        if "sha256" in entry and not SHA256.fullmatch(entry["sha256"]):
            raise LockError(f"components.lock.json SHA-256 is invalid: {key}")
    return value


def read(path: Path, sources: dict[str, str | tuple[str, ...]]) -> dict:
    try:
        value = owned_files.read_json(path)
    except owned_files.OwnershipError as error:
        raise LockError(str(error)) from error
    if value is None:
        return {"schema": 1, "components": {}}
    return validate(value, sources)


def write(path: Path, value: dict, sources: dict[str, str | tuple[str, ...]]) -> None:
    validate(value, sources)
    previous = owned_files.read(path, private=True)
    owned_files.replace(path, owned_files.json_bytes(value), previous, mode=0o600)


def set_entry(value: dict, key: str, entry: dict, sources: dict[str, str | tuple[str, ...]]) -> dict:
    updated = {"schema": 1, "components": dict(value["components"])}
    updated["components"][key] = entry
    return validate(updated, sources)


def remove_entry(value: dict, key: str) -> dict:
    updated = {"schema": 1, "components": dict(value["components"])}
    updated["components"].pop(key, None)
    return updated
