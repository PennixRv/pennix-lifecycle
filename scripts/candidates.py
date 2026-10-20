"""Resolve verified, immutable delivery candidates before owner operations."""
from __future__ import annotations

import base64
import hashlib
import json
import platform
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile
import urllib.parse
import urllib.request

import owned_files


class CandidateError(ValueError):
    pass


SHA = re.compile(r"^[0-9a-f]{40}$")


def _json(url: str) -> dict:
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "pennix-lifecycle"})
    with urllib.request.urlopen(request, timeout=30) as response:
        value = json.load(response)
    if not isinstance(value, dict):
        raise CandidateError("candidate metadata is invalid")
    return value


def _run(command: list[str], *, timeout: int = 60) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CandidateError(f"candidate command failed: {command[0]}") from error


def _digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def _check_target(candidate: dict, target: dict | None) -> dict:
    if target:
        for field in ("version", "ref", "sha256"):
            actual = candidate.get(field)
            if field == "sha256" and actual is None:
                actual = candidate.get("archive_sha256")
            if field in target and actual != target[field]:
                raise CandidateError(f"locked candidate {field} changed")
    return candidate


def resolve_npm(component: dict, workspace: Path, target: dict | None = None) -> dict:
    package = component["package"]
    name = package["name"]
    registry = package.get("registry", "https://registry.npmjs.org/").rstrip("/")
    encoded = urllib.parse.quote(name, safe="")
    selector = urllib.parse.quote(target["version"] if target else package.get("tag", "latest"), safe="")
    metadata = _json(f"{registry}/{encoded}/{selector}")
    dist = metadata.get("dist")
    if (metadata.get("name") != name or not isinstance(metadata.get("version"), str)
            or target and metadata["version"] != target.get("version") or not isinstance(dist, dict)
            or not isinstance(dist.get("tarball"), str) or not isinstance(dist.get("integrity"), str)):
        raise CandidateError(f"npm candidate metadata is incomplete: {name}")
    match = re.search(r"(?:^|\s)sha512-([A-Za-z0-9+/=]+)(?:\s|$)", dist["integrity"])
    if not match:
        raise CandidateError(f"npm candidate has no SHA-512 integrity: {name}")
    artifact = workspace / "package.tgz"
    owned_files.download(dist["tarball"], artifact)
    raw = artifact.read_bytes()
    sha256 = hashlib.sha256(raw).hexdigest()
    if base64.b64encode(hashlib.sha512(raw).digest()).decode() != match[1]:
        raise CandidateError(f"npm candidate integrity mismatch: {name}")
    return _check_target({"source": component["lock_source"], "version": metadata["version"],
                          "sha256": sha256, "path": str(artifact),
                          "impact": f"npm package {name}@{metadata['version']}"}, target)


