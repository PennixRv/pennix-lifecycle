"""One-time cleanup after the old native search owners have withdrawn."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

import apply_journal
import owned_files as files


# Only preferences left by Grok's native unconfigure; credentials are excluded.
PREFERENCES = {
    "apiProvider", "deadlineSeconds", "defaultExtra", "maxSources", "searchSource", "sourceChars",
    "responsesAllowedDomains", "responsesAllowedXHandles", "responsesExcludedDomains",
    "responsesExcludedXHandles", "responsesMaxTurns", "responsesOpenRouterEngine",
    "responsesReasoningEffort", "xImageUnderstanding", "xVideoUnderstanding",
}


def cleanup(*, execute: bool = False, expected_sha256: str | None = None) -> dict:
    config = files.xdg_home("XDG_CONFIG_HOME", Path.home() / ".config")
    directories = [config / "grok-search", config / "tavily-hikari-cli",
                   Path.home() / ".local/lib/pennix-clients/tavily-hikari"]
    path = directories[0] / "config.json"
    with files.locked():
        if apply_journal.read() is not None:
            raise files.OwnershipError("先完成已有 Lifecycle 事务")
        for directory in directories:
            files.safe_path(directory)
            if directory.exists():
                allowed = {path} if directory == path.parent else set()
                if not directory.is_dir() or set(directory.iterdir()) - allowed:
                    raise files.OwnershipError("旧 owner 尚未退出或含未知文件；保留现场")
        raw = files.read(path, private=True)
        digest = hashlib.sha256(raw).hexdigest() if raw is not None else None
        if raw is not None:
            value = json.loads(raw, object_pairs_hook=files.unique_pairs)
            if not isinstance(value, dict) or set(value) - PREFERENCES:
                raise files.OwnershipError("旧搜索配置含凭据或未知字段；先由原 owner 撤回")
        if execute:
            if raw is not None and expected_sha256 != digest:
                raise files.OwnershipError("必须提供当前预览的精确配置摘要")
            if raw is not None:
                files.replace(path, None, raw)
            for directory in directories:
                if directory.exists():
                    directory.rmdir()
        return {"state": "cleaned" if execute else "preview", "config_sha256": digest,
                "directories": [str(directory) for directory in directories],
                "config_present": raw is not None}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--config-sha256")
    args = parser.parse_args()
    try:
        print(json.dumps(cleanup(execute=args.yes, expected_sha256=args.config_sha256)))
        return 0
    except (OSError, ValueError, files.OwnershipError, apply_journal.JournalError) as error:
        print(f"retire web search: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    raise SystemExit(main())
