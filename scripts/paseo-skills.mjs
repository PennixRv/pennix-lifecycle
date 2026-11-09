#!/usr/bin/env node
// Resolve only public SDK exports from the verified CLI's own installation.
import { realpathSync, readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { randomUUID } from 'node:crypto';

const run = promisify(execFile);
const [action, executable, home] = process.argv.slice(2);
let stage = 'compatibility';
try {
  if (!['probe', 'install', 'status'].includes(action) || !executable || !home?.startsWith('/')) throw new Error();
  let root = dirname(realpathSync(executable));
  let metadata;
  for (;;) {
    try { metadata = JSON.parse(readFileSync(join(root, 'package.json'), 'utf8')); } catch {}
    if (metadata?.name === '@getpaseo/cli') break;
    const parent = dirname(root);
    if (parent === root) throw new Error();
    root = parent;
  }
  if (metadata.version !== '0.15.3') throw new Error();
  const require = createRequire(join(root, 'package.json'));
  const clientEntry = require.resolve('@getpaseo/client/node');
  const serverEntry = require.resolve('@getpaseo/server/daemon-control');
  for (const name of ['client', 'server']) {
    const value = JSON.parse(readFileSync(join(root, 'node_modules', '@getpaseo', name, 'package.json'), 'utf8'));
    if (value.name !== `@getpaseo/${name}` || value.version !== metadata.version) throw new Error();
  }
  const { createHostClient } = await import(pathToFileURL(clientEntry));
  const { readDaemonInstance, waitForDaemonReady, readLocalCredentialForTarget,
    isSameDaemonInstance, stopDaemonInstance } = await import(pathToFileURL(serverEntry));
  if (action === 'probe') {
    process.stdout.write(`${JSON.stringify({ state: 'compatible', version: metadata.version,
      targets: ['.agents/skills', '.claude/skills', '.codex/skills (legacy owned only)'] })}\n`);
  } else {
    const previous = await readDaemonInstance(home);
    let owned = null;
    let client;
    try {
      if (!previous) {
        stage = 'temporary-start';
        if (action === 'status') throw new Error();
        await run(executable, ['daemon', 'start', '--home', home, '--timeout', '45'], { timeout: 60000, maxBuffer: 1024 * 1024 });
        owned = await readDaemonInstance(home);
        if (!owned) throw new Error();
      }
      stage = 'readiness';
      const instance = await waitForDaemonReady(home, { timeoutMs: 30000, instance: previous ?? owned });
      if (!isSameDaemonInstance(instance, previous ?? owned)) throw new Error();
      const status = JSON.parse((await run(executable, ['daemon', 'status', '--home', home, '--json'],
        { timeout: 15000, maxBuffer: 1024 * 1024 })).stdout);
      stage = 'version';
      if (status.daemonVersion !== metadata.version || status.home !== home) throw new Error();
      stage = 'connection';
      client = createHostClient(instance.listen, { clientId: `pennix-${randomUUID()}`, clientType: 'cli',
        appVersion: metadata.version, localCredential: () => readLocalCredentialForTarget(home, instance.listen) ?? undefined,
        connectTimeoutMs: 15000, reconnect: { enabled: false } });
      await client.connect();
      stage = 'native-skills';
      const result = action === 'install' ? await client.saveAgentSkillsSelection({ mode: 'all' })
        : await client.getAgentSkillsStatus();
      if (result.confirmationRequired || result.state !== 'up-to-date' || result.ops.length
          || result.selection.mode !== 'all' || result.available.some(name => !result.installed.includes(name))) throw new Error();
      const reply = { state: 'current', version: metadata.version,
        installed: result.installed, temporary_daemon: Boolean(owned) };
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
      if (client) await client.close();
      if (owned) {
        const actual = await readDaemonInstance(home);
        if (actual && !isSameDaemonInstance(actual, owned)) throw new Error();
        await stopDaemonInstance(home, { instance: owned, timeoutMs: 30000, force: false });
      }
    }
  }
} catch {
  process.stderr.write(`Paseo native Skills ${stage} failed; preserve owner state for inspection\n`);
  process.exitCode = 2;
}
