import type { PageId } from '../../App';

/**
 * The app tour: one stop per sidebar tab, in sidebar order, then the account
 * card and the tour button.
 *
 * Each step names a `data-tour` target rather than a selector or a label, so
 * restyling the sidebar or renaming a tab's text can't silently break it. A
 * test renders the Sidebar and fails if any step's target is missing, and
 * another derives the order from the Sidebar's own nav definition, so a tab
 * added later without a stop here fails the suite instead of being skipped.
 * At runtime a step whose target is not on screen (a tab hidden in Simple
 * mode) is skipped by the Tour itself.
 *
 * The copy is held to the app's rules: no feature that isn't there, no hint
 * that the bundled strategies make money (the presets measured net negative
 * in the offline backtest), and agents described as starting on paper
 * because they do.
 */

export type TourSide = 'right' | 'left' | 'bottom' | 'top';

export interface TourStep {
  /** The `data-tour` value of the element this step points at. */
  id: string;
  /** Eyebrow above the title: the sidebar group, or "Status". */
  group: string;
  title: string;
  /** One or two plain sentences: what it is for, when a beginner uses it. */
  body: string;
  /** The page "Go there" opens. Only nav steps have one. */
  page?: PageId;
  /** Where the card prefers to sit, in order. Default: right, left, bottom, top. */
  prefer?: TourSide[];
}

/** The one place the nav's `data-tour` naming lives; Sidebar uses it too. */
export const navTourId = (page: PageId): string => `nav-${page}`;

const nav = (page: PageId, group: string, title: string, body: string): TourStep => ({
  id: navTourId(page), group, title, body, page,
});

export const TOUR_STEPS: TourStep[] = [
  // AGENTS
  nav('agents', 'Agents', 'AI Agents',
    'Connect the AI agent you already use (Claude Desktop, Claude Code, Cursor, Codex, Windsurf or VS Code), or the in-app Autopilot. '
    + 'Agents start on paper, and a scoreboard checks whether their forecasts beat the market price.'),
  nav('visualizer', 'Agents', 'Agent Hub',
    'A live view of what your agents and the engines are doing: tool calls, forecasts and trades. '
    + 'It only shows activity; nothing on it places an order.'),

  // OVERVIEW
  nav('dashboard', 'Overview', 'Dashboard',
    'A snapshot of the account and the engines: balance, P&L, recent settlements and bot health.'),

  // TERMINAL
  nav('discover', 'Terminal', 'Discover',
    'Browse and search every Polymarket US market by volume, closing time or newness. '
    + 'A good first stop if you want to look around before automating anything.'),
  nav('market', 'Terminal', 'Market',
    'One market in depth: chart, order book and resolution rules, and a ticket to trade it by hand.'),
  nav('track', 'Terminal', 'Track',
    'Your own positions, resting orders and fills, whoever placed them: the paper book in Paper, your Polymarket US account in Live.'),

  // AUTOMATION
  nav('strategies', 'Automation', 'Strategies',
    'Built-in presets for the engines. None has a proven edge, so try anything on paper or in Backtest first.'),
  nav('crypto15m', 'Automation', 'Crypto',
    'Polymarket US\'s short crypto up/down markets, with their own engine and their own switch, off by default.'),
  nav('scripts', 'Automation', 'Scripts',
    'Write your own strategies in Python, or have an AI draft them, then backtest them and run them under money rails. '
    + 'Live scripts have their own switch, off by default.'),
  nav('positions', 'Automation', 'Bot positions',
    'The engines\' own ledger of open and recent positions, separate from what you trade by hand.'),
  nav('backtest', 'Automation', 'Backtest',
    'Test a strategy on past markets you downloaded or this app recorded, with Polymarket US fees.'),
  nav('terminal', 'Automation', 'Cockpit HUD',
    'A full-screen heads-up view of the engines while they run.'),

  // ACCOUNT
  nav('api', 'Account', 'API Key',
    'Connect your Polymarket US account with an API key, needed only to trade Live; Paper needs none. The key stays on this computer.'),
  nav('history', 'Account', 'History',
    'The bot\'s session diary and the full ledger of its settled trades.'),
  nav('profiles', 'Account', 'Profiles',
    'Save, export and import snapshots of each engine\'s settings. A profile never carries your agents, keys or Paper/Live mode.'),
  nav('accounts', 'Account', 'Accounts',
    'Run a second, fully separate copy of the app with its own API key and settings.'),

  // APP
  nav('remote', 'App', 'Remote',
    'Pair a Discord or Telegram bot to check on the app from your phone. '
    + 'Trading from the phone is a separate permission, off by default.'),
  nav('settings', 'App', 'Settings',
    'Paper or Live for the whole app, your AI provider, and the app\'s own preferences. Changes apply immediately.'),
  nav('logs', 'App', 'Logs',
    'A live tail of the backend\'s log. The first place to look when something is not doing what you expected.'),
  nav('guide', 'App', 'Guide',
    'How the app works and what each setting does, in more depth than this tour.'),
  nav('about', 'App', 'About',
    'Version, links, support and credits.'),

  // STATUS (the account card at the foot of the sidebar), then the tour button
  {
    id: 'account', group: 'Status', title: 'Account',
    body: 'Your wallet: click it to refresh the balance, deposit, withdraw or change settings. The pill shows which book the app trades: PAPER is imaginary money on real order books, LIVE is your real Polymarket US account.',
  },
  {
    id: 'tour-info', group: 'Status', title: 'Replay this tour',
    body: 'Click this any time to take the tour again.',
  },
];
