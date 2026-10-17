#!/usr/bin/env bash
set -euo pipefail

if [[ "$#" -ne 3 ]]; then
  echo "usage: build-release.sh VERSION GIT-REF OUTPUT.tar.gz" >&2
  exit 2
fi

version="$1"
ref="$2"
output="$3"
[[ "${version}" =~ ^[0-9]+\.[0-9]+\.[0-9]+([.-][A-Za-z0-9.-]+)?$ ]] || {
  echo "release version is invalid" >&2
  exit 2
}
[[ "${output}" == /* ]] || {
  echo "release output path must be absolute" >&2
  exit 2
}

repository="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
commit="$(git -C "${repository}" rev-parse --verify --quiet --end-of-options "${ref}^{commit}")" || {
  echo "release source ref is missing: ${ref}" >&2
  exit 2
}
[[ ! -e "${output}" && ! -L "${output}" ]] || {
  echo "release output already exists" >&2
  exit 2
}
mkdir -p -- "$(dirname -- "${output}")"

git -C "${repository}" ls-tree -r "${commit}" -- config pennix scripts references templates |
  awk '$1 != "100644" && $1 != "100755" { invalid = 1 } END { exit invalid }' || {
    echo "release source contains a non-regular file" >&2
    exit 2
  }

temporary="$(mktemp "$(dirname -- "${output}")/.pennix-release.XXXXXX")"
raw_archive="$(mktemp "$(dirname -- "${output}")/.pennix-release-raw.XXXXXX")"
cleanup() {
  [[ -z "${temporary}" ]] || rm -f -- "${temporary}"
  [[ -z "${raw_archive}" ]] || rm -f -- "${raw_archive}"
}
trap cleanup EXIT
git -C "${repository}" archive --format=tar --mtime='@0' --prefix=pennix-lifecycle/ "${commit}" \
  config pennix scripts references templates > "${raw_archive}"
python3 - "${raw_archive}" "${temporary}" <<'PY'
import gzip
import sys
import tarfile

source_path, destination_path = sys.argv[1:]
with tarfile.open(source_path, "r:") as source, open(destination_path, "wb") as output:
    with gzip.GzipFile(filename="", fileobj=output, mode="wb", mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as destination:
            for member in source.getmembers():
                if member.issym() or member.islnk() or member.isdev():
                    raise SystemExit("release archive contains an unsafe member")
                member.mtime = 0
                member.uid = 0
                member.gid = 0
                member.uname = ""
                member.gname = ""
                member.pax_headers = {}
                content = source.extractfile(member) if member.isfile() else None
                destination.addfile(member, content)
PY
raw_archive=""
python3 - "${temporary}" <<'PY'
import sys
import tarfile

with tarfile.open(sys.argv[1], "r:gz") as archive:
    names = {member.name for member in archive.getmembers()}
    required = {
        "pennix-lifecycle/config/Kconfig",
        "pennix-lifecycle/pennix",
        "pennix-lifecycle/scripts/pennix.py",
        "pennix-lifecycle/references/catalog.json",
    }
    if not required <= names:
        raise SystemExit("release archive is incomplete")
    if any(member.issym() or member.islnk() or member.isdev() for member in archive.getmembers()):
        raise SystemExit("release archive contains an unsafe member")
    if "pennix-lifecycle/bootstrap.sh" in names:
        raise SystemExit("bootstrap must stay outside the checksummed archive")
PY
mv -- "${temporary}" "${output}"
temporary=""
sha256sum -- "${output}"
