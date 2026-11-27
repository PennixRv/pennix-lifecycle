#!/usr/bin/python3 -I
"""Fixed privileged entry point. It consumes a reviewed request, not commands or paths."""
import ctypes as c
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from types import ModuleType


LIB = Path("/usr/lib/pennix-lifecycle")
STATE = Path("/var/lib/pennix-lifecycle")
ENV = {"PATH": "/usr/bin:/bin", "HOME": "/root", "LC_ALL": "C"}
REPOS = {"core", "extra", "multilib"}
PACKAGE = r"[a-z0-9][a-z0-9@._+-]{0,254}"
VERSION = r"[a-zA-Z0-9][a-zA-Z0-9@._+~:-]{0,254}"
FILENAME = r"[a-zA-Z0-9@._+-]{1,220}\.pkg\.tar(?:\.[a-z0-9]{1,12})?"
MAX_REQUEST = 8 * 1024 * 1024


def token(value, pattern):
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise ValueError("invalid request field")
    return value.encode("ascii")


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


class Package(c.Structure):
    _fields_ = [(key, c.c_char_p) for key in ("repo", "name", "installed", "version", "sha256", "filename")]


class Installed(c.Structure):
    _fields_ = [(key, c.c_char_p) for key in ("name", "version")]


class Receipt(c.Structure):
    _fields_ = [("dirfd", c.c_int), ("id", c.c_char_p), ("request_sha256", c.c_char_p)]


def parse_request(raw, transaction, digest, uid):
    if len(raw) > MAX_REQUEST or hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("request digest or length mismatch")
    request = json.loads(raw, object_pairs_hook=unique)
    if (set(request) != {"protocol", "step", "transaction", "uid", "target"}
            or request["protocol"] != "pennix-arch-v1" or request["step"] != "system-upgrade"
            or request["transaction"] != transaction or type(request["uid"]) is not int or request["uid"] != uid):
        raise ValueError("request identity mismatch")
    target = request["target"]
    installed, review = target["installed"], target["native_review"]
    local = review.get("local", {})
    if (not isinstance(installed, dict) or set(review) - {"local"} != {"requested", "transaction", "policy"}
            or not isinstance(local, dict) or not all(isinstance(entry, dict) for entry in local.values())
            or not isinstance(review["requested"], list) or not isinstance(review["transaction"], list)
            or not 0 < len(review["transaction"]) <= 8192 or len(installed) > 65536):
        raise ValueError("invalid transaction shape")
    baseline = [Installed(token(name, PACKAGE), token(version, VERSION))
                for name, version in sorted(installed.items())]
    requested = [token(name, PACKAGE) for name in review["requested"]]
    rows = {}
    for row in review["transaction"]:
        repo, name, filename = (row[key] for key in ("repo", "name", "filename"))
        if (set(row) != {"repo", "name", "installed", "target", "filename", "sha256"}
                or (repo is not None and repo not in REPOS) or name in rows or row["installed"] != installed.get(name)
                or not token(filename, FILENAME).startswith(token(name, PACKAGE) + b"-")):
            raise ValueError("invalid package row")
        if repo is None and any(local.get(name, {}).get(field) != row[column]
                                for field, column in (("version", "target"), ("filename", "filename"), ("sha256", "sha256"))):
            raise ValueError("local artifact identity differs from the review")
        rows[name] = Package(repo.encode() if repo else None, name.encode(), token(row["installed"], VERSION) if name in installed else None,
                             token(row["target"], VERSION), token(row["sha256"], r"[0-9a-f]{64}"), filename.encode())
    if (set(local) != {name for name, row in rows.items() if row.repo is None}
            or len(set(requested)) != len(requested)
            or not set(requested) <= {row.name for row in rows.values() if row.repo is not None}):
        raise ValueError("requested package missing from review")
    return baseline, requested, list(rows.values()), review["policy"], local


