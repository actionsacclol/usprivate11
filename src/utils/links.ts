// Centralised external links used throughout the app.

// Polymarket US. Sign-up starts on the invite page, the identity check and funding
// happen in its phone app, and API keys are created on the developer page.
export const POLYMARKET_HOME_URL = 'https://polymarket.us';
export const POLYMARKET_INVITE_CODE = 'YUHGO';
export const POLYMARKET_SIGNUP_URL = 'https://polymarket.us/join/yuhgo';
export const POLYMARKET_SQUAD_URL = 'https://polymarket.us/squad/join/0xOHjNIfwLBVzmovbjml?referrer=yuhgo';
export const POLYMARKET_DEVELOPER_URL = 'https://polymarket.us/developer';
export const POLYMARKET_SUPPORT_EMAIL = 'support@polymarket.us';

/** The public page of one market. `null` when there is no slug to link to. */
export function polymarketMarketUrl(slug: string | null | undefined): string | null {
  const s = (slug ?? '').trim();
  return s ? `https://polymarket.us/market/${encodeURIComponent(s)}` : null;
}

export const KRYPT_HOME = 'https://krypt.cc';
export const KRYPT_TOOLS = 'https://krypt.cc/tools';
export const KRYPT_TRADER_PAGE = 'https://krypt.cc/tools/polyus';
export const KRYPT_DISCORD = 'https://discord.gg/muzFKR657F';
