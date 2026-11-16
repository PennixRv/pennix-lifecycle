"""The shim lock covers the exact executable platform package, not only the shim."""
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import candidates
import _engine as engine


def archive(files):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w:gz') as bundle:
        for name, value in files.items():
            raw = value.encode()
            member = tarfile.TarInfo('package/' + name)
            member.size = len(raw)
            member.mode = 0o755 if name in {'node', 'bin/codegraph', 'npm-shim.js'} else 0o644
            bundle.addfile(member, io.BytesIO(raw))
    return output.getvalue()


class CodeGraphBundleTests(unittest.TestCase):
    def test_two_registry_integrities_lock_and_actual_offline_npm_payload(self):
        version = '9.0.0-test'
        name, platform_name = '@pennixrv/codegraph', '@pennixrv/codegraph-linux-x64'
        main = archive({'package.json': json.dumps({'name': name, 'version': version,
            'optionalDependencies': {platform_name: version}, 'bin': {'codegraph': 'npm-shim.js'}}),
            'npm-shim.js': '#!/usr/bin/env node\nconsole.log("9.0.0-test");\n'})
        platform = archive({'package.json': json.dumps({'name': platform_name, 'version': version, 'os': ['linux'], 'cpu': ['x64']}),
            'node': '#!/bin/sh\nexit 0\n', 'bin/codegraph': '#!/bin/sh\nexit 0\n', 'lib/dist/bin/codegraph.js': '/* synthetic */\n'})
        blobs = {name: main, platform_name: platform}
        component = {'id': 'codegraph-cli', 'lock_source': 'npm:@pennixrv/codegraph:latest', 'probe': 'codegraph',
                     'package': {'source': 'npm', 'name': name, 'registry': 'https://registry.test/', 'tag': 'latest'}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def metadata(url):
                key = platform_name if 'linux-x64' in url else name
                raw = blobs[key]
                return {'name': key, 'version': version, 'dist': {'tarball': 'https://registry.test/' + ('platform' if key == platform_name else 'main') + '.tgz',
                    'integrity': 'sha512-' + base64.b64encode(hashlib.sha512(raw).digest()).decode()}}
            def download(url, destination):
                destination.write_bytes(platform if url.endswith('platform.tgz') else main)
            with patch.object(candidates, '_json', side_effect=metadata), patch.object(candidates.owned_files, 'download', side_effect=download):
                selected = candidates.resolve_codegraph(component, root)
                self.assertNotEqual(selected['sha256'], hashlib.sha256(main).hexdigest())
                other = root / 'locked';other.mkdir()
                self.assertEqual(candidates.resolve_codegraph(component, other, candidates.lock_entry(selected))['sha256'], selected['sha256'])
            prefix = root / 'prefix'
            env = {**os.environ, 'HOME': str(root), 'NPM_CONFIG_USERCONFIG': '/dev/null', 'NPM_CONFIG_OFFLINE': 'true'}
            result = subprocess.run(['npm', 'install', '--global', '--prefix', str(prefix), '--ignore-scripts', '--omit=optional',
                                     selected['path']], env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            installed = prefix / 'lib/node_modules' / name
            result = subprocess.run(['npm', 'install', '--prefix', str(installed), '--ignore-scripts', '--no-save',
                '--package-lock=false', '--include=optional', selected['platform_path']], env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            bundle = engine._codegraph_platform(installed, version)
            self.assertTrue(bundle.is_relative_to(installed))
            engine.verify_npm_archive(installed, Path(selected['path']))
            engine.verify_npm_archive(bundle, Path(selected['platform_path']))
            (bundle / 'node').write_text('tampered')
            with self.assertRaisesRegex(engine.BootstrapError, 'differ'):
                engine.verify_npm_archive(bundle, Path(selected['platform_path']))
            (bundle / 'node').unlink()
            component['package']['prefix'] = str(prefix)
            with patch.object(engine.subprocess, 'run', side_effect=AssertionError('must not execute a fallback')):
                self.assertIsNone(engine._version(component, str(prefix / 'bin/codegraph')))
            # A trusted main package can repair a missing bundle; both npm actions
            # stay offline and the bundle remains inside its owner's install tree.
            component.update(delivery='package', _lock_target=candidates.lock_entry(selected))
            env.update(XDG_STATE_HOME=str(root / 'state'), CODEX_HOME=str(root / 'codex'))
            receipt = {'schema': 1, 'component': 'codegraph-cli', 'source': 'npm', 'name': name, 'version': version}
            args = SimpleNamespace(codex_home=root / 'codex', candidate=selected, explicit_update=True, repair=True)
            with patch.dict(os.environ, env), patch.object(engine.host, 'detect_host', return_value={'supported': True, 'installers': {'npm': 'npm'}}), \
                    patch.object(engine, '_ensure_npm_prefix'):
                path = engine._package_receipt_path('codegraph-cli')
                engine.owned_files.replace(path, engine.owned_files.json_bytes(receipt), None)
                self.assertTrue(engine.codegraph_main_owned('codegraph-cli', component))
                self.assertFalse(engine.package_owned('codegraph-cli', component))
                # The first damaged artifact must be replaced from the reviewed source.
                self.assertEqual(engine.component_operation(args, {}, 'codegraph-cli', component, 'upgrade'), 'changed')
                bundle = engine._codegraph_platform(installed, version)
                engine.verify_npm_archive(bundle, Path(selected['platform_path']))
                (installed / 'npm-shim.js').write_text('unreviewed main edit')
                with patch.object(engine.subprocess, 'run', side_effect=AssertionError('must not overwrite or execute main drift')):
                    with self.assertRaisesRegex(engine.BootstrapError, 'differ'):
                        engine.component_operation(args, {}, 'codegraph-cli', component, 'upgrade')
                # Restore exactly the synthetic reviewed main bytes, then prove native removal.
                with tarfile.open(selected['path'], 'r:gz') as tar:
                    (installed / 'npm-shim.js').write_bytes(tar.extractfile('package/npm-shim.js').read())
                # An older trusted shim may lack its bundle; prove its original
                # bytes, then install a different fixed version without running it.
                next_version = '10.0.0-test'
                next_main = root / 'next-main.tgz'
                next_main.write_bytes(archive({'package.json': json.dumps({'name': name, 'version': next_version,
                    'optionalDependencies': {platform_name: next_version}, 'bin': {'codegraph': 'npm-shim.js'}}),
                    'npm-shim.js': '#!/usr/bin/env node\nconsole.log("10.0.0-test");\n'}))
                next_platform = root / 'next-platform.tgz'
                next_platform.write_bytes(archive({'package.json': json.dumps({'name': platform_name, 'version': next_version,
                    'os': ['linux'], 'cpu': ['x64']}), 'node': '#!/bin/sh\nexit 0\n',
                    'bin/codegraph': '#!/bin/sh\nexit 0\n', 'lib/dist/bin/codegraph.js': '/* next */\n'}))
                next_target = {**candidates.lock_entry(selected), 'version': next_version, 'sha256': 'd' * 64}
                args.candidate = {**next_target, 'path': str(next_main), 'platform_path': str(next_platform),
                    'previous_main': {**selected, 'previous_lock_sha256': selected['sha256']}}
                args.repair = False
                component['_candidate_target'] = next_target
                (bundle / 'node').unlink()
                self.assertEqual(engine.component_operation(args, {}, 'codegraph-cli', component, 'upgrade'), 'changed')
                bundle = engine._codegraph_platform(installed, next_version)
                engine.verify_npm_archive(bundle, next_platform)
                component['_lock_target'] = next_target
                args.candidate = None
                self.assertEqual(engine.component_operation(args, {}, 'codegraph-cli', component, 'uninstall'), 'changed')
                self.assertFalse(installed.exists())
                self.assertFalse(bundle.exists())
                self.assertFalse((prefix / 'lib/node_modules' / platform_name).exists())

    def test_previous_main_proof_accepts_legacy_or_combined_lock_but_not_another_main(self):
        previous = {'path': '/fixed/main', 'version': '9', 'source': 'same-source', 'sha256': 'a' * 64}
        with tempfile.TemporaryDirectory() as directory, patch.object(candidates, 'resolve_npm', return_value=previous):
            root = Path(directory)
            legacy = {'source': 'same-source', 'version': '9', 'sha256': previous['sha256']}
            with patch.object(candidates, 'resolve_codegraph', side_effect=AssertionError('legacy SHA binds the main')):
                self.assertEqual(candidates.codegraph_previous_main({}, root, legacy)['previous_lock_sha256'], previous['sha256'])
            for count, main_sha in enumerate([previous['sha256'], 'c' * 64]):
                other = root / str(count);other.mkdir()
                with patch.object(candidates, 'resolve_codegraph', return_value={'main_sha256': main_sha}):
                    if count == 0:
                        self.assertEqual(candidates.codegraph_previous_main({}, other, {**legacy, 'sha256': 'b' * 64})['sha256'], previous['sha256'])
                    else:
                        with self.assertRaisesRegex(candidates.CandidateError, 'differs'):
                            candidates.codegraph_previous_main({}, other, {**legacy, 'sha256': 'b' * 64})

    def test_platform_metadata_and_missing_sri_are_refused(self):
        component = {'lock_source': 'npm:@pennixrv/codegraph:latest', 'package': {'name': '@pennixrv/codegraph'}}
        with tempfile.TemporaryDirectory() as directory, patch.object(candidates, '_json', return_value={
                'name': '@pennixrv/codegraph', 'version': '9', 'dist': {'tarball': 'https://registry.npmjs.org/fake.tgz', 'integrity': ''}}):
            with self.assertRaisesRegex(candidates.CandidateError, 'SHA-512'):
                candidates.resolve_codegraph(component, Path(directory))

    def test_invalid_dependency_shape_and_wrong_platform_are_refused(self):
        name, platform_name = '@pennixrv/codegraph', '@pennixrv/codegraph-linux-x64'
        component = {'lock_source': 'npm:@pennixrv/codegraph:latest', 'package': {'name': name}}
        valid_main = {'name': name, 'version': '9', 'optionalDependencies': {platform_name: '9'}}
        valid_platform = {'name': platform_name, 'version': '9', 'os': ['linux'], 'cpu': ['x64']}
        for main, platform in [({**valid_main, 'optionalDependencies': []}, valid_platform),
                               (valid_main, {**valid_platform, 'cpu': ['arm64']})]:
            with self.subTest(main=main, platform=platform), tempfile.TemporaryDirectory() as directory:
                with patch.object(candidates, 'resolve_npm', side_effect=[
                        {'path': 'main', 'version': '9', 'sha256': 'a' * 64, 'impact': 'test'},
                        {'path': 'platform', 'version': '9', 'sha256': 'b' * 64}]), \
                     patch.object(candidates, '_archive_json', side_effect=[main, platform]):
                    with self.assertRaisesRegex(candidates.CandidateError, 'identity'):
                        candidates.resolve_codegraph(component, Path(directory))


if __name__ == '__main__':
    unittest.main()
