#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -eq 0 ]]; then
  echo "bootstrap tests require an unprivileged user" >&2
  exit 77
fi

repository="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
sandbox="$(mktemp -d)"
trap 'rm -rf -- "${sandbox}"' EXIT
source="${sandbox}/source"
mkdir -p "${source}/config" "${source}/scripts" "${source}/references" "${sandbox}/stubs"
cp "${repository}/bootstrap.sh" "${source}/bootstrap.sh"
cp "${repository}/config/Kconfig" "${source}/config/Kconfig"
cp "${repository}/scripts/pennix.py" "${source}/scripts/pennix.py"
cp "${repository}/references/catalog.json" "${source}/references/catalog.json"
printf '#!/bin/sh\nexit 0\n' > "${source}/pennix"
printf '#!/bin/sh\nexit 0\n' > "${sandbox}/stubs/sudo"
printf '#!/bin/sh\nexit 0\n' > "${sandbox}/stubs/pacman"
chmod +x "${source}/pennix" "${sandbox}/stubs/sudo" "${sandbox}/stubs/pacman"
export PATH="${sandbox}/stubs:${PATH}"

run_bootstrap() {
  local home="$1" data_home="$2"
  shift 2
  HOME="${home}" XDG_DATA_HOME="${data_home}" bash "${source}/bootstrap.sh" "$@"
}

mkdir -p "${sandbox}/home/.local/bin"
run_bootstrap "${sandbox}/home" "${sandbox}/data" install
test -L "${sandbox}/home/.local/bin/pennix"
test "$(stat -c %a "${sandbox}/data/pennix-lifecycle/.pennix-lifecycle-owner")" = 600
rm "${sandbox}/data/pennix-lifecycle/releases/0.1.7/pennix"
run_bootstrap "${sandbox}/home" "${sandbox}/data" --uninstall
test ! -e "${sandbox}/data/pennix-lifecycle"
test ! -e "${sandbox}/home/.local/bin/pennix"

mkdir -p "${sandbox}/data/pennix-lifecycle"
printf 'user-data\n' > "${sandbox}/data/pennix-lifecycle/keep"
if run_bootstrap "${sandbox}/home" "${sandbox}/data" install; then
  echo "unowned data root was adopted" >&2
  exit 1
fi
test "$(<"${sandbox}/data/pennix-lifecycle/keep")" = user-data

mkdir -p "${sandbox}/external-home/.local/bin"
ln -s /usr/bin/printf "${sandbox}/external-home/.local/bin/pennix"
if run_bootstrap "${sandbox}/external-home" "${sandbox}/external-data" install; then
  echo "external command link was replaced" >&2
  exit 1
fi
test -L "${sandbox}/external-home/.local/bin/pennix"
test ! -e "${sandbox}/external-data/pennix-lifecycle"

mkdir -p "${sandbox}/file-home/.local/bin"
printf 'user command\n' > "${sandbox}/file-home/.local/bin/pennix"
if run_bootstrap "${sandbox}/file-home" "${sandbox}/file-data" install; then
  echo "regular command file was replaced" >&2
  exit 1
fi
test -f "${sandbox}/file-home/.local/bin/pennix"
test ! -e "${sandbox}/file-data/pennix-lifecycle"

echo "bootstrap isolation matrix passed"
