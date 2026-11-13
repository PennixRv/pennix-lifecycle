"""Run a built release through bootstrap with no host home, network or service access."""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.environ.get("PENNIX_TEST_RELEASE_ARCHIVE"), "requires an explicitly built release archive")
class ReleasePayloadTests(unittest.TestCase):
    def test_bootstrap_real_payload_retry_recovery_and_reinstall(self):
        self.assertIsNotNone(shutil.which("bwrap"), "payload gate needs a filesystem/network namespace")
        archive = Path(os.environ["PENNIX_TEST_RELEASE_ARCHIVE"]).resolve(strict=True)
        with tarfile.open(archive) as source:
            version = source.extractfile("pennix-lifecycle/VERSION").read().decode().strip()
        with tempfile.TemporaryDirectory(prefix="pennix-payload-") as directory:
            fixture = Path(directory)
            home = fixture / "home"
            home.mkdir(mode=0o700)
            etc = fixture / "etc"
            etc.mkdir()
            (etc / "os-release").write_text("ID=arch\n")
            (etc / "passwd").write_text(f"smoke:x:{os.getuid()}:{os.getgid()}::/home/smoke:/bin/zsh\n")
            (etc / "group").write_text(f"smoke:x:{os.getgid()}:\n")
            (etc / "nsswitch.conf").write_text("passwd: files\ngroup: files\n")
            assets = fixture / "assets"
            assets.mkdir()
            asset_name = f"pennix-lifecycle-{version}.tar.gz"
            shutil.copyfile(archive, assets / asset_name)
            (assets / f"{asset_name}.sha256").write_text(f"{hashlib.sha256(archive.read_bytes()).hexdigest()}  {asset_name}\n")
            # Download only the hash-pinned wheels before entering the networkless namespace.
            requirements = fixture / 'runtime-requirements.txt'
            with tarfile.open(archive) as source:
                requirements.write_bytes(source.extractfile('pennix-lifecycle/references/runtime-requirements.txt').read())
            wheels = assets / 'wheels'
            target_python = subprocess.check_output(['/usr/bin/python3', '-c', 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")'], text=True).strip()
            subprocess.run([__import__('sys').executable, '-m', 'pip', 'download', '--require-hashes', '--python-version', target_python,
                            '--only-binary=:all:', '--no-deps', '--index-url', 'https://pypi.org/simple',
                            '-r', str(requirements), '--dest', str(wheels)], check=True, capture_output=True, timeout=120)
            shutil.copyfile('/usr/bin/uv', assets/'uv-real')
            (assets/'uv-real').chmod(0o700)
            uv = fixture/'uv'
            uv.write_text('#!/bin/sh\nexec /fixtures/uv-real "$@" --no-index --find-links /fixtures/wheels\n')
            uv.chmod(0o700)
            curl = fixture / "curl"
            curl.write_text("""#!/usr/bin/python3
import pathlib,sys
args=sys.argv[1:]
url=next(value for value in args if value.startswith('https://'))
assert url.startswith('https://github.com/PennixRv/pennix-lifecycle/releases/download/v')
name=url.rsplit('/',1)[1]
assert '/' not in name and name not in ('.','..')
pathlib.Path(args[args.index('-o')+1]).write_bytes((pathlib.Path('/fixtures')/name).read_bytes())
""")
            sudo = fixture / "sudo"
            sudo.write_text("""#!/usr/bin/python3
import pathlib,sys
args=sys.argv[1:]
if args[0]=='-n': args=args[1:]
assert args[:2]==['pacman','-Syu'], args
pathlib.Path('/home/smoke/upgrade-observed').write_text('full system upgrade explicitly requested')
""")
            for command in (curl, sudo):
                command.chmod(0o700)
            mounts = []
            for name, replacement in (("curl", curl), ("sudo", sudo), ("uv", uv)):
                target = Path("/usr/bin") / name
                while target.is_symlink():
                    link = target.readlink()
                    if target.is_relative_to("/etc"):
                        alias = etc / target.relative_to("/etc")
                        alias.parent.mkdir(parents=True, exist_ok=True)
                        alias.symlink_to(link)
                    target = link if link.is_absolute() else target.parent / link
                mounts += ["--ro-bind", str(replacement), str(target)]
            namespace = ["bwrap", "--unshare-all", "--die-with-parent", "--new-session",
                         "--ro-bind", "/usr", "/usr", "--symlink", "usr/bin", "/bin",
                         "--ro-bind", "/lib", "/lib", "--ro-bind", "/lib64", "/lib64",
                         "--ro-bind", str(etc), "/etc", "--proc", "/proc", "--dev", "/dev",
                         "--tmpfs", "/tmp", "--tmpfs", "/run", "--tmpfs", "/var",
                         "--bind", str(home), "/home/smoke", "--ro-bind", str(assets), "/fixtures",
                         *mounts,
                         "--ro-bind", str(ROOT / "bootstrap.sh"), "/bootstrap.sh", "--chdir", "/home/smoke"]
            env = {"HOME": "/home/smoke", "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
                   "CODEX_HOME": "/home/smoke/nondefault-codex", "XDG_RUNTIME_DIR": "/run/user-test"}
            def run(*command):
                result = subprocess.run([*namespace, *command], env=env, capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                return result.stdout
            run("bash", "/bootstrap.sh", "install", "--release", f"v{version}", "--no-input")
            entry = home / ".local/bin/pennix"
            installed = entry.readlink()
            self.assertEqual(str(installed), f"/home/smoke/.local/share/pennix-lifecycle/releases/{version}/pennix")
            self.assertTrue((home / installed.relative_to("/home/smoke")).is_file())
            self.assertTrue((home / "upgrade-observed").is_file())
            run("/home/smoke/.local/bin/pennix", "config", "init")
            # Configure only intent in the isolated target; never probe real host owners.
            script = f"""
import sys
from pathlib import Path
sys.path.insert(0, '/home/smoke/.local/share/pennix-lifecycle/releases/{version}/scripts')
import config_model
k,p=config_model.load()
for s in k.unique_defined_syms:
    if s.name.startswith('PENNIX_COMPONENT_'): s.set_value('n')
k.syms['PENNIX_CODEX_HOME'].set_value('/home/smoke/nondefault-codex')
config_model.write(k,p,before=k.snapshot)
"""
            python = f'/home/smoke/.local/share/pennix-lifecycle/releases/{version}/.runtime/bin/python'
            run(python, "-c", script)
            run("/home/smoke/.local/bin/pennix", "config", "check", "--json")
            run("/home/smoke/.local/bin/pennix", "plan", "--json")
            # Inject failure after staging and entry switch, then reuse that same version.
            recovery = f"""
import sys,shutil
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, '/home/smoke/.local/share/pennix-lifecycle/releases/{version}/scripts')
import release_owner,owned_files
entry=Path('/home/smoke/.local/bin/pennix')
old=entry.resolve()
candidate=Path('/tmp/candidate')
candidate.mkdir()
for name in release_owner.RUNTIME:
    item=old.parent/name
    if item.is_dir(): shutil.copytree(item,candidate/name)
    else: shutil.copy2(item,candidate/name)
(candidate/'VERSION').write_text('99.0.1\\n')
replace=owned_files.replace
def fail(path,content,before,**kwargs):
    if path==release_owner._receipt_path(): raise OSError('synthetic receipt failure')
    return replace(path,content,before,**kwargs)
with patch.object(owned_files,'replace',side_effect=fail):
    try: release_owner.install(candidate)
    except release_owner.ReleaseError: pass
    else: raise AssertionError('failure was not injected')
assert entry.resolve()==old
assert release_owner.install(candidate)=='installed 99.0.1'
assert entry.resolve().parent.name=='99.0.1'
assert release_owner.uninstall()=='uninstalled'
assert not entry.exists()
"""
            run(python, "-c", recovery)
            self.assertFalse(entry.is_symlink())
            run("bash", "/bootstrap.sh", "install", "--release", f"v{version}", "--no-input")
            run("/home/smoke/.local/bin/pennix", "recover", "--json")
            run("bash", "/bootstrap.sh", "uninstall")
            self.assertFalse(entry.is_symlink())
            self.assertTrue((home / ".config/pennix/workflow.config").is_file())
