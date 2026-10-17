#!/usr/bin/env bash
set -euo pipefail

version="0.1.7"
release_name="pennix-lifecycle-${version}.tar.gz"
release_url="https://github.com/PennixRv/pennix-lifecycle/releases/download/v${version}/${release_name}"
release_sha256="d2681a98ceeb6d4376fc0b2436cbd490ea880cef7aa8460ed48323f4da4b2309"
data_root="${XDG_DATA_HOME:-$HOME/.local/share}/pennix-lifecycle"
release_root="${data_root}/releases/${version}"
owner_marker="${data_root}/.pennix-lifecycle-owner"
bin_dir="${HOME}/.local/bin"

usage() {
  cat <<'EOF'
用法：
  bootstrap.sh install      安装固定版本的 pennix-lifecycle
  bootstrap.sh update       更新到固定版本
  bootstrap.sh --uninstall  移除 pennix-lifecycle 的活动版本和入口
EOF
}

action="${1:-install}"
if [[ "${action}" == "--help" || "${action}" == "-h" ]]; then
  usage
  exit 0
fi
if [[ "${action}" != "install" && "${action}" != "update" && "${action}" != "--uninstall" ]]; then
  usage >&2
  exit 2
fi
[[ "${data_root}" == /* && "${bin_dir}" == /* ]] || {
  echo "bootstrap: install paths must be absolute" >&2
  exit 2
}
if [[ "${EUID}" -eq 0 ]]; then
  echo "bootstrap: run as the target user, not root" >&2
  exit 2
fi

if [[ "${action}" == "--uninstall" ]]; then
  if [[ ! -e "${data_root}" ]]; then
    exit 0
  fi
  [[ -d "${data_root}" && ! -L "${data_root}" && -f "${owner_marker}" ]] || {
    echo "bootstrap: refusing to remove an unowned or unsafe data root" >&2
    exit 2
  }
  [[ "$(<"${owner_marker}")" == "pennix-lifecycle-owner=1" ]] || {
    echo "bootstrap: owner marker is invalid" >&2
    exit 2
  }
  if [[ -e "${bin_dir}" || -L "${bin_dir}" ]]; then
    [[ -d "${bin_dir}" && ! -L "${bin_dir}" ]] || {
      echo "bootstrap: bin directory is unsafe" >&2
      exit 2
    }
  fi
  if [[ -L "${bin_dir}/pennix" ]]; then
    link_target="$(readlink -- "${bin_dir}/pennix")"
    [[ "${link_target}" == "${data_root}/"* ]] && rm -f -- "${bin_dir}/pennix"
  elif [[ -e "${bin_dir}/pennix" ]]; then
    echo "bootstrap: refusing to remove a non-symlink pennix entry" >&2
    exit 2
  fi
  rm -rf -- "${data_root}"
  exit 0
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
for command in curl tar sha256sum; do
  command -v "${command}" >/dev/null || { echo "bootstrap: ${command} was not installed" >&2; exit 2; }
done

source_dir=""
if [[ -n "${BASH_SOURCE[0]-}" && -f "${BASH_SOURCE[0]}" ]]; then
  candidate="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
  if [[ -f "${candidate}/config/Kconfig" && -x "${candidate}/pennix" ]]; then
    source_dir="${candidate}"
  fi
fi

archive_root=""
staging_release=""
temporary_link=""
cleanup() {
  [[ -z "${archive_root}" ]] || rm -rf -- "${archive_root}"
  [[ -z "${staging_release}" ]] || rm -rf -- "${staging_release}"
  [[ -z "${temporary_link}" ]] || rm -f -- "${temporary_link}"
}
trap cleanup EXIT
if [[ -z "${source_dir}" ]]; then
  if [[ "${release_sha256}" == __RELEASE_SHA256__ ]]; then
    echo "bootstrap: release checksum is not published in this bootstrap" >&2
    exit 2
  fi
  archive_root="$(mktemp -d)"
  archive="${archive_root}/${release_name}"
  curl -fsSL --proto '=https' --tlsv1.2 "${release_url}" -o "${archive}"
  printf '%s  %s\n' "${release_sha256}" "${archive}" | sha256sum -c - >/dev/null
  python3 - "${archive}" <<'PY'
import sys
import tarfile

archive = sys.argv[1]
with tarfile.open(archive, "r:gz") as handle:
    members = handle.getmembers()
    if not members:
        raise SystemExit("empty archive")
    for member in members:
        parts = member.name.split("/")
        if member.name.startswith("/") or ".." in parts or parts[0] != "pennix-lifecycle":
            raise SystemExit(f"unsafe archive path: {member.name}")
        if not (member.isdir() or member.isfile()):
            raise SystemExit(f"unsafe archive member type: {member.name}")
PY
  extracted="${archive_root}/extracted"
  mkdir -p -- "${extracted}"
  tar --no-same-owner --no-same-permissions -xzf "${archive}" -C "${extracted}"
  source_dir="${extracted}/pennix-lifecycle"
fi

for required in config/Kconfig pennix scripts/pennix.py references/catalog.json; do
  [[ -f "${source_dir}/${required}" ]] || { echo "bootstrap: release is incomplete: ${required}" >&2; exit 2; }
done
[[ -x "${source_dir}/pennix" ]] || { echo "bootstrap: pennix is not executable" >&2; exit 2; }

[[ ! -L "${data_root}" && ( ! -e "${data_root}" || -d "${data_root}" ) ]] || {
  echo "bootstrap: data root is unsafe" >&2
  exit 2
}
[[ ! -L "${bin_dir}" && ( ! -e "${bin_dir}" || -d "${bin_dir}" ) ]] || {
  echo "bootstrap: bin directory is unsafe" >&2
  exit 2
}
if [[ -e "${data_root}" ]]; then
  [[ -f "${owner_marker}" && ! -L "${owner_marker}" ]] || {
    echo "bootstrap: refusing to adopt an unowned data root" >&2
    exit 2
  }
  [[ "$(<"${owner_marker}")" == "pennix-lifecycle-owner=1" ]] || {
    echo "bootstrap: owner marker is invalid" >&2
    exit 2
  }
fi
if [[ -e "${bin_dir}/pennix" || -L "${bin_dir}/pennix" ]]; then
  [[ -L "${bin_dir}/pennix" ]] || {
    echo "bootstrap: refusing to replace a non-symlink pennix entry" >&2
    exit 2
  }
  link_target="$(readlink -- "${bin_dir}/pennix")"
  [[ "${link_target}" == "${data_root}/"* ]] || {
    echo "bootstrap: refusing to replace an externally owned pennix link" >&2
    exit 2
  }
fi
mkdir -p -- "${data_root}/releases" "${bin_dir}"
[[ ! -L "${data_root}" && -d "${data_root}" && ! -L "${bin_dir}" && -d "${bin_dir}" ]] || {
  echo "bootstrap: install directory is unsafe" >&2
  exit 2
}
if [[ -e "${release_root}" || -L "${release_root}" ]]; then
  [[ -d "${release_root}" && ! -L "${release_root}" ]] || {
    echo "bootstrap: active release path is unsafe" >&2
    exit 2
  }
  [[ -f "${release_root}/pennix" && -f "${release_root}/references/catalog.json" ]] || {
    echo "bootstrap: active release is incomplete; use a new release version" >&2
    exit 2
  }
else
  staging_release="$(mktemp -d "${data_root}/.release-${version}.XXXXXX")"
  find "${source_dir}" -mindepth 1 -maxdepth 1 ! -name .git -exec cp -a -- {} "${staging_release}/" \;
  [[ -f "${staging_release}/pennix" && -f "${staging_release}/references/catalog.json" ]] || {
    echo "bootstrap: staged release is incomplete" >&2
    rm -rf -- "${staging_release}"
    exit 2
  }
  mv -- "${staging_release}" "${release_root}"
  staging_release=""
fi
temporary_link="${bin_dir}/.pennix-link-${BASHPID}"
rm -f -- "${temporary_link}"
ln -s -- "${release_root}/pennix" "${temporary_link}"
mv -Tf -- "${temporary_link}" "${bin_dir}/pennix"
temporary_link=""
if [[ -e "${owner_marker}" || -L "${owner_marker}" ]]; then
  [[ -f "${owner_marker}" && ! -L "${owner_marker}" ]] || {
    echo "bootstrap: owner marker is unsafe" >&2
    exit 2
  }
  [[ "$(<"${owner_marker}")" == "pennix-lifecycle-owner=1" ]] || {
    echo "bootstrap: owner marker is invalid" >&2
    exit 2
  }
fi
printf '%s\n' 'pennix-lifecycle-owner=1' > "${owner_marker}"
chmod 600 -- "${owner_marker}"
find "${data_root}/releases" -mindepth 1 -maxdepth 1 -type d ! -name "${version}" -exec rm -rf -- {} +
echo "installed ${version} to ${release_root}"