def root_file(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022 or info.st_nlink != 1:
        raise ValueError("not a system-owned regular file")
    return info


def config_inputs(arch_policy):
    pending = [Path("/etc/pacman.conf")]
    seen = {}
    while pending:
        path = pending.pop()
        if path in seen:
            continue
        arch_policy.trusted_directory(path.parent)
        info = root_file(path)
        if info.st_size > 1024 * 1024 or len(seen) >= 64:
            raise ValueError("native configuration input exceeds supported limits")
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as source:
            if os.fstat(source.fileno()) != info:
                raise ValueError("native configuration input changed before reading")
            content = source.read(1024 * 1024 + 1)
        if len(content) > 1024 * 1024:
            raise ValueError("native configuration input exceeds supported limits")
        seen[path] = hashlib.sha256(content).digest()
        for line in content.decode().splitlines():
            key, separator, value = line.split("#", 1)[0].partition("=")
            if key.strip() == "Include":
                if not separator:
                    raise ValueError("invalid native include")
                # Do not let an unverified glob or alternate parser grammar pick root inputs.
                included = token(value.strip(), r"/[a-zA-Z0-9._/-]{1,1023}")
                pending.append(Path(included.decode()))
    return seen


def config_text(arch_policy):
    before = config_inputs(arch_policy)
    p = subprocess.run(["/usr/bin/pacman-conf", "--verbose"], capture_output=True, text=True,
                       stdin=subprocess.DEVNULL, env=ENV, timeout=15, check=True)
    if (p.stderr or not p.stdout.startswith("[options]\n") or len(p.stdout) > 1024 * 1024
            or config_inputs(arch_policy) != before):
        raise ValueError("native configuration could not be resolved")
    return p.stdout


def config_sections(text):
    sections = {}
    for line in text.splitlines():
        if re.fullmatch(r"\[[a-z]+\]", line):
            name = line[1:-1]
            if name in sections:
                raise ValueError("duplicate native configuration section")
            sections[name] = section = {}
            continue
        key, separator, value = line.partition(" = ")
        if not re.fullmatch(r"[A-Za-z]+", key) or separator and not value or "\0" in value:
            raise ValueError("invalid native configuration")
        section.setdefault(key, []).append(value if separator else None)
    options = sections.pop("options")
    if not sections or sections.keys() - REPOS:
        raise ValueError("unreviewed native repository layout")
    return options, sections


def flags(values, bits):
    if not values or len(set(values)) != len(values) or any(value not in bits for value in values):
        raise ValueError("unsupported native policy flags")
    return sum(bits[value] for value in values)


def siglevel(values):
    # The supported ALPM ABI is checked before constructing its handle.
    if not {"PackageRequired", "PackageTrustedOnly", "DatabaseTrustedOnly"} <= set(values):
        raise ValueError("package or repository signature policy is not strictly trusted")
    if len({"DatabaseRequired", "DatabaseOptional"} & set(values)) != 1:
        raise ValueError("repository signature policy is incomplete")
    return flags(values, {"PackageRequired": 1, "PackageTrustedOnly": 0, "DatabaseRequired": 1 << 10,
                          "DatabaseOptional": (1 << 10) | (1 << 11), "DatabaseTrustedOnly": 0})


def one(values, name):
    (value,) = values[name]
    return value


def stage(rows, caches, destination, local_paths, local):
    budget = max(0, shutil.disk_usage(destination).free - 64 * 1024**2)
    for row in rows:
        name = row.filename.decode("ascii")
        origin = caches if row.repo else [local_paths[row.name.decode("ascii")]]
        for cache in origin:
            try:
                info = root_file(cache / name)
                signature = root_file(cache / (name + ".sig"))
            except FileNotFoundError:
                continue
            for filename, checked, limit in ((name, info, 8 * 1024**3), (name + ".sig", signature, 256 * 1024)):
                if not 0 < checked.st_size <= min(limit, budget):
                    raise ValueError("native cache artifact length out of bounds")
                budget -= checked.st_size
                with os.fdopen(os.open(cache / filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as source:
                    if os.fstat(source.fileno()) != checked:
                        raise ValueError("native cache artifact changed before copying")
                    with (destination / filename).open("xb") as sink:
                        remaining = checked.st_size
                        while remaining and (chunk := source.read(min(remaining, 1024 * 1024))):
                            sink.write(chunk)
                            remaining -= len(chunk)
                        if remaining or source.read(1):
                            raise ValueError("native cache artifact length changed")
            if not row.repo:
                with (destination / (name + ".sig")).open("rb") as source:
                    if hashlib.file_digest(source, "sha256").hexdigest() != local[row.name.decode()]["signature_sha256"]:
                        raise ValueError("reviewed local signature changed")
            break
        else:
            raise ValueError("reviewed package and detached signature must already be in a trusted system cache")


def native(lib, name, result, *arguments):
    fn = getattr(lib, name)
    fn.restype, fn.argtypes = result, arguments
    return fn


def local_approvals(review, arch_policy):
    paths, signers = {}, set()
    for name, expected in review.items():
        path, approval = arch_policy.local_package(name)
        if expected != approval:
            raise ValueError("local artifact is not authorized by the installed system policy")
        paths[name] = path
        signers.add(approval["signer"])
    if len(signers) > 1:
        raise ValueError("local artifacts require a common approved signing identity")
    return paths, next(iter(signers)).encode() if signers else None


def apply(text, baseline, targets, rows, receipt, policy_check, directory, arch_policy, local):
    options, repos = config_sections(text)
    strings = {"NoUpgrade": "add_noupgrade", "NoExtract": "add_noextract", "IgnorePkg": "add_ignorepkg",
               "IgnoreGroup": "add_ignoregroup", "Architecture": "add_architecture", "HookDir": "add_hookdir"}
    switches = {"CheckSpace": "set_checkspace", "UseSyslog": "set_usesyslog"}
    # These settings only affect the frontend or native downloads, which are disabled.
    inactive = {"CacheDir", "DownloadUser", "HoldPkg", "ParallelDownloads", "CleanMethod", "Color", "VerbosePkgLists",
                "DisableDownloadTimeout", "DisableSandbox", "DisableSandboxFilesystem", "DisableSandboxSyscalls",
                "ILoveCandy", "LocalFileSigLevel", "RemoteFileSigLevel"}
    if options.keys() - (strings.keys() | switches.keys() | inactive |
                         {"RootDir", "DBPath", "GPGDir", "LogFile", "SigLevel"}):
        raise ValueError("unsupported native options")
    if one(options, "RootDir") != "/" or not options.get("Architecture") or options["Architecture"] == ["auto"]:
        raise ValueError("unsupported root or unresolved architecture")
    db, gpg, log = (Path(one(options, name)) for name in ("DBPath", "GPGDir", "LogFile"))
    for path in (db / "local", db / "sync", gpg, log.parent):
        arch_policy.trusted_directory(path)
    if log.exists() or log.is_symlink():
        root_file(log)
    cache_dirs = list(map(Path, options["CacheDir"]))
    for cache in cache_dirs:
        arch_policy.trusted_directory(cache)
    paths, signer = local_approvals(local, arch_policy)
    stage(rows, cache_dirs, directory, paths, local)
    for repo in repos:
        for suffix in (".db", ".db.sig"):
            index = db / "sync" / (repo + suffix)
            if index.is_symlink():
                link = index.readlink()
                if link != Path(link.name):
                    raise ValueError("repository database link leaves the trusted sync directory")
                index = index.with_name(link.name)
            if suffix == ".db" or index.exists():
                root_file(index)
    tx_lib = LIB / "arch-transaction.so"
    root_file(tx_lib)
    alpm = c.CDLL("/usr/lib/libalpm.so.16")
    transaction = c.CDLL(str(tx_lib))
    if not native(alpm, "alpm_version", c.c_char_p)().startswith(b"16."):
        raise ValueError("unsupported native package-manager ABI")
    p, s, i = c.c_void_p, c.c_char_p, c.c_int
    error = i()
    handle = native(alpm, "alpm_initialize", p, s, s, c.POINTER(i))(b"/", os.fsencode(db), c.byref(error))
    if not handle:
        raise ValueError("cannot initialize native package manager")

    def setting(name, value, kind=s):
        if native(alpm, "alpm_option_" + name, i, p, kind)(handle, value) != 0:
            raise ValueError("could not apply native option")

    try:
        setting("add_cachedir", os.fsencode(directory))
        setting("set_gpgdir", os.fsencode(gpg))
        setting("set_logfile", os.fsencode(log))
        default_level = siglevel(options["SigLevel"])
        setting("set_default_siglevel", default_level, i)
        for key, setter in strings.items():
            for value in options.get(key, ()):
                setting(setter, value.encode())
        for key, setter in switches.items():
            if key in options:
                if options[key] != [None]:
                    raise ValueError("invalid native switch")
                setting(setter, 1, i)
        for name, repo in repos.items():
            if repo.keys() - {"Usage", "SigLevel", "Server", "CacheServer"}:
                raise ValueError("unsupported repository options")
            level = siglevel(repo["SigLevel"]) if "SigLevel" in repo else default_level
            usage = flags(repo.get("Usage", ["All"]), {"All": 15, "Sync": 1, "Search": 2, "Install": 4, "Upgrade": 8})
            if not 0 < usage <= 15:
                raise ValueError("invalid repository usage")
            registered = native(alpm, "alpm_register_syncdb", p, p, s, i)(handle, name.encode(), level)
            if not registered or native(alpm, "alpm_db_set_usage", i, p, i)(registered, usage) != 0:
                raise ValueError("could not register the native repository")
            for server in repo.get("Server", ()):
                if native(alpm, "alpm_db_add_server", i, p, s)(registered, server.encode()) != 0:
                    raise ValueError("could not register the native repository server")
        check = c.CFUNCTYPE(c.c_bool, p)(lambda _: policy_check())
        return native(transaction, "arch_commit_reviewed", i, p, c.POINTER(s), c.c_size_t,
                      c.POINTER(Package), c.c_size_t, c.POINTER(Installed), c.c_size_t,
                      s, c.POINTER(Receipt), type(check), p)(
            handle, (s * len(targets))(*targets), len(targets),
            (Package * len(rows))(*rows), len(rows), (Installed * len(baseline))(*baseline), len(baseline),
            signer, c.byref(receipt), check, None)
    finally:
        native(alpm, "alpm_release", i, p)(handle)


def main():
    result = 1
    try:
        if not sys.flags.isolated or os.geteuid() != 0 or len(sys.argv) != 4 or sys.argv[1] != "commit":
            return 1
        transaction, digest = sys.argv[2:]
        token(transaction, r"[0-9a-f]{32}")
        token(digest, r"[0-9a-f]{64}")
        uid = int(token(os.environ.get("SUDO_UID", "0"), r"(?:0|[1-9][0-9]{0,9})"))
        os.environ.clear()
        os.environ.update(ENV)
        os.umask(0o077)
        os.chdir("/")
        # This directory is the independently installed system payload, not the user checkout.
        for parent in (*reversed(LIB.parents), LIB):
            info = parent.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
                return 1
        for filename in ("arch-commit", "arch_policy.py"):
            root_file(LIB / filename)
        policy_source = LIB / "arch_policy.py"
        arch_policy = ModuleType("arch_policy")
        exec(compile(policy_source.read_bytes(), str(policy_source), "exec"), vars(arch_policy))

        raw = sys.stdin.buffer.read(MAX_REQUEST + 1)
        baseline, targets, rows, policy, local = parse_request(raw, transaction, digest, uid)
        receipts = STATE / "arch-transactions"
        arch_policy.trusted_directory(receipts)
        fd = os.open(receipts, os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return 2
            try:
                os.stat(transaction, dir_fd=fd, follow_symlinks=False)
                return 2
            except FileNotFoundError:
                pass
            text = config_text(arch_policy)

            def policy_matches():
                try:
                    local_approvals(local, arch_policy)
                    return arch_policy.snapshot(config_text(arch_policy)) == policy
                except (OSError, ValueError, subprocess.SubprocessError):
                    return False

            if arch_policy.snapshot(text) != policy:
                return 1
            with tempfile.TemporaryDirectory(prefix="cache-", dir=STATE) as directory:
                receipt = Receipt(fd, transaction.encode(), digest.encode())
                result = apply(text, baseline, targets, rows, receipt, policy_matches, Path(directory), arch_policy, local)
        finally:
            os.close(fd)
        return result
    except (OSError, ValueError, KeyError, AttributeError, TypeError, RecursionError, subprocess.SubprocessError):
        return 1 if result == 1 else 2


if __name__ == "__main__":
    outcome = main()
    if outcome == 1 and len(sys.argv) == 4 and re.fullmatch(r"[0-9a-f]{64}", sys.argv[3]):
        print(f"pennix-arch-refused-v1 {sys.argv[3]}")
    raise SystemExit(outcome)
