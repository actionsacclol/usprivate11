import { useEffect, useRef } from 'react';
import { EditorView, keymap } from '@codemirror/view';
import { EditorState } from '@codemirror/state';
import { autocompletion, type Completion, type CompletionContext } from '@codemirror/autocomplete';
import { linter, type Diagnostic } from '@codemirror/lint';
import { indentWithTab } from '@codemirror/commands';
import { basicSetup } from 'codemirror';
import { python } from '@codemirror/lang-python';

export interface EditorField {
  name: string;
  doc: string;
  backtestable: boolean;
}

export interface EditorDiagnostic {
  line: number;               // 1-based; 0 when the backend gave no line
  message: string;
  severity: 'error' | 'warning';
}

/** Hook skeletons offered at the start of a line — the fastest way to learn
 *  the surface without leaving the editor. */
const HOOK_SNIPPETS: { label: string; detail: string; body: string }[] = [
  {
    label: 'decide',
    detail: 'crypto Up/Down window entries',
    body: 'def decide(ctx):\n    return None\n',
  },
  {
    label: 'decide_market',
    detail: 'general Polymarket US markets',
    body: 'def decide_market(market):\n    return None\n',
  },
  {
    label: 'manage',
    detail: 'dynamic exits on open positions',
    body: 'def manage(position, ctx):\n    return None\n',
  },
  {
    label: 'supervise',
    detail: 'change whitelisted config',
    body: 'def supervise(app):\n    return None\n',
  },
  {
    label: 'on_start',
    detail: 'once when the script loads',
    body: 'def on_start(state):\n    pass\n',
  },
  {
    label: 'on_fill',
    detail: 'an entry order filled',
    body: 'def on_fill(position, state):\n    pass\n',
  },
  {
    label: 'on_settle',
    detail: 'a position exited or settled',
    body: 'def on_settle(position, state):\n    pass\n',
  },
];

const MARKET_FIELDS = [
  'ticker', 'eventTicker', 'title', 'category', 'slug', 'closeTime', 'status',
  'yesBid', 'yesAsk', 'noBid', 'noAsk', 'yesMid', 'noMid', 'lastPrice',
  'prevPrice', 'priceChange', 'spreadCents', 'volume', 'volume24h',
  'openInterest', 'portfolio',
];

const POSITION_FIELDS = [
  'ticker', 'asset', 'side', 'contracts', 'avgEntryCents', 'curBidCents',
  'minsLeft', 'tpPct', 'slCents', 'unrealizedPct',
];

const SIGNAL_FIELDS = [
  'source', 'ticker', 'category', 'title', 'price', 'side', 'dollarValue',
  'confidence', 'edgePts', 'costCents', 'signalType', 'hourUtc', 'createdAt',
];

