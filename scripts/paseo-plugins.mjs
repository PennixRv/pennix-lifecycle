import { execFile } from 'node:child_process';
import { lstatSync, readFileSync, realpathSync } from 'node:fs';
import { join } from 'node:path';
import { isDeepStrictEqual, promisify } from 'node:util';

const run = promisify(execFile);

export function assertPluginSdk(client) {
  const required = ['getDaemonConfig', 'listPlugins', 'invokePluginRpc', 'installPluginSource',
    'previewPluginUpdates', 'applyPluginUpdates', 'removePlugin', 'enablePlugin', 'disablePlugin'];
  if (required.some(name => typeof client[name] !== 'function')) throw new Error('plugin SDK unavailable');
}

function storedSettings(home, id, name) {
  const path = join(home, 'plugin-settings', id, `${name}.json`);
  try {
    const info = lstatSync(path);
    if (!info.isFile() || info.uid !== process.getuid() || info.size > 256 * 1024
        || realpathSync(path) !== path) throw new Error('unsafe settings path');
    const stored = JSON.parse(readFileSync(path, 'utf8'));
    // v0.17.1 can persist migrations during a normal settings read.
    if (stored.version !== 1 || !stored.values || Array.isArray(stored.values)
        || typeof stored.values !== 'object') throw new Error('unreviewed settings schema');
    return stored.values;
  } catch (error) {
    if (error.code === 'ENOENT') return null;
    throw error;
  }
}

export async function managePlugins(client, home, daemonStatus) {
  assertPluginSdk(client);
  const request = JSON.parse(readFileSync(0, 'utf8'));
  const { id, url, blocks, settingsContract, settingsUpgrade, server } = request;
  if (!/^[a-z0-9-]+$/.test(id) || !blocks.every(name => /^[a-z]+$/.test(name))
      || (server !== undefined && server !== daemonStatus.serverId)) throw new Error('invalid target');
  if ((await client.getDaemonConfig()).config.pluginsEnabled !== true) {
    throw new Error('plugins disabled for daemon');
  }

  const snapshot = async () => {
    const item = (await client.listPlugins()).find(plugin => plugin.id === id);
    if (!item) {
      const residue = blocks.some(name => storedSettings(home, id, name) !== null);
      return { server: daemonStatus.serverId, plugin: null, residue };
    }
    const installation = item.installation;
    const identity = installation?.identity;
    if (identity?.kind !== 'git' || identity.remote !== url || identity.pluginPath !== '.'
        || !/^[0-9a-f]{40}$/.test(installation.currentRevision)
        || !item.path.startsWith(`${home}/plugins/${id}/`) || realpathSync(item.path) !== item.path) {
      throw new Error('unknown plugin installation');
    }
    const git = args => run('git', ['--no-optional-locks', '-C', item.path, ...args], { timeout: 10000 });
    if ((await git(['rev-parse', 'HEAD'])).stdout.trim() !== installation.currentRevision
        || (await git(['status', '--porcelain', '--untracked-files=normal'])).stdout) {
      throw new Error('plugin checkout drift');
    }
    const contract = settingsUpgrade?.ref === installation.currentRevision ? settingsUpgrade.contract : settingsContract;
    const contractPaths = Object.keys(contract);
    const blobs = (await git(['rev-parse', ...contractPaths.map(path => `HEAD:${path}`)])).stdout.trim().split('\n');
    if (contractPaths.some((path, index) => blobs[index] !== contract[path])) {
      throw new Error('plugin settings contract changed');
    }
    const settings = {};
    for (const name of blocks) {
      const values = storedSettings(home, id, name);
      if (item.status === 'running') {
        const result = await client.invokePluginRpc(id, `settings.${name}.read`, {});
        if (result.status !== 'ready') throw new Error('unavailable settings');
        settings[name] = { values: result.values, revision: result.revision };
      } else {
        settings[name] = { values, revision: null };
      }
    }
    return { server: daemonStatus.serverId, plugin: { path: item.path, enabled: item.enabled,
      status: item.status, ref: installation.currentRevision, registry: identity.registry ?? null, settings } };
  };

  const before = await snapshot();
  if (request.action === 'inspect') return before;
  if (!isDeepStrictEqual(before, request.before)) throw new Error('state changed after planning');
  const { plugin } = before;
  switch (request.action) {
    case 'install':
      if (plugin || before.residue || !/^[0-9a-f]{40}$/.test(request.ref)) throw new Error('invalid install');
      await client.installPluginSource({ id, source: url, ref: request.ref });
      break;
    case 'update': {
      if (!plugin || plugin.registry || !/^[0-9a-f]{40}$/.test(request.ref)) throw new Error('invalid update');
      const [item] = await client.previewPluginUpdates({ pluginId: id, target: { kind: 'git', ref: request.ref } });
      const proposal = item?.proposal;
      if (item?.id !== id || item.outcome !== 'update' || proposal?.target?.commit !== request.ref
          || proposal.expected.revision !== plugin.ref || proposal.expected.identity.remote !== url
          || proposal.expected.identity.registry) throw new Error('unexpected update proposal');
      const [result] = await client.applyPluginUpdates([proposal]);
      if (result?.id !== id || result.outcome !== 'updated') throw new Error('native update failed');
      break;
    }
    case 'remove':
      if (!plugin) throw new Error('plugin missing');
      await client.removePlugin(id);
      break;
    case 'enable':
      await client.enablePlugin(id);
      break;
    case 'disable':
      await client.disablePlugin(id);
      break;
    case 'settings': {
      const name = request.block;
      if (!blocks.includes(name) || !plugin?.settings[name]?.revision) throw new Error('settings unavailable');
      const result = await client.invokePluginRpc(id, `settings.${name}.write`,
        { revision: plugin.settings[name].revision, values: request.values });
      if (result.status !== 'saved' || !isDeepStrictEqual(result.values, request.values)) {
        throw new Error('settings write did not converge');
      }
      break;
    }
    default:
      throw new Error('unknown action');
  }
  return await snapshot();
}
