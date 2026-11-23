#!/usr/bin/env node
// Resolve only public SDK exports from the verified CLI's own installation.
import { realpathSync, readFileSync, readdirSync, statSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { randomUUID } from 'node:crypto';
import { managePlugins } from './paseo-plugins.mjs';

const run = promisify(execFile);
const [action, executable, home] = process.argv.slice(2);
let stage = 'compatibility';
function packageRoot(entry, name) {
  let root = dirname(realpathSync(entry));
  for (;;) {
    try {
      const metadata = JSON.parse(readFileSync(join(root, 'package.json'), 'utf8'));
      if (metadata.name === name) return { root, metadata };
    } catch {}
    const parent = dirname(root);
    if (parent === root) throw new Error();
    root = parent;
  }
}
async function main() {
try {
  if (!['probe', 'install', 'status', 'inspect', 'plugins'].includes(action) || !executable || !home?.startsWith('/')) throw new Error();
  const { root, metadata } = packageRoot(executable, '@getpaseo/cli');
  if (typeof metadata.version !== 'string' || !metadata.version) throw new Error();
  const require = createRequire(join(root, 'package.json'));
  const clientEntry = require.resolve('@getpaseo/client/node');
  const serverEntry = require.resolve('@getpaseo/server/daemon-control');
  packageRoot(clientEntry, '@getpaseo/client');
  const { root: serverRoot } = packageRoot(serverEntry, '@getpaseo/server');
  const { createHostClient } = await import(pathToFileURL(clientEntry));
  const { readDaemonInstance, waitForDaemonReady, readLocalCredentialForTarget,
    isSameDaemonInstance, stopDaemonInstance } = await import(pathToFileURL(serverEntry));
  if ([createHostClient, readDaemonInstance, waitForDaemonReady, readLocalCredentialForTarget,
    isSameDaemonInstance, ...(['probe', 'install'].includes(action) ? [stopDaemonInstance] : [])]
    .some(fn => typeof fn !== 'function')) throw new Error();
  if (action === 'probe') {
    const client = createHostClient('127.0.0.1:1', { clientId: `pennix-${randomUUID()}`,
      clientType: 'cli', appVersion: metadata.version, reconnect: { enabled: false } });
    try {
      if (['connect', 'close', 'getLastServerInfoMessage', 'getAgentSkillsStatus', 'saveAgentSkillsSelection']
        .some(name => typeof client[name] !== 'function')) throw new Error();
    } finally { await client.close(); }
    stage = 'skill-catalog';
    const bundled = join(serverRoot, 'dist/server/skills');
    const available = readdirSync(bundled, { withFileTypes: true })
      .filter(entry => entry.isDirectory() && /^[a-z0-9][a-z0-9-]*$/.test(entry.name)
        && statSync(join(bundled, entry.name, 'SKILL.md')).isFile())
      .map(entry => entry.name).sort();
    if (!available.length) throw new Error();
    process.stdout.write(`${JSON.stringify({ state: 'compatible', version: metadata.version,
      available,
      targets: ['.agents/skills', '.claude/skills', '.codex/skills (legacy owned only)'] })}\n`);
  } else {
    const previous = await readDaemonInstance(home);
    let owned = null;
    let client;
    let api;
    try {
      if (!previous) {
        stage = 'temporary-start';
        if (action !== 'install') throw new Error();
        await run(executable, ['daemon', 'start', '--home', home, '--timeout', '45'], { timeout: 60000, maxBuffer: 1024 * 1024 });
        owned = await readDaemonInstance(home);
        if (!owned) throw new Error();
      }
      stage = 'readiness';
      const instance = await waitForDaemonReady(home, { timeoutMs: 30000, instance: previous ?? owned });
      if (!isSameDaemonInstance(instance, previous ?? owned)) throw new Error();
      const status = JSON.parse((await run(executable, ['daemon', 'status', '--home', home, '--json'],
        { timeout: 15000, maxBuffer: 1024 * 1024 })).stdout);
      stage = 'identity';
      if (status.pid !== instance.pid || status.home !== home || status.serverId !== instance.serverId) throw new Error();
      stage = 'connection';
      client = createHostClient(instance.listen, { clientId: `pennix-${randomUUID()}`, clientType: 'cli',
        appVersion: metadata.version, localCredential: () => readLocalCredentialForTarget(home, instance.listen) ?? undefined,
        connectTimeoutMs: 15000, reconnect: { enabled: false } });
      await client.connect();
      if (client.getLastServerInfoMessage()?.serverId !== instance.serverId) throw new Error();
      if (action === 'plugins') {
        stage = 'plugin-operation';
        const result = await managePlugins(client, home, status);
        if (!isSameDaemonInstance(instance, await readDaemonInstance(home))) throw new Error();
        process.stdout.write(`${JSON.stringify(result)}\n`);
        return;
      }
      if (action === 'inspect') {
        stage = 'delegations';
        const { createPaseoApi } = await import(pathToFileURL(require.resolve('@getpaseo/client')));
        api = createPaseoApi(client);
        let cursor, count = 0, open = false;
        const cursors = new Set();
        do {
          const page = await api.agents.list({ cursor, limit: 100, includeArchived: true, includeBackground: true });
          if (!Array.isArray(page.entries) || !page.pageInfo || !('nextCursor' in page.pageInfo)) throw new Error();
          for (const entry of page.entries) {
            const agent = entry.agent ?? entry;
            if (typeof agent.id !== 'string' || !agent.id || typeof agent.status !== 'string' || !agent.status
                || !agent.labels || typeof agent.labels !== 'object' || Array.isArray(agent.labels)) throw new Error();
            count++;
            if ((agent.labels['trellis.attempt'] || agent.labels['trellis.task_id'])
                && (agent.status !== 'closed' || agent.activeTurn)) open = true;
          }
          cursor = page.pageInfo.nextCursor;
          if (cursor != null && (typeof cursor !== 'string' || !cursor || cursors.has(cursor))) throw new Error();
          if (cursor != null) cursors.add(cursor);
        } while (cursor != null);
        if (!isSameDaemonInstance(instance, await readDaemonInstance(home))) throw new Error();
        process.stdout.write(`${JSON.stringify({ state: 'inspected',
          identity: { pid: instance.pid, server_id: instance.serverId, home },
          agents_scanned: count, delegations_open: open })}\n`);
        return;
      }
      stage = 'native-skills';
      const result = action === 'install' ? await client.saveAgentSkillsSelection({ mode: 'all' })
        : await client.getAgentSkillsStatus();
      if (result.confirmationRequired || result.state !== 'up-to-date' || result.ops.length
          || result.selection.mode !== 'all' || result.available.some(name => !result.installed.includes(name))) throw new Error();
      const reply = { state: 'current', version: metadata.version,
        available: result.available, installed: result.installed, temporary_daemon: Boolean(owned) };
      // Emit success only after instance-bound cleanup has also succeeded.
      if (owned) {
        stage = 'temporary-stop';
        const actual = await readDaemonInstance(home);
        if (!actual || !isSameDaemonInstance(actual, owned)) throw new Error();
        await client.close();
        client = null;
        await stopDaemonInstance(home, { instance: owned, timeoutMs: 30000, force: false });
        owned = null;
      }
      process.stdout.write(`${JSON.stringify(reply)}\n`);
    } finally {
      if (api) await api.dispose();
      if (client) await client.close();
      if (owned) {
        const actual = await readDaemonInstance(home);
        if (actual && !isSameDaemonInstance(actual, owned)) throw new Error();
        await stopDaemonInstance(home, { instance: owned, timeoutMs: 30000, force: false });
      }
    }
  }
} catch {
  process.stderr.write(`Paseo native ${action === 'plugins' ? 'plugins' : 'Skills'} ${stage} failed; preserve owner state for inspection\n`);
  process.exitCode = 2;
}
}
await main();
