#!/usr/bin/env bash
set -euo pipefail

version="0.1.0"
source_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
install_root="${XDG_DATA_HOME:-$HOME/.local/share}/pennix-lifecycle/releases/${version}"
bin_dir="${HOME}/.local/bin"

if [[ "${1:-}" == "--uninstall" ]]; then
  rm -f -- "${bin_dir}/pennix"
  rm -rf -- "${XDG_DATA_HOME:-$HOME/.local/share}/pennix-lifecycle/releases/${version}"
  exit 0
fi

if [[ "${source_dir}" == "${install_root}"* ]]; then
  echo "bootstrap: source is inside its target release" >&2
  exit 2
fi

mkdir -p -- "${install_root}" "${bin_dir}"
find "${source_dir}" -mindepth 1 -maxdepth 1 ! -name .git -exec cp -a -- {} "${install_root}/" \;
ln -sfn -- "${install_root}/pennix" "${bin_dir}/pennix"
echo "installed ${version} to ${install_root}"
