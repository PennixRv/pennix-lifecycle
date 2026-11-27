"""Read-only source discovery, including an isolated Arch sync database."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import http.client
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import urllib.parse

import arch_policy
import candidates
import owned_files


class UpdateError(ValueError):
    pass


def run(command, *, timeout=120, noninteractive=False):
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
                               **({"stdin": subprocess.DEVNULL, "env": {**os.environ, "LC_ALL": "C"}}
                                  if noninteractive else {}))
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


def installed_arch(*, pacman="pacman"):
    result = run([pacman, '-Q'])
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


def _arch_policy():
    result = run(['/usr/bin/pacman-conf', '--verbose'], noninteractive=True)
    if result.returncode or result.stderr.strip():
        raise UpdateError("Arch 原生配置无法完整解析")
    try:
        return arch_policy.snapshot(result.stdout)
    except arch_policy.PolicyError as error:
        raise UpdateError(str(error)) from error


def arch(*, include_packages=(), review_full=False):
    """Use the pacman-contrib checkupdates procedure; never refresh the real DB."""
    # The reviewed paths do not resolve helpers from a project-controlled PATH.
    fixed = bool(include_packages or review_full)

    def native(command):
        return f"/usr/bin/{command}" if fixed else command

    try:
        result = run([native('pacman-conf'), 'DBPath'])
        if result.returncode or not result.stdout.strip():
            raise UpdateError('无法解析 pacman DBPath')
        real = Path(result.stdout.strip())
        before = sync_digest(real / 'sync')
        installed = installed_arch(pacman=native('pacman')) if fixed else installed_arch()
        with tempfile.TemporaryDirectory(prefix='pennix-checkupdates-') as directory:
            temporary = Path(directory)
            (temporary / 'local').symlink_to(real / 'local', target_is_directory=True)
            result = run([native('fakeroot'), '--', native('pacman'), '-Sy', '--disable-sandbox-filesystem',
                          '--dbpath', directory, '--logfile', '/dev/null'])
            if result.returncode:
                raise UpdateError('临时 Arch 数据库同步失败；真实数据库未作为同步目标')
            result = run([native('pacman'), '-Sl', '--dbpath', directory])
            if result.returncode:
                raise UpdateError('临时 Arch 数据库查询失败')
            available = {}
            for line in result.stdout.splitlines():
                _repo, name, version, *_ = line.split()
                available.setdefault(name, version)
            result = run([native('pacman'), '-Qu', '--dbpath', directory])
            if result.returncode not in {0, 1} or result.returncode == 1 and result.stdout.strip():
                raise UpdateError('Arch 可更新包查询失败')
            upgrades = []
            for line in result.stdout.splitlines():
                name, old, arrow, new, *_ = line.split()
                if arrow != '->' or installed.get(name) != old:
                    raise UpdateError('Arch 更新清单与安装身份不一致')
                upgrades.append({'name': name, 'installed': old, 'available': new})
            review, transaction = None, None
            if review_full:
                policy = _arch_policy() if include_packages or upgrades else None
                rows = _arch_transaction(directory, installed, upgrades, include_packages, exact=True)
                if rows:
                    if policy is None or _arch_policy() != policy:
                        raise UpdateError("Arch 原生计划、配置或 Hook 与审阅输入不一致")
                    review = {'requested': sorted(include_packages), 'transaction': rows, 'policy': policy}
            elif include_packages:
                transaction = _arch_transaction(directory, installed, upgrades, include_packages)
        current = installed_arch(pacman=native('pacman')) if fixed else installed_arch()
        if sync_digest(real / 'sync') != before or current != installed:
            raise UpdateError('查询期间真实 Arch 数据库或已安装包发生变化；须重新审阅')
        return {'state': 'current', 'installed': installed, 'available': available, 'upgrades': upgrades,
                'real_sync_sha256': before, **({'transaction': transaction} if transaction is not None else {}),
                **({'native_review': review} if review is not None else {})}
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        return {'state': 'blocked', 'detail': str(error) if isinstance(error, UpdateError)
                else 'Arch 只读查询失败或前置程序不可用', 'upgrades': []}


def _arch_transaction(directory, installed, upgrades, requested, *, exact=False):
    # pacman --print may silently accept replacements; the exact lane refuses
    # such rows and still requires a native, locked check before commit.
    fmt = "%r\t%n\t%v\t%f\t%h\t%R" if exact else "%r\t%n\t%v"
    result = run(["/usr/bin/pacman", "-Sup", "--needed", "--dbpath", directory, "--print-format", fmt,
                  "--", *requested], noninteractive=True)
    if result.returncode or result.stderr.strip():
        raise UpdateError("Arch 包计划含无法安全审阅的冲突、替换或诊断；不执行包事务")
    rows = {}
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if (len(parts) != (6 if exact else 3) or parts[0] not in {"core", "extra", "multilib"}
                or not re.fullmatch(r"[a-z0-9][a-z0-9@._+-]*", parts[1])
                or not re.fullmatch(r"[^\s/]+", parts[2]) or parts[1] in rows):
            raise UpdateError("Arch 包计划格式、来源或目标不明确；不执行包事务")
        repo, name, version = parts[:3]
        rows[name] = {"repo": repo, "name": name, "installed": installed.get(name), "target": version}
        if exact:
            filename, sha256, replaces = parts[3:]
            if replaces and replaces != "None":
                raise UpdateError("Arch 只读预演包含声明替换的包；当前执行路径不支持")
            if (not re.fullmatch(r'[a-zA-Z0-9@._+-]+\.pkg\.tar(?:\.[a-z0-9]+)?', filename)
                    or not filename.startswith(name + '-') or not re.fullmatch(r'[0-9a-f]{64}', sha256)):
                raise UpdateError("Arch 包候选没有可核验的文件名或 SHA-256")
            rows[name].update(filename=filename, sha256=sha256)
    if (not set(requested) <= rows.keys()
            or any(rows.get(item["name"], {}).get("target") != item["available"] for item in upgrades)):
        raise UpdateError("Arch 包计划未覆盖全部明确要求和系统升级；不执行包事务")
    return sorted(rows.values(), key=lambda row: row["name"])


def available(component, locked, system):
    package = component.get('package', {})
    source = package.get('source')
    identity = {'source': component['lock_source']}
    if source == 'official':
        if system['state'] != 'current' or package['name'] not in system['available']:
            raise candidates.SourceQueryError('官方包临时数据库候选不可用')
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
        return candidates.plugin_release(component)
    if component.get('delivery') == 'collection':
        spec = component['collection_contract']['source']
        return {**identity, 'ref': spec.get('ref') or candidates._git_head(spec['repo'])}
    if component.get('delivery') == 'client':
        spec = component['client']['source']
        return {**identity, 'ref': spec.get('ref') or candidates._git_head(spec['repo'])}
    with tempfile.TemporaryDirectory(prefix='pennix-update-query-') as directory:
        return candidates.lock_entry(candidates.resolve(component, Path(directory)))


def candidate_failure(error):
    invalid = (isinstance(error, (ValueError, owned_files.OwnershipError))
               and not isinstance(error, candidates.SourceQueryError))
    return {
        'reason': 'candidate-invalid' if invalid else 'source-query-failed',
        'query_error': type(error).__name__,
        'detail': '候选身份或载荷校验失败；不视为最新' if invalid else '登记来源查询失败；不视为最新',
        'next': '核验登记来源后重新运行 pennix updates',
    }


CANDIDATE_FAILURES = (OSError, ValueError, subprocess.TimeoutExpired, http.client.HTTPException,
                      owned_files.OwnershipError)


def inspect(catalog, selected, locks, plan, *, system_upgrade, reviewed=False):
    if reviewed and system_upgrade:
        requested = sorted({package['name'] for name in selected & catalog['components'].keys()
                            if (package := catalog['components'][name].get('package', {})).get('source') == 'official'
                            and plan['components'][name].get('package_state',
                                  plan['components'][name]['state']) == 'missing'})
        system = arch(include_packages=requested, review_full=True)
    else:
        system = arch()
    def query(name):
        component = catalog['components'][name]
        locked = locks.get(name)
        observed = plan['components'][name]
        installed = observed.get('package_state', observed['state'])
        installed_view = ({**observed, 'detail': '本机身份无法核验；具体原因请在本机运行 pennix plan'}
                          if installed in {'blocked', 'drifted'} and observed.get('detail') else observed)
        item = {'locked': locked, 'installed': installed_view, 'source': component.get('lock_source'),
                'configuration': component.get('native_config'), 'restart': name == 'paseo-cli'}
        if component.get('delivery') not in {'package', 'plugin', 'client', 'collection'}:
            return name, {**item, 'state': 'native', 'available': None}
        try:
            target = available(component, locked, system)
            if component.get('delivery') == 'plugin':
                candidates.verify_plugin_upgrade(locked, target)
        except CANDIDATE_FAILURES as error:
            return name, {**item, 'state': 'unknown', 'available': None, **candidate_failure(error)}
        ownership = observed.get('package_ownership', observed.get('ownership'))
        package = component.get('package', {})
        is_external = package.get('external_satisfies') and ownership == 'external' and installed == 'match'
        state, reason, detail, next_action = None, None, None, None
        if installed == 'plugin-migration-required':
            state = 'migration-required'
        elif not is_external and (ownership == 'unknown' or installed in {
            'adoption-required', 'external', 'lock-migration-required'
        } or ownership == 'external' and installed != 'missing'):
            state, reason = 'unmanaged', 'ownership-unverified'
            detail, next_action = '现有安装或受管回执不能证明归属', '先运行 pennix plan 核对安装、锁与受管回执'
        elif installed in {'outdated', 'drifted', 'blocked'}:
            state, reason = 'unknown', 'installed-identity-mismatch'
            detail, next_action = '实装身份不可核验或与登记不一致', '先运行 pennix plan 核对实装身份与锁'
            if installed == 'outdated' and ownership == 'lifecycle' and observed.get('managed_record') \
                    and locked and package.get('source') in {'official', 'aur'}:
                next_action = f'先运行 pennix plan 核对同源身份，再审阅 pennix reconcile {name}'
        elif installed == 'missing':
            state = 'update'
        else:
            state = 'external' if is_external else 'unmanaged' if not locked else 'current' if candidates.matches(locked, target) else 'update'
            if state == 'unmanaged':
                reason, detail, next_action = ('ownership-unverified', '缺少可信组件锁',
                                               '先运行 pennix plan 核对安装、锁与受管回执')
        return name, {**item, 'state': state, 'available': target,
                      **({'reason': reason, 'detail': detail, 'next': next_action} if reason else {})}
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
