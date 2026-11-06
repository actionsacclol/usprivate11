import { existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import {
  agentSpec, backupPath, claudeAddArgs, claudeCodeFile, claudeDesktopConfigDirs, codexBlock, codexServerName,
  install, installClaudeCode, listClients, mergeCodexToml, mergeJsonServer, removeCodexTable, removeJsonServer,
  snippetFor, systemClaudeCli, uninstall, uninstallClaudeCode, writeWithBackup, type ClaudeCli, type PathEnv,
  type RelaySpec,
} from '../electron/system/mcp-clients';

/**
 * One-click "Add to Claude Desktop / Cursor / Windsurf / Codex", one named
 * agent at a time. Everything here runs against temp directories: nothing in
 * this suite reads or writes a real client config.
 */

const BOM = String.fromCharCode(0xfeff);
const SPEC: RelaySpec = {
  command: 'C:\\app\\krypt-polybot-us-backend.exe', args: ['--mcp'],
  env: { KRYPT_POLYBOT_US_USERDATA: 'C:\\Users\\me\\AppData\\Roaming\\Krypt PolyBot US' },
};
const DEFAULT = { id: 'default', name: 'Default' };
const SPORTS = { id: 'abc123xyz', name: 'Sports Sam' };

let root = '';
beforeEach(() => { root = mkdtempSync(join(tmpdir(), 'krypt-polybot-us-mcpcfg-')); });
afterEach(() => rmSync(root, { recursive: true, force: true }));

function winEnv(): PathEnv {
  return { platform: 'win32', home: join(root, 'home'), appData: join(root, 'Roaming'), localAppData: join(root, 'Local') };
}

/** A stand-in `claude` that answers the way the real one does. */
function fakeClaude(answers: { ok: boolean; out: string }[] = [], bin: string | null = 'claude.exe') {
  const calls: string[][] = [];
  const cli: ClaudeCli = {
    find: async () => bin,
    run: async (_bin, args) => { calls.push(args); return answers.shift() ?? { ok: true, out: '' }; },
  };
  return { cli, calls };
}

describe('Add to Claude Code runs Claude Code\'s own command', () => {
  it('adds this agent for every project, with the relay as one whole command', async () => {
    const { cli, calls } = fakeClaude();
    const r = await installClaudeCode(SPEC, SPORTS, winEnv(), cli);
    expect(r.ok).toBe(true);
    expect(r.message).toMatch(/new Claude Code session/);
    expect(calls).toEqual([[
      'mcp', 'add', 'krypt-polybot-us-sports-sam-abc123', '--scope', 'user',
      '--env', 'KRYPT_POLYBOT_US_USERDATA=C:\\Users\\me\\AppData\\Roaming\\Krypt PolyBot US',
      '--', 'C:\\app\\krypt-polybot-us-backend.exe', '--mcp', '--agent', 'abc123xyz',
    ]]);
  });

  it('pressing it again replaces the old entry and says so', async () => {
    const { cli, calls } = fakeClaude([{ ok: false, out: 'MCP server krypt-polybot-us already exists in user config' }]);
    const r = await installClaudeCode(SPEC, DEFAULT, winEnv(), cli);
    expect(r.ok).toBe(true);
    expect(r.message).toMatch(/^Updated/);
    expect(calls.map((a) => a[1])).toEqual(['add', 'remove', 'add']);
    expect(calls[1]).toEqual(['mcp', 'remove', 'krypt-polybot-us', '--scope', 'user']);
  });

  it('no Claude Code on this computer: runs nothing and says what to do', async () => {
    const { cli, calls } = fakeClaude([], null);
    const r = await installClaudeCode(SPEC, DEFAULT, winEnv(), cli);
    expect(r.ok).toBe(false);
    expect(r.message).toMatch(/install it/);
    expect(r.message).toMatch(/Copy setup/);
    expect(calls).toEqual([]);
  });

  it('a failed add is reported, not called done', async () => {
    const { cli } = fakeClaude([{ ok: false, out: 'something broke' }]);
    const r = await installClaudeCode(SPEC, DEFAULT, winEnv(), cli);
    expect(r.ok).toBe(false);
    expect(r.message).toContain('something broke');
  });

  it('no backend binary: nothing to add', async () => {
    const { cli, calls } = fakeClaude();
    expect((await installClaudeCode(null, DEFAULT, winEnv(), cli)).ok).toBe(false);
    expect(calls).toEqual([]);
  });

  it('Remove asks Claude Code, and a missing entry is not an error', async () => {
    const gone = fakeClaude([{ ok: false, out: 'No MCP server named "krypt-polybot-us" in user scope' }]);
    expect(await uninstallClaudeCode(DEFAULT, winEnv(), gone.cli)).toMatchObject({ ok: true, message: 'Nothing to remove.' });
    const ok = fakeClaude();
    expect((await uninstallClaudeCode(SPORTS, winEnv(), ok.cli)).message).toMatch(/^Removed/);
    expect(ok.calls[0]).toEqual(['mcp', 'remove', 'krypt-polybot-us-sports-sam-abc123', '--scope', 'user']);
  });

  it('it never lets Claude Code skip its permission prompts', async () => {
    const env = winEnv();
    mkdirSync(join(env.home, '.claude'), { recursive: true });
    const settings = join(env.home, '.claude', 'settings.json');
    writeFileSync(settings, '{"permissions":{"allow":[]}}');
    const { cli, calls } = fakeClaude();
    await installClaudeCode(SPEC, DEFAULT, env, cli);
    expect(readFileSync(settings, 'utf-8')).toBe('{"permissions":{"allow":[]}}');
    expect(calls.flat().join(' ')).not.toMatch(/allow|permission/i);
  });

  it('the list offers the button and reads whether it is added from Claude Code\'s file', () => {
    const env = winEnv();
    const row = () => listClients(SPEC, DEFAULT, env).find((r) => r.id === 'claude-code')!;
    expect(row()).toMatchObject({ canInstall: true, detected: false, installed: false });
    mkdirSync(env.home, { recursive: true });
    writeFileSync(claudeCodeFile(env), JSON.stringify({ mcpServers: { 'krypt-polybot-us': { command: 'x' } } }));
    expect(row()).toMatchObject({ canInstall: true, detected: true, installed: true });
  });

  it('CLAUDE_CONFIG_DIR moves the file it reads', () => {
    expect(claudeCodeFile({ ...winEnv(), claudeConfigDir: join(root, 'cc') })).toBe(join(root, 'cc', '.claude.json'));
  });

  it.runIf(process.platform === 'win32')('an npm-installed claude.cmd gets every argument whole', async () => {
    const dir = join(root, 'npm folder');
    mkdirSync(dir, { recursive: true });
    writeFileSync(join(dir, 'echo.js'), 'process.stdout.write(JSON.stringify(process.argv.slice(2)))');
    writeFileSync(join(dir, 'claude.cmd'), '@node "%~dp0echo.js" %*\r\n');
    const args = claudeAddArgs(agentSpec(SPEC, SPORTS), 'krypt-polybot-us-sports-sam-abc123');
    const r = await systemClaudeCli(winEnv()).run(join(dir, 'claude.cmd'), args);
    expect(r.ok).toBe(true);
    expect(JSON.parse(r.out)).toEqual(args);
  });

  it('the copied command and the button add the same thing', () => {
    const args = claudeAddArgs(agentSpec(SPEC, SPORTS), 'krypt-polybot-us-sports-sam-abc123');
    const cmd = snippetFor('claude-code', SPEC, SPORTS, 'win32');
    for (const a of args) expect(cmd.replace(/"/g, '')).toContain(a);
  });
});

describe('the entry is token-less stdio, per agent', () => {
  it('launches the relay as that agent', () => {
    expect(agentSpec(SPEC, SPORTS).args).toEqual(['--mcp', '--agent', 'abc123xyz']);
    expect(agentSpec(SPEC, DEFAULT).args).toEqual(['--mcp', '--agent', 'default']);
  });

  it('files each agent under its own server name, Default under the original one', () => {
    const d = JSON.parse(snippetFor('cursor', SPEC, DEFAULT));
    const s = JSON.parse(snippetFor('cursor', SPEC, SPORTS));
    expect(Object.keys(d.mcpServers)).toEqual(['krypt-polybot-us']);
    expect(Object.keys(s.mcpServers)).toEqual(['krypt-polybot-us-sports-sam-abc123']);
  });

  it('no snippet for any client carries a token or a secret-looking value', () => {
    for (const id of ['claude-desktop', 'claude-code', 'cursor', 'codex', 'windsurf', 'vscode'] as const) {
      const text = snippetFor(id, SPEC, SPORTS, 'win32');
      expect(text).not.toMatch(/token|bearer|authorization|secret/i);
      expect(text).toContain('abc123xyz');
    }
  });

  it('Claude Code gets a one-line command with the agent\'s server name', () => {
    const cmd = snippetFor('claude-code', SPEC, SPORTS, 'linux');
    expect(cmd).toMatch(/^claude mcp add krypt-polybot-us-sports-sam-abc123 --scope user --env KRYPT_POLYBOT_US_USERDATA=/);
    expect(cmd).toContain("'--agent' 'abc123xyz'");
  });

  it('Codex gets its own table named after the agent', () => {
    expect(codexServerName(codexBlock(agentSpec(SPEC, SPORTS), 'krypt-polybot-us-sports-sam-abc123')))
      .toBe('krypt-polybot-us-sports-sam-abc123');
  });
});

describe('mergeJsonServer', () => {
  const entry = { command: 'x', args: ['--mcp'], env: {} };

  it('creates the file shape from nothing', () => {
    const r = mergeJsonServer(null, 'krypt-polybot-us', entry);
    expect(r.ok).toBe(true);
    if (r.ok) expect(JSON.parse(r.text).mcpServers['krypt-polybot-us']).toEqual(entry);
  });

  it('keeps every other server and setting, replacing only its own entry', () => {
    const existing = JSON.stringify({
      globalShortcut: 'Ctrl+Space',
      mcpServers: { filesystem: { command: 'npx', args: ['fs'] }, 'krypt-polybot-us': { command: 'old' } },
    });
    const r = mergeJsonServer(existing, 'krypt-polybot-us', entry);
    expect(r.ok && r.replaced).toBe(true);
    if (!r.ok) return;
    const doc = JSON.parse(r.text);
    expect(doc.globalShortcut).toBe('Ctrl+Space');
    expect(doc.mcpServers.filesystem).toEqual({ command: 'npx', args: ['fs'] });
    expect(doc.mcpServers['krypt-polybot-us']).toEqual(entry);
  });

  it('a second agent adds a second server instead of replacing the first', () => {
    const one = mergeJsonServer(null, 'krypt-polybot-us', entry);
    if (!one.ok) throw new Error('merge failed');
    const two = mergeJsonServer(one.text, 'krypt-polybot-us-sports-sam-abc123', entry);
    if (!two.ok) throw new Error('merge failed');
    expect(Object.keys(JSON.parse(two.text).mcpServers)).toHaveLength(2);
  });

  it('refuses a file that is not valid JSON, with a plain message', () => {
    const r = mergeJsonServer('{ "mcpServers": { , }', 'krypt-polybot-us', entry, 'Cursor');
    expect(r.ok).toBe(false);
    if (!r.ok) {
      expect(r.message).toMatch(/not valid JSON/);
      expect(r.message).not.toMatch(/SyntaxError|Unexpected token/);
    }
  });

  it('refuses a non-object document or a non-object mcpServers', () => {
    expect(mergeJsonServer('[1,2]', 'k', entry).ok).toBe(false);
    expect(mergeJsonServer('{"mcpServers": []}', 'k', entry).ok).toBe(false);
    expect(mergeJsonServer('{"mcpServers": "x"}', 'k', entry).ok).toBe(false);
  });

  it('tolerates a BOM and keeps CRLF if the file used it', () => {
    const r = mergeJsonServer(`${BOM}{\r\n  "a": 1\r\n}\r\n`, 'k', entry);
    expect(r.ok).toBe(true);
    if (r.ok) {
      expect(r.text.includes('\r\n')).toBe(true);
      expect(JSON.parse(r.text).a).toBe(1);
    }
  });

  it('removes only its own entry', () => {
    const existing = JSON.stringify({ mcpServers: { other: { command: 'o' }, 'krypt-polybot-us': { command: 'k' } } });
    const r = removeJsonServer(existing, 'krypt-polybot-us');
    expect(r.ok && r.replaced).toBe(true);
    if (r.ok) expect(JSON.parse(r.text).mcpServers).toEqual({ other: { command: 'o' } });
  });
});

describe('mergeCodexToml', () => {
  const block = codexBlock(agentSpec(SPEC, DEFAULT), 'krypt-polybot-us');

  it('appends to an existing file, keeping everything else', () => {
    const existing = 'model = "gpt-5"\n\n[mcp_servers.other]\ncommand = "o"\n';
    const r = mergeCodexToml(existing, block);
    expect(r.ok).toBe(true);
    if (!r.ok) return;
    expect(r.text).toContain('model = "gpt-5"');
    expect(r.text).toContain('[mcp_servers.other]');
    expect(r.text).toContain('[mcp_servers.krypt-polybot-us]');
  });

  it('re-adding replaces its own table in place (and its sub-tables), never a duplicate', () => {
    const existing = '[mcp_servers.krypt-polybot-us]\ncommand = "old"\n[mcp_servers.krypt-polybot-us.env]\nA = "1"\n\n[mcp_servers.other]\ncommand = "o"\n';
    const r = mergeCodexToml(existing, block);
    expect(r.ok && r.replaced).toBe(true);
    if (!r.ok) return;
    expect(r.text.match(/\[mcp_servers\.krypt-polybot-us\]/g)).toHaveLength(1);
    expect(r.text).not.toContain('command = "old"');
    expect(r.text).not.toContain('[mcp_servers.krypt-polybot-us.env]');
    expect(r.text.indexOf('krypt-polybot-us')).toBeLessThan(r.text.indexOf('[mcp_servers.other]'));
  });

  it('refuses a name defined in a form it cannot edit safely', () => {
    const r = mergeCodexToml('[mcp_servers]\n"krypt-polybot-us" = { command = "x" }\n', block);
    expect(r.ok).toBe(false);
  });

  it('keeps CRLF line ends', () => {
    const r = mergeCodexToml('model = "x"\r\n', block);
    expect(r.ok && r.text.includes('\r\n')).toBe(true);
  });

  it('uninstall removes only its own table', () => {
    const existing = 'model = "x"\n\n[mcp_servers.krypt-polybot-us]\ncommand = "k"\n\n[mcp_servers.other]\ncommand = "o"\n';
    const r = removeCodexTable(existing, 'krypt-polybot-us');
    expect(r.ok && r.replaced).toBe(true);
    if (!r.ok) return;
    expect(r.text).not.toContain('krypt-polybot-us');
    expect(r.text).toContain('[mcp_servers.other]');
    expect(r.text).toContain('model = "x"');
  });
});

describe('Claude Desktop folders', () => {
  it('Windows: the normal install AND every Microsoft Store package', () => {
    const env = winEnv();
    mkdirSync(join(env.localAppData!, 'Packages', 'Claude_pzs8sxrjxfjjc'), { recursive: true });
    mkdirSync(join(env.localAppData!, 'Packages', 'Other_123'), { recursive: true });
    const dirs = claudeDesktopConfigDirs(env);
    expect(dirs[0]).toBe(join(env.appData!, 'Claude'));
    expect(dirs).toContain(join(env.localAppData!, 'Packages', 'Claude_pzs8sxrjxfjjc', 'LocalCache', 'Roaming', 'Claude'));
    expect(dirs).toHaveLength(2);
  });
});

describe('writeWithBackup', () => {
  it('keeps a timestamped copy of what was there, then writes atomically', () => {
    const f = join(root, 'cfg.json');
    writeFileSync(f, 'old', 'utf-8');
    const now = new Date('2026-10-08T12:30:05.123Z');
    const w = writeWithBackup(f, 'new', now);
    expect(w.ok).toBe(true);
    expect(readFileSync(f, 'utf-8')).toBe('new');
    expect(readFileSync(backupPath(f, now), 'utf-8')).toBe('old');
    expect(backupPath(f, now)).toBe(`${f}.2026-10-08T12-30-05.bak`);
    expect(readdirSync(root).some((n) => n.endsWith('.krypt-tmp'))).toBe(false);
  });
});

describe('install / uninstall', () => {
  it('Claude Desktop: refuses when it is not installed, writes nothing', () => {
    const env = winEnv();
    const r = install('claude-desktop', SPEC, DEFAULT, env);
    expect(r.ok).toBe(false);
    expect(existsSync(join(env.appData!, 'Claude', 'claude_desktop_config.json'))).toBe(false);
  });

  it('Claude Desktop: writes every install, all-or-nothing on a bad file', () => {
    const env = winEnv();
    const normal = join(env.appData!, 'Claude');
    const store = join(env.localAppData!, 'Packages', 'Claude_x', 'LocalCache', 'Roaming', 'Claude');
    mkdirSync(normal, { recursive: true });
    mkdirSync(store, { recursive: true });
    writeFileSync(join(store, 'claude_desktop_config.json'), '{ broken', 'utf-8');
    const bad = install('claude-desktop', SPEC, SPORTS, env);
    expect(bad.ok).toBe(false);
    expect(existsSync(join(normal, 'claude_desktop_config.json'))).toBe(false);

    writeFileSync(join(store, 'claude_desktop_config.json'), '{"mcpServers":{"fs":{"command":"npx"}}}', 'utf-8');
    const good = install('claude-desktop', SPEC, SPORTS, env);
    expect(good.ok).toBe(true);
    expect(good.files).toHaveLength(2);
    expect(good.backups).toHaveLength(1);
    const doc = JSON.parse(readFileSync(join(store, 'claude_desktop_config.json'), 'utf-8'));
    expect(doc.mcpServers.fs).toEqual({ command: 'npx' });
    expect(doc.mcpServers['krypt-polybot-us-sports-sam-abc123'].args).toEqual(['--mcp', '--agent', 'abc123xyz']);

    const listed = listClients(SPEC, SPORTS, env).find((c) => c.id === 'claude-desktop');
    expect(listed?.installed).toBe(true);
    expect(listClients(SPEC, DEFAULT, env).find((c) => c.id === 'claude-desktop')?.installed).toBe(false);

    const gone = uninstall('claude-desktop', SPORTS, env);
    expect(gone.ok).toBe(true);
    expect(JSON.parse(readFileSync(join(store, 'claude_desktop_config.json'), 'utf-8')).mcpServers)
      .toEqual({ fs: { command: 'npx' } });
  });

  it('Codex: add, re-add (no duplicate), remove', () => {
    const env = { ...winEnv(), codexHome: join(root, 'codex') };
    expect(install('codex', SPEC, DEFAULT, env).ok).toBe(true);
    expect(install('codex', SPEC, DEFAULT, env).ok).toBe(true);
    const text = readFileSync(join(root, 'codex', 'config.toml'), 'utf-8');
    expect(text.match(/\[mcp_servers\.krypt-polybot-us\]/g)).toHaveLength(1);
    expect(uninstall('codex', DEFAULT, env).ok).toBe(true);
    expect(readFileSync(join(root, 'codex', 'config.toml'), 'utf-8')).not.toContain('krypt-polybot-us');
  });

  it('copy/paste clients are never written', () => {
    expect(install('vscode', SPEC, DEFAULT, winEnv()).ok).toBe(false);
  });

  it('Claude Code\'s own file is never written by this app', () => {
    expect(install('claude-code', SPEC, DEFAULT, winEnv()).ok).toBe(false);
    expect(existsSync(claudeCodeFile(winEnv()))).toBe(false);
  });

  it('no backend binary: nothing to install, and the list says why', () => {
    expect(install('cursor', null, DEFAULT, winEnv()).ok).toBe(false);
    const rows = listClients(null, DEFAULT, winEnv());
    expect(rows.every((r) => !r.canInstall)).toBe(true);
    expect(rows[0].snippet).toMatch(/could not be located/);
  });
});
