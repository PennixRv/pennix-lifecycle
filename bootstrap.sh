#!/usr/bin/env bash
set -euo pipefail

version="0.1.5"
if [[ -n "${BASH_SOURCE[0]-}" && -f "${BASH_SOURCE[0]}" ]]; then
  source_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
else
  source_dir="${PWD}"
fi
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

if [[ "${EUID}" -eq 0 ]]; then
  echo "bootstrap: run as the target user, not root" >&2
  exit 2
fi

if [[ -r /etc/os-release ]]; then
  . /etc/os-release
fi
if [[ "${ID:-}" != "arch" ]]; then
  echo "bootstrap: Arch Linux is required" >&2
  exit 2
fi

for command in sudo pacman; do
  command -v "${command}" >/dev/null || { echo "bootstrap: ${command} is required" >&2; exit 2; }
done
sudo pacman -S --needed --noconfirm curl tar python python-kconfiglib python-tomlkit npm git base-devel
for command in curl tar; do
  command -v "${command}" >/dev/null || { echo "bootstrap: ${command} was not installed" >&2; exit 2; }
done

if ! command -v yay >/dev/null 2>&1 && ! command -v paru >/dev/null 2>&1; then
  build_root="${XDG_CACHE_HOME:-$HOME/.cache}/pennix-lifecycle/bootstrap/yay"
  mkdir -p -- "$(dirname -- "${build_root}")"
  if [[ -e "${build_root}" ]]; then
    echo "bootstrap: refusing to reuse an existing yay build directory" >&2
    exit 2
  fi
  git clone https://aur.archlinux.org/yay.git "${build_root}"
  (cd -- "${build_root}" && makepkg -si --noconfirm)
  rm -rf -- "${build_root}"
fi

if [[ ! -f "${source_dir}/config/Kconfig" ]]; then
  archive_root="$(mktemp -d)"
  trap 'rm -rf -- "${archive_root}"' EXIT
  curl -fsSL "https://github.com/PennixRv/pennix-lifecycle/archive/refs/tags/v${version}.tar.gz" | tar -xz -C "${archive_root}"
  source_dir="$(find "${archive_root}" -mindepth 1 -maxdepth 1 -type d -print -quit)"
fi

if [[ ! -f "${source_dir}/config/Kconfig" || ! -x "${source_dir}/pennix" ]]; then
  echo "bootstrap: lifecycle release is incomplete" >&2
  exit 2
fi

mkdir -p -- "${install_root}" "${bin_dir}"
find "${source_dir}" -mindepth 1 -maxdepth 1 ! -name .git -exec cp -a -- {} "${install_root}/" \;
ln -sfn -- "${install_root}/pennix" "${bin_dir}/pennix"
echo "installed ${version} to ${install_root}"
