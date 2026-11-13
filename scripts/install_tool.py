#!/usr/bin/env python3
"""Bootstrap entry; must not import the CLI or its not-yet-installed dependencies."""
import sys
from pathlib import Path

sys.dont_write_bytecode = True
import release_owner

if __name__ == "__main__":
    try:
        print(release_owner.install(Path(__file__).resolve().parents[1]))
    except (OSError, ValueError, release_owner.ReleaseError) as error:
        print(f"pennix install: {error}", file=sys.stderr)
        raise SystemExit(2)
