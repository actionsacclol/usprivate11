/**
 * Errors that cross from the backend to a person.
 *
 * Electron hands a rejected `ipcRenderer.invoke` to the renderer as ONE
 * string — "Error invoking remote method 'mcp:decide': Error: ValueError: …"
 * — and drops every other property of the error. So a machine-readable code
 * the backend attached cannot ride along as a field. It rides in the message
 * as a `[code] ` prefix instead, put there by main (encodeRpcError) and taken
 * off again by the renderer (parseErrorText) before anyone reads it.
 *
 * Shared, and free of electron and React, so both halves and the tests use
 * the very same parser.
 */

const CODE_RE = /^\[([a-z][a-z0-9_.-]{0,63})\]\s*/i;

/** The message main throws for a backend error carrying a code. */
export function encodeRpcError(message: string, code?: string | null): string {
  const m = String(message ?? '').trim() || 'The backend refused that.';
  const c = typeof code === 'string' ? code.trim() : '';
  return c && /^[a-z][a-z0-9_.-]{0,63}$/i.test(c) ? `[${c}] ${m}` : m;
}

/**
 * Peel the transport off an error's text: Electron's "Error invoking remote
 * method 'x': " wrapper, any number of "Error: " / "ValueError: "-style class
 * names a Python or JS exception put in front, and a `[code]` tag.
 */
export function parseErrorText(raw: string): { code: string | null; message: string } {
  let s = String(raw ?? '').trim();
  s = s.replace(/^Error invoking remote method '[^']*':\s*/i, '');
  let code: string | null = null;
  // Class-name prefixes and the code tag can interleave ("Error: [x] ...",
  // "[x] RuntimeError: ..."), so peel until nothing changes.
  for (let i = 0; i < 6; i++) {
    const before = s;
    s = s.replace(/^(?:[A-Z][A-Za-z0-9_]*(?:Error|Exception|Exit)|Error):\s*/, '');
    const m = CODE_RE.exec(s);
    if (m) {
      code = code ?? m[1].toLowerCase();
      s = s.slice(m[0].length);
    }
    if (s === before) break;
  }
  return { code, message: s.trim() };
}
