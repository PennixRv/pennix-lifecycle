#!/usr/bin/env bash
set -euo pipefail

repository="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"

bash "${repository}/bootstrap.sh" --help >/dev/null
if bash "${repository}/bootstrap.sh" --release invalid >/dev/null 2>&1; then
  echo "bootstrap accepted an invalid release selector" >&2
  exit 1
fi

if [[ "${EUID}" -ne 0 ]]; then
  current_user="$(id -un)"
  if bash "${repository}/bootstrap.sh" --user nobody --release v0.1.8 >/dev/null 2>&1; then
    echo "bootstrap allowed an unprivileged caller to target another user" >&2
    exit 1
  fi
  if [[ "${current_user}" == nobody ]]; then
    echo "bootstrap isolation test requires a user other than nobody" >&2
    exit 77
  fi
fi

echo "bootstrap argument and target isolation checks passed"