/** Which dict is being subscripted right before the cursor, if any. */
const SUBSCRIPT_RE = /(ctx|market|position|signal|app|state)\s*(?:\[|\.get\(\s*)\s*(["'])([A-Za-z0-9_]*)$/;

// Krypt-toned CodeMirror theme: fully self-contained (CSP forbids CDN/fonts),
// matched to the tailwind krypt-* palette used everywhere else.
const kryptTheme = EditorView.theme(
  {
    '&': {
      backgroundColor: 'transparent',
      color: '#e5e7eb',
      fontSize: '12.5px',
      height: '100%',
    },
    '.cm-content': {
      fontFamily: "'JetBrains Mono', ui-monospace, monospace",
      caretColor: '#a78bfa',
    },
    '.cm-cursor, .cm-dropCursor': { borderLeftColor: '#a78bfa' },
    '&.cm-focused': { outline: 'none' },
    '.cm-gutters': {
      backgroundColor: 'transparent',
      color: 'rgba(148,163,184,0.45)',
      border: 'none',
    },
    '.cm-activeLine': { backgroundColor: 'rgba(255,255,255,0.03)' },
    '.cm-activeLineGutter': { backgroundColor: 'rgba(255,255,255,0.04)' },
    '.cm-selectionBackground, &.cm-focused .cm-selectionBackground': {
      backgroundColor: 'rgba(139,92,246,0.25) !important',
    },
    '.cm-matchingBracket': { backgroundColor: 'rgba(139,92,246,0.3)' },
    // Autocomplete + lint surfaces, themed to match the rest of the app
    // (the defaults are light-mode and unreadable on the dark editor).
    '.cm-tooltip': {
      backgroundColor: '#12121a',
      border: '1px solid rgba(139,92,246,0.35)',
      borderRadius: '8px',
      color: '#e5e7eb',
    },
    '.cm-tooltip-autocomplete > ul > li': { padding: '3px 8px' },
    '.cm-tooltip-autocomplete > ul > li[aria-selected]': {
      backgroundColor: 'rgba(139,92,246,0.25)',
      color: '#fff',
    },
    '.cm-completionDetail': { color: 'rgba(148,163,184,0.7)', fontStyle: 'normal' },
    '.cm-completionInfo': {
      backgroundColor: '#12121a',
      border: '1px solid rgba(139,92,246,0.35)',
      borderRadius: '8px',
      maxWidth: '320px',
      padding: '6px 8px',
    },
    '.cm-diagnostic-error': { borderLeftColor: '#f87171' },
    '.cm-diagnostic-warning': { borderLeftColor: '#fbbf24' },
    '.cm-lintRange-error': { backgroundImage: 'none', textDecoration: 'underline wavy #f87171' },
    '.cm-lintRange-warning': { backgroundImage: 'none', textDecoration: 'underline wavy #fbbf24' },
  },
  { dark: true },
);

export function ScriptEditor({
  value, onChange, readOnly = false, fields, lintSource,
}: {
  value: string;
  onChange: (code: string) => void;
  readOnly?: boolean;
  /** ctx vocabulary, straight from the backend so it can't drift. */
  fields?: EditorField[];
  /** Debounced validation — the editor calls this and shows the result inline. */
  lintSource?: (code: string) => Promise<EditorDiagnostic[]>;
}) {
  const hostRef = useRef<HTMLDivElement>(null);
  const viewRef = useRef<EditorView | null>(null);
  const onChangeRef = useRef(onChange);
  onChangeRef.current = onChange;
  // Read through refs so new docs/validators don't force an editor rebuild
  // (which would drop the cursor and undo history mid-edit).
  const fieldsRef = useRef(fields);
  fieldsRef.current = fields;
  const lintRef = useRef(lintSource);
  lintRef.current = lintSource;

  useEffect(() => {
    if (!hostRef.current) return;

    const complete = (context: CompletionContext) => {
      const line = context.state.doc.lineAt(context.pos);
      const before = line.text.slice(0, context.pos - line.from);

      // ctx["…"] / market.get("…") → the real field vocabulary.
      const sub = SUBSCRIPT_RE.exec(before);
      if (sub) {
        const [, dictName, , typed] = sub;
        let names: string[];
        let docFor: (n: string) => string | undefined = () => undefined;
        if (dictName === 'market') {
          names = MARKET_FIELDS;
        } else if (dictName === 'position') {
          names = POSITION_FIELDS;
        } else if (dictName === 'signal') {
          names = SIGNAL_FIELDS;
        } else if (dictName === 'ctx') {
          const fs = fieldsRef.current ?? [];
          names = [...fs.map((f) => f.name), 'portfolio'];
          docFor = (n) => fs.find((f) => f.name === n)?.doc;
        } else {
          return null;   // state / app — user-defined or documented elsewhere
        }
        return {
          from: context.pos - typed.length,
          options: names.map<Completion>((n) => ({
            label: n,
            type: 'property',
            detail: dictName,
            info: docFor(n),
          })),
          validFor: /^[A-Za-z0-9_]*$/,
        };
      }

      // Start of a line (module level) → hook skeletons.
      const word = context.matchBefore(/[A-Za-z_][A-Za-z0-9_]*$/);
      if (word && !/\S/.test(line.text.slice(0, word.from - line.from))) {
        return {
          from: word.from,
          options: HOOK_SNIPPETS.map<Completion>((h) => ({
            label: h.label,
            type: 'function',
            detail: h.detail,
            apply: h.body,
          })),
          validFor: /^[A-Za-z0-9_]*$/,
        };
      }
      return null;
    };

    const lint = linter(async (view): Promise<Diagnostic[]> => {
      const fn = lintRef.current;
      if (!fn) return [];
      let found: EditorDiagnostic[];
      try {
        found = await fn(view.state.doc.toString());
      } catch {
        return [];   // backend down — never block typing over it
      }
      const lines = view.state.doc.lines;
      return found.map((d) => {
        // A diagnostic with no line still has to land somewhere visible;
        // pin it to line 1 rather than dropping it silently.
        const ln = Math.min(Math.max(d.line || 1, 1), lines);
        const l = view.state.doc.line(ln);
        return {
          from: l.from, to: l.to,
          severity: d.severity,
          message: d.message,
        };
      });
    }, { delay: 600 });

    const view = new EditorView({
      parent: hostRef.current,
      state: EditorState.create({
        doc: value,
        extensions: [
          basicSetup,
          python(),
          kryptTheme,
          autocompletion({ override: [complete] }),
          lint,
          keymap.of([indentWithTab]),
          EditorState.readOnly.of(readOnly),
          EditorView.updateListener.of((u) => {
            if (u.docChanged) onChangeRef.current(u.state.doc.toString());
          }),
        ],
      }),
    });
    viewRef.current = view;
    return () => {
      view.destroy();
      viewRef.current = null;
    };
    // Recreate only when readOnly flips — `value` sync is handled below so
    // typing doesn't rebuild the editor on every keystroke.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [readOnly]);

  // External value swap (selecting another script) — replace the doc.
  useEffect(() => {
    const view = viewRef.current;
    if (!view) return;
    const cur = view.state.doc.toString();
    if (cur !== value) {
      view.dispatch({ changes: { from: 0, to: cur.length, insert: value } });
    }
  }, [value]);

  return (
    <div
      ref={hostRef}
      className="h-full min-h-[280px] overflow-auto rounded-lg border border-krypt-border bg-krypt-void/50 [&_.cm-editor]:h-full"
    />
  );
}