def resolve_pypi(component: dict, workspace: Path, target: dict | None = None) -> dict:
    name = component["package"]["name"]
    metadata = _json(f"https://pypi.org/pypi/{urllib.parse.quote(name, safe='')}/json")
    version = target.get("version") if target else metadata.get("info", {}).get("version")
    files = metadata.get("releases", {}).get(version, []) if target else metadata.get("urls", [])
    wheels = [item for item in files if item.get("packagetype") == "bdist_wheel"]
    selected = next((item for item in wheels if item.get("filename", "").endswith("py3-none-any.whl")), None)
    if selected is None and len(wheels) == 1:
        selected = wheels[0]
    if (not isinstance(version, str) or not isinstance(selected, dict)
            or not isinstance(selected.get("url"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", selected.get("digests", {}).get("sha256", ""))):
        raise CandidateError(f"PyPI has no fixed compatible wheel for {name} {version}")
    artifact = workspace / selected["filename"]
    owned_files.download(selected["url"], artifact)
    sha256 = _digest(artifact)
    if sha256 != selected["digests"]["sha256"]:
        raise CandidateError(f"PyPI candidate digest mismatch: {name}")
    return _check_target({"source": component["lock_source"], "version": version, "sha256": sha256,
                          "path": str(artifact), "impact": f"uv tool {name}=={version}"}, target)


def _gh_release(repo: str, tag: str | None) -> dict:
    command = ["gh", "release", "view"]
    if tag:
        command.append(tag)
    command += ["--repo", repo, "--json", "tagName,assets"]
    result = _run(command)
    try:
        value = json.loads(result.stdout)
    except (ValueError, TypeError) as error:
        raise CandidateError(f"GitHub Release metadata unavailable: {repo}") from error
    if result.returncode or not isinstance(value, dict) or not isinstance(value.get("tagName"), str):
        raise CandidateError(f"GitHub Release metadata unavailable: {repo}; verify gh authentication")
    return value


def _archive_json(archive: Path, suffix: str) -> dict:
    try:
        with tarfile.open(archive, "r:*") as source:
            matches = [item for item in source.getmembers()
                       if item.isfile() and (PurePosixPath(item.name).as_posix() == suffix
                                             or PurePosixPath(item.name).as_posix().endswith("/" + suffix))]
            if len(matches) != 1 or matches[0].size > 1024 * 1024:
                raise CandidateError(f"archive identity file is missing or ambiguous: {suffix}")
            stream = source.extractfile(matches[0])
            value = json.load(stream)
    except (OSError, tarfile.TarError, ValueError, TypeError) as error:
        if isinstance(error, CandidateError):
            raise
        raise CandidateError(f"invalid source archive: {archive.name}") from error
    if not isinstance(value, dict):
        raise CandidateError(f"archive identity is invalid: {suffix}")
    return value


def resolve_release(component: dict, workspace: Path, target: dict | None = None) -> dict:
    asset_spec = component["package"]["asset"]
    repo = asset_spec["repo"]
    release = _gh_release(repo, target.get("ref") if target else None)
    assets = [item for item in release.get("assets", [])
              if item.get("name", "").startswith(asset_spec["name_prefix"])
              and item.get("name", "").endswith(asset_spec["suffix"])]
    if len(assets) != 1:
        raise CandidateError(f"expected one npm asset in {repo}@{release['tagName']}")
    asset = assets[0]
    path = workspace / asset["name"]
    result = _run(["gh", "release", "download", release["tagName"], "--repo", repo,
                   "--pattern", asset["name"], "--output", str(path)], timeout=120)
    if result.returncode or not path.is_file():
        raise CandidateError(f"cannot stage private release asset: {repo}@{release['tagName']}")
    sha256 = _digest(path)
    supplied = asset.get("digest")
    if supplied and supplied != f"sha256:{sha256}":
        raise CandidateError("GitHub Release asset digest mismatch")
    package = _archive_json(path, "package/package.json")
    name = component["package"]["name"]
    version = package.get("version")
    if package.get("name") != name or not isinstance(version, str):
        raise CandidateError("private npm asset package identity mismatch")
    return _check_target({"source": component["lock_source"], "version": version,
                          "ref": release["tagName"], "sha256": sha256, "path": str(path),
                          "impact": f"private npm asset {asset['name']} ({version})"}, target)


def _git_head(repo: str, *, aur: bool = False) -> str:
    url = f"https://aur.archlinux.org/{repo}.git" if aur else f"https://github.com/{repo}.git"
    result = _run(["git", "ls-remote", url, "HEAD"], timeout=45)
    head = result.stdout.split()
    if result.returncode or not head or not SHA.fullmatch(head[0]):
        raise CandidateError(f"cannot resolve default branch HEAD: {repo}")
    return head[0]


def _download_archive(repo: str, ref: str, workspace: Path, name: str) -> tuple[Path, str]:
    if not SHA.fullmatch(ref):
        raise CandidateError("Git candidate ref must be a complete commit")
    path = workspace / name
    owned_files.download(f"https://codeload.github.com/{repo}/tar.gz/{ref}", path)
    return path, _digest(path)


def _checkout_aur(name: str, ref: str, workspace: Path) -> Path:
    checkout = workspace / "aur-source"
    url = f"https://aur.archlinux.org/{name}.git"
    result = _run(["git", "clone", "--filter=blob:none", "--no-checkout", url, str(checkout)], timeout=120)
    if result.returncode:
        raise CandidateError(f"cannot stage exact AUR source: {name}")
    result = _run(["git", "-C", str(checkout), "checkout", "--detach", ref], timeout=60)
    head = _run(["git", "-C", str(checkout), "rev-parse", "HEAD"])
    if result.returncode or head.returncode or head.stdout.strip() != ref:
        raise CandidateError(f"AUR source commit mismatch: {name}")
    return checkout


def resolve_aur(component: dict, workspace: Path, target: dict | None = None) -> dict:
    name = component["package"]["name"]
    if target:
        version = target.get("version")
        ref = target.get("ref")
    else:
        info = _json(f"https://aur.archlinux.org/rpc/v5/info/{urllib.parse.quote(name, safe='')}")
        results = info.get("results")
        if not isinstance(results, list) or len(results) != 1 or results[0].get("Name") != name:
            raise CandidateError(f"AUR metadata unavailable: {name}")
        version = results[0].get("Version")
        ref = _git_head(name, aur=True)
    if not isinstance(version, str) or not isinstance(ref, str) or not SHA.fullmatch(ref):
        raise CandidateError(f"AUR candidate identity is incomplete: {name}")
    checkout = _checkout_aur(name, ref, workspace)
    srcinfo = (checkout / ".SRCINFO").read_text(encoding="utf-8")
    fields = {}
    package_names = set()
    for line in srcinfo.splitlines():
        if "=" not in line:
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        if key == "pkgname":
            package_names.add(value)
        elif key in {"pkgver", "pkgrel", "epoch"} and key not in fields:
            fields[key] = value
    resolved_version = f"{fields.get('epoch') + ':' if fields.get('epoch') not in {None, '0'} else ''}{fields.get('pkgver', '')}-{fields.get('pkgrel', '')}"
    if name not in package_names or resolved_version != version:
        raise CandidateError(f"AUR RPC and fixed .SRCINFO differ: {name}")
    candidate = {"source": component["lock_source"], "version": version, "ref": ref,
                 "path": str(checkout), "impact": f"AUR source {name} {version} at {ref}; makepkg runs after confirmation"}
    return _check_target(candidate, target)


def _arch_pkginfo(path: Path) -> tuple[dict, list[str]]:
    result = _run(["bsdtar", "-xOf", str(path), ".PKGINFO"])
    if result.returncode:
        raise CandidateError("cannot inspect official package metadata")
    fields = {}
    dependencies = []
    for line in result.stdout.splitlines():
        if " = " not in line:
            continue
        key, value = line.split(" = ", 1)
        if key == "depend":
            dependencies.append(value)
        else:
            fields.setdefault(key, value)
    return fields, dependencies


def _verify_arch_package(component: dict, workspace: Path, filename: str, version: str,
                         url: str, target: dict | None) -> dict:
    path = workspace / filename
    signature = workspace / f"{filename}.sig"
    owned_files.download(url, path)
    owned_files.download(url + ".sig", signature)
    verified = _run(["pacman-key", "--verify", str(signature), str(path)], timeout=60)
    if verified.returncode:
        raise CandidateError("official package signature verification failed")
    sha256 = _digest(path)
    info, dependencies = _arch_pkginfo(path)
    name = component["package"]["name"]
    if info.get("pkgname") != name or f"{info.get('pkgver')}-{info.get('pkgrel')}" != version:
        raise CandidateError("official package metadata identity mismatch")
    missing = []
    for dependency in dependencies:
        result = _run(["pacman", "-T", dependency], timeout=30)
        if result.returncode == 1:
            missing.append(dependency)
        elif result.returncode != 0:
            raise CandidateError("cannot safely check official package dependencies")
    if missing:
        raise CandidateError(f"official package requires uninstalled dependencies: {', '.join(missing)}")
    preview = _run(["pacman", "-U", "--print", str(path)], timeout=30)
    if preview.returncode or not preview.stdout.strip():
        raise CandidateError("pacman cannot preview the exact local package")
    candidate = {"source": component["lock_source"], "version": version, "sha256": sha256,
                 "path": str(path), "impact": f"pacman installs only {name} {version} from a signed local package"}
    return _check_target(candidate, target)


def resolve_arch(component: dict, workspace: Path, target: dict | None = None) -> dict:
    if platform.machine() != "x86_64":
        raise CandidateError("Arch candidate resolver currently supports x86_64 only")
    name = component["package"]["name"]
    repo = component["lock_source"].split(":", 1)[1].split("/", 1)[0]
    if target:
        version = target.get("version", "")
        plain = version.split(":", 1)[-1]
        filename = f"{name}-{plain}-x86_64.pkg.tar.zst"
        url = f"https://archive.archlinux.org/packages/{name[0]}/{name}/{filename}"
        return _verify_arch_package(component, workspace, filename, version, url, target)
    metadata = _json(f"https://archlinux.org/packages/{repo}/x86_64/{name}/json/")
    pkgver, pkgrel, filename = metadata.get("pkgver"), metadata.get("pkgrel"), metadata.get("filename")
    if not all(isinstance(value, str) and value for value in (pkgver, pkgrel, filename)):
        raise CandidateError(f"official package metadata unavailable: {name}")
    version = f"{pkgver}-{pkgrel}"
    url = f"https://geo.mirror.pkgbuild.com/{repo}/os/x86_64/{filename}"
    return _verify_arch_package(component, workspace, filename, version, url, target)


def resolve_plugin(component: dict, workspace: Path, target: dict | None = None) -> dict:
    plugin = component["plugin"]
    repo = urllib.parse.urlparse(plugin["marketplace"]["source"]).path.strip("/").removesuffix(".git")
    ref = target.get("ref") if target else _git_head(repo)
    archive, sha256 = _download_archive(repo, ref, workspace, "plugin.tar.gz")
    checkout = owned_files.extract(archive, workspace / "plugin-source")
    manifest = json.loads((checkout / ".codex-plugin/plugin.json").read_bytes())
    name, marketplace = plugin["id"].split("@", 1)
    version = manifest.get("version")
    if manifest.get("name") != name or not isinstance(version, str):
        raise CandidateError("Codex plugin manifest identity mismatch")
    candidate = {"source": component["lock_source"], "version": version, "ref": ref,
                 "sha256": sha256, "path": str(archive), "checkout": str(checkout),
                 "impact": f"Codex plugin {name}@{marketplace} {version} at {ref}"}
    return _check_target(candidate, target)


def resolve_client(component: dict, workspace: Path, target: dict | None = None) -> dict:
    source = component["client"]["source"]
    repo = source["repo"]
    ref = target.get("ref") if target else _git_head(repo)
    if source.get("archive"):
        archive, sha256 = _download_archive(repo, ref, workspace, "client.tar.gz")
        checkout = owned_files.extract(archive, workspace / "client-source")
        path = archive
        version_path = next((checkout / item for item in ("VERSION", "version" ) if (checkout / item).is_file()), None)
        version = version_path.read_text(encoding="utf-8").strip() if version_path else None
    else:
        if not SHA.fullmatch(ref):
            raise CandidateError("client ref is not a complete commit")
        url = f"https://raw.githubusercontent.com/{repo}/{ref}/{source['path']}"
        path = workspace / Path(source["path"]).name
        owned_files.download(url, path)
        sha256 = _digest(path)
        checkout = None
        version = None
    candidate = {"source": component["lock_source"], "repo": repo, "ref": ref, "sha256": sha256,
                 "path": str(path), "impact": f"Git client {repo} at {ref}"}
    if version:
        candidate["version"] = version
        candidate["impact"] += f" (version {version})"
    if checkout:
        candidate["checkout"] = str(checkout)
    return _check_target(candidate, target)


def resolve_collection(component: dict, workspace: Path, target: dict | None = None) -> dict:
    source = component["collection_contract"]["source"]
    ref = target.get("ref") if target else _git_head(source["repo"])
    archive, sha256 = _download_archive(source["repo"], ref, workspace, "collection.tar.gz")
    checkout = owned_files.extract(archive, workspace / "collection-source")
    manifest = json.loads((checkout / source["manifest"]).read_bytes())
    expected = component["collection_contract"]["directory"]
    if manifest.get("schema") != 1 or manifest.get("name") != expected:
        raise CandidateError("Skill collection manifest identity mismatch")
    candidate = {"source": component["lock_source"], "repo": source["repo"], "ref": ref, "sha256": sha256,
                 "path": str(archive), "checkout": str(checkout),
                 "impact": f"Skill collection {source['repo']} at {ref}"}
    return _check_target(candidate, target)


def _resolve_aur(component: dict, workspace: Path, target: dict | None = None) -> dict:
    return resolve_aur(component, workspace, target)


RESOLVERS = {
    "codex-cli": _resolve_aur,
    "trellis-cli": resolve_npm,
    "pnpm": resolve_npm,
    "context-mode": resolve_npm,
    "codegraph-cli": resolve_npm,
    "paseo-cli": resolve_npm,
    "ponytail-plugin": resolve_plugin,
    "cch-status": resolve_release,
    "tmux": resolve_arch,
    "aoe": _resolve_aur,
    "tavily-hikari": resolve_client,
    "uv": resolve_arch,
    "tavily-cli": resolve_pypi,
    "peon-ping": resolve_client,
    "pennix-skills": resolve_collection,
    "pennix-skills-extra": resolve_collection,
}


def validate_catalog(catalog: dict) -> None:
    dynamic = {key for key, component in catalog["components"].items()
               if component.get("delivery") in {"package", "plugin", "client", "collection"}}
    if dynamic != set(RESOLVERS):
        raise CandidateError(f"candidate resolver coverage differs: missing={sorted(dynamic-set(RESOLVERS))}, extra={sorted(set(RESOLVERS)-dynamic)}")


def resolve(component: dict, workspace: Path, target: dict | None = None) -> dict:
    key = component.get("id")
    resolver = RESOLVERS.get(key)
    if resolver is None:
        raise CandidateError(f"no candidate resolver for component: {key}")
    return resolver(component, workspace, target)


def lock_entry(candidate: dict) -> dict:
    entry = {key: candidate[key] for key in ("source", "version", "ref", "sha256") if key in candidate}
    if "sha256" not in entry and "archive_sha256" in candidate:
        entry["sha256"] = candidate["archive_sha256"]
    return entry
