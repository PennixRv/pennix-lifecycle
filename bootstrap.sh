#!/usr/bin/env bash
set -euo pipefail

repo='PennixRv/pennix-lifecycle'
asset='pennix-lifecycle.tar.gz'
operation='install'
release='latest'
target_user=''
no_input=0

usage() {
  cat <<'EOF'
Usage: bootstrap.sh [install|uninstall] [--release TAG] [--user USER] [--no-input]

install downloads and verifies the current official lifecycle runtime.
uninstall removes only the active lifecycle tool release.
When run as root, --user is required and names the target ordinary user.
EOF
}

run_target() {
  local name
  local -a target_env=(
    "HOME=${target_home}"
    "PATH=${target_home}/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/bin:/bin"
  )
  if [[ "${EUID}" -ne 0 ]]; then
    for name in XDG_CONFIG_HOME XDG_DATA_HOME XDG_STATE_HOME XDG_CACHE_HOME; do
      if [[ -v "${name}" ]]; then target_env+=("${name}=${!name}"); fi
    done
  fi
  runuser -u "${target_user}" -- env "${target_env[@]}" "$@"
}

while (($#)); do
  case "$1" in
    install|uninstall) operation="$1"; shift ;;
    --release) (($# >= 2)) || { usage >&2; exit 2; }; release="$2"; shift 2 ;;
    --user) (($# >= 2)) || { usage >&2; exit 2; }; target_user="$2"; shift 2 ;;
    --no-input) no_input=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done

if [[ "${EUID}" -eq 0 ]]; then
  [[ -n "${target_user}" ]] || { echo 'bootstrap: root requires --user USER' >&2; exit 2; }
else
  current_user="$(id -un)"
  if [[ -n "${target_user}" && "${target_user}" != "${current_user}" ]]; then
    echo 'bootstrap: an unprivileged caller may target only itself' >&2
    exit 2
  fi
  target_user="${current_user}"
fi
[[ "${target_user}" =~ ^[a-z_][a-z0-9_-]*\$?$ ]] || { echo 'bootstrap: invalid target username' >&2; exit 2; }
passwd_entry="$(getent passwd "${target_user}")" || { echo 'bootstrap: target user does not exist' >&2; exit 2; }
IFS=: read -r _ _ target_uid target_gid _ target_home _ <<<"${passwd_entry}"
[[ "${target_uid}" =~ ^[0-9]+$ && "${target_uid}" -ne 0 && "${target_home}" == /* ]] || {
  echo 'bootstrap: target must be an existing non-root user' >&2; exit 2;
}

if [[ "${operation}" == uninstall ]]; then
  installed="${target_home}/.local/bin/pennix"
  [[ -x "${installed}" ]] || { echo 'bootstrap: active pennix installation not found' >&2; exit 2; }
  if [[ "${EUID}" -eq 0 ]]; then
    run_target "${installed}" tool uninstall --yes
  else
    "${installed}" tool uninstall --yes
  fi
  exit 0
fi

if [[ "${release}" != latest && ! "${release}" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo 'bootstrap: release must be latest or a vMAJOR.MINOR.PATCH tag' >&2
  exit 2
fi

if [[ -r /etc/os-release ]]; then . /etc/os-release; fi
[[ "${ID:-}" == arch ]] || { echo 'bootstrap: Arch Linux is required' >&2; exit 2; }
kernel_markers="$(cat /proc/sys/kernel/osrelease /proc/version 2>/dev/null || true)"
if [[ "${kernel_markers,,}" == *microsoft* && "${kernel_markers,,}" != *wsl2* && "${kernel_markers,,}" != *microsoft-standard-wsl2* ]]; then
  echo 'bootstrap: Arch on WSL requires WSL2' >&2
  exit 2
fi

if [[ "${EUID}" -eq 0 ]]; then
  pacman -S --needed --noconfirm curl python python-kconfiglib python-tomlkit python-yaml npm git
else
  command -v sudo >/dev/null || { echo 'bootstrap: sudo is required to install Arch dependencies' >&2; exit 2; }
  if ((no_input)); then
    sudo -n pacman -S --needed --noconfirm curl python python-kconfiglib python-tomlkit python-yaml npm git
  else
    sudo pacman -S --needed --noconfirm curl python python-kconfiglib python-tomlkit python-yaml npm git
  fi
fi

for command in curl sha256sum python3; do
  command -v "${command}" >/dev/null || { echo "bootstrap: required command is missing: ${command}" >&2; exit 2; }
done

if [[ "${release}" == latest ]]; then
  latest_url="$(curl --proto '=https' --tlsv1.2 -fsSIL -o /dev/null -w '%{url_effective}' "https://github.com/${repo}/releases/latest")"
  release_tag="${latest_url##*/}"
else
  release_tag="${release}"
fi
[[ "${release_tag}" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || {
  echo 'bootstrap: latest release did not resolve to a semver tag' >&2; exit 2;
}
release_version="${release_tag#v}"
asset="pennix-lifecycle-${release_version}.tar.gz"
base="https://github.com/${repo}/releases/download/${release_tag}"
tmp="$(mktemp -d "${TMPDIR:-/tmp}/pennix-bootstrap.XXXXXXXX")"
trap 'rm -rf -- "${tmp}"' EXIT

curl --proto '=https' --tlsv1.2 -fsSL "${base}/${asset}" -o "${tmp}/${asset}"
curl --proto '=https' --tlsv1.2 -fsSL "${base}/${asset}.sha256" -o "${tmp}/${asset}.sha256"
IFS=' ' read -r expected filename < "${tmp}/${asset}.sha256"
[[ "${expected}" =~ ^[0-9a-f]{64}$ && "${filename}" == "${asset}" ]] || {
  echo 'bootstrap: invalid release checksum manifest' >&2; exit 2;
}
(cd -- "${tmp}" && sha256sum --check --status "${asset}.sha256") || {
  echo 'bootstrap: release archive checksum mismatch' >&2; exit 2;
}
mkdir -- "${tmp}/unpacked"
python3 - "${tmp}/${asset}" "${tmp}/unpacked" <<'PY'
import shutil
import sys
import tarfile
from pathlib import Path
from pathlib import PurePosixPath

archive, destination = map(Path, sys.argv[1:])
seen, roots, total = set(), set(), 0
with tarfile.open(archive, "r:gz") as source:
    for item in source:
        name = PurePosixPath(item.name)
        if (name.is_absolute() or ".." in name.parts or not name.parts
                or str(name) in seen or not (item.isdir() or item.isfile())):
            raise SystemExit("bootstrap: unsafe archive path, link, special file, or duplicate")
        seen.add(str(name))
        roots.add(name.parts[0])
        total += item.size
        if len(roots) != 1 or len(seen) > 100000 or total > 1024 * 1024 * 1024:
            raise SystemExit("bootstrap: archive layout or expanded size exceeds limits")
        target = destination.joinpath(*name.parts)
        if item.isdir():
            target.mkdir(parents=True, exist_ok=True, mode=0o700)
        else:
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with source.extractfile(item) as incoming, target.open("xb") as outgoing:
                shutil.copyfileobj(incoming, outgoing, 1024 * 1024)
            target.chmod(0o700 if item.mode & 0o111 else 0o600)
if roots != {"pennix-lifecycle"}:
    raise SystemExit("bootstrap: release archive has an unexpected root directory")
PY
source_dir="${tmp}/unpacked/pennix-lifecycle"
[[ -x "${source_dir}/pennix" && -f "${source_dir}/VERSION" ]] || {
  echo 'bootstrap: release archive has an unexpected layout' >&2; exit 2;
}
[[ "$(<"${source_dir}/VERSION")" == "${release_version}" ]] || {
  echo 'bootstrap: release archive version does not match the requested tag' >&2; exit 2;
}

if [[ "${EUID}" -eq 0 ]]; then
  chown -R "${target_uid}:${target_gid}" "${tmp}"
  run_target "${source_dir}/pennix" tool install
else
  "${source_dir}/pennix" tool install
fi
