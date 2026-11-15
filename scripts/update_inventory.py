"""Read-only source discovery, including an isolated Arch sync database."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import urllib.parse

import candidates


class UpdateError(ValueError):
    pass


def run(command, *, timeout=120):
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except BaseException:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate()
        raise
    if len(stdout) + len(stderr) > 8 * 1024 * 1024:
        raise UpdateError("更新查询输出超过上限")
    return subprocess.CompletedProcess(command, process.returncode, stdout.decode(), stderr.decode())


def installed_arch():
    result = run(['pacman', '-Q'])
    if result.returncode:
        raise UpdateError("无法读取已安装 Arch 包身份")
    return dict(line.split() for line in result.stdout.splitlines())


def sync_digest(directory):
    digest = hashlib.sha256()
    if directory.exists():
        for path in sorted(directory.rglob('*')):
            if path.is_file():
                digest.update(str(path.relative_to(directory)).encode() + b'\0' + path.read_bytes())
    return digest.hexdigest()


def arch():
    """Use the pacman-contrib checkupdates procedure; never refresh the real DB."""
    try:
        result = run(['pacman-conf', 'DBPath'])
        if result.returncode or not result.stdout.strip():
            raise UpdateError('无法解析 pacman DBPath')
        real = Path(result.stdout.strip())
        before = sync_digest(real / 'sync')
        installed = installed_arch()
        with tempfile.TemporaryDirectory(prefix='pennix-checkupdates-') as directory:
            temporary = Path(directory)
            (temporary / 'local').symlink_to(real / 'local', target_is_directory=True)
            result = run(['fakeroot', '--', 'pacman', '-Sy', '--disable-sandbox-filesystem',
                          '--dbpath', directory, '--logfile', '/dev/null'])
            if result.returncode:
                raise UpdateError('临时 Arch 数据库同步失败；真实数据库未作为同步目标')
            result = run(['pacman', '-Sl', '--dbpath', directory])
            if result.returncode:
                raise UpdateError('临时 Arch 数据库查询失败')
            available = {}
            for line in result.stdout.splitlines():
                _repo, name, version, *_ = line.split()
                available.setdefault(name, version)
            result = run(['pacman', '-Qu', '--dbpath', directory])
            if result.returncode not in {0, 1} or result.returncode == 1 and result.stdout.strip():
                raise UpdateError('Arch 可更新包查询失败')
            upgrades = []
            for line in result.stdout.splitlines():
                name, old, arrow, new, *_ = line.split()
                if arrow != '->' or installed.get(name) != old:
                    raise UpdateError('Arch 更新清单与安装身份不一致')
                upgrades.append({'name': name, 'installed': old, 'available': new})
        if sync_digest(real / 'sync') != before or installed_arch() != installed:
            raise UpdateError('查询期间真实 Arch 数据库或已安装包发生变化；须重新审阅')
        return {'state': 'current', 'installed': installed, 'available': available, 'upgrades': upgrades,
                'real_sync_sha256': before}
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        return {'state': 'blocked', 'detail': str(error) if isinstance(error, UpdateError)
                else 'Arch 只读查询失败或前置程序不可用', 'upgrades': []}


def available(component, locked, system):
    package = component.get('package', {})
    source = package.get('source')
    identity = {'source': component['lock_source']}
    if source == 'official':
        if system['state'] != 'current' or package['name'] not in system['available']:
            raise UpdateError('官方包临时数据库候选不可用')
        return {**identity, 'version': system['available'][package['name']]}
    if source == 'aur':
        name = package['name']
        info = candidates._json('https://aur.archlinux.org/rpc/v5/info/' + urllib.parse.quote(name, safe=''))
        rows = info.get('results', [])
        if len(rows) != 1 or rows[0].get('Name') != name or not isinstance(rows[0].get('Version'), str):
            raise UpdateError('AUR 候选身份不完整')
        return {**identity, 'version': rows[0]['Version'], 'ref': candidates._git_head(name, aur=True)}
    if source == 'npm' and not package.get('asset'):
        registry = package.get('registry', 'https://registry.npmjs.org/').rstrip('/')
        name = urllib.parse.quote(package['name'], safe='')
        info = candidates._json(f"{registry}/{name}/{package.get('tag', 'latest')}")
        if info.get('name') != package['name'] or not isinstance(info.get('version'), str):
            raise UpdateError('npm 候选身份不完整')
        return {**identity, 'version': info['version']}
    if package.get('asset'):
        spec = package['asset']
        release = candidates._gh_release(spec['repo'], None)
        assets = [item for item in release.get('assets', []) if item.get('name', '').startswith(spec['name_prefix'])
                  and item.get('name', '').endswith(spec['suffix'])]
        if len(assets) != 1:
            raise UpdateError('固定发行没有唯一登记资产')
        version = assets[0]['name'][len(spec['name_prefix']):-len(spec['suffix'])]
        if not version or release['tagName'] != 'v' + version:
            raise UpdateError('固定发行 tag 与资产身份不一致')
        return {**identity, 'version': version, 'ref': release['tagName']}
    if component.get('delivery') == 'plugin':
        repo = urllib.parse.urlparse(component['plugin']['marketplace']['source']).path.strip('/').removesuffix('.git')
        return {**identity, 'ref': candidates._git_head(repo)}
    if component.get('delivery') == 'collection':
        spec = component['collection_contract']['source']
        return {**identity, 'ref': spec.get('ref') or candidates._git_head(spec['repo'])}
    if component.get('delivery') == 'client':
        spec = component['client']['source']
        return {**identity, 'ref': spec.get('ref') or candidates._git_head(spec['repo'])}
    with tempfile.TemporaryDirectory(prefix='pennix-update-query-') as directory:
        return candidates.lock_entry(candidates.resolve(component, Path(directory)))


def inspect(catalog, selected, locks, plan, *, system_upgrade):
    system = arch()
    def query(name):
        component = catalog['components'][name]
        locked = locks.get(name)
        item = {'locked': locked, 'installed': plan['components'][name], 'source': component.get('lock_source'),
                'configuration': component.get('native_config'), 'restart': name == 'paseo-cli'}
        if component.get('delivery') not in {'package', 'plugin', 'client', 'collection'}:
            return name, {**item, 'state': 'native', 'available': None}
        try:
            target = available(component, locked, system)
            external = component.get('package', {}).get('external_satisfies') and plan['components'][name].get('ownership') == 'external'
            state = 'external' if external else 'unmanaged' if not locked else 'current' if candidates.matches(locked, target) else 'update'
            return name, {**item, 'state': state, 'available': target}
        except (OSError, ValueError, subprocess.TimeoutExpired) as error:
            return name, {**item, 'state': 'unknown', 'available': None, 'query_error': type(error).__name__,
                          'detail': '登记来源查询失败；不视为最新'}
    with ThreadPoolExecutor(max_workers=6) as pool:
        components = dict(pool.map(query, sorted(selected & catalog['components'].keys())))
    return {'components': components, 'connections': plan['connections'],
            'system': {key: value for key, value in system.items() if key != 'available'},
            'system_upgrade_enabled': system_upgrade,
            'tool_update': 'pennix tool update；切换后须用新进程重审此清单'}


def verify_system(before, upgrades, *, accepted=None):
    expected = dict(before) if accepted is None else dict(accepted)
    if accepted is None:
        expected.update({item['name']: item['available'] for item in upgrades})
    if installed_arch() != expected:
        raise UpdateError('完整系统升级的实际包集合超出已审阅清单；保留事务，停止后续动作并重新审阅')
