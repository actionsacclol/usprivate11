import { describe, expect, it } from 'vitest';
import { apiKeyProblem } from '../src/utils/apiKey';
import { polymarketMarketUrl } from '../src/utils/links';
import { polymarketUrl } from '../src/utils/polymarket';

const KEY_ID = '123e4567-e89b-12d3-a456-426614174000';
const SECRET = 'q83vEjK1mZ0oXn7yWv2l5s8uYc4tRb6dHf9pLa3NwGk=';

describe('a pasted API key', () => {
  it('accepts a UUID key id and a base64 secret', () => {
    expect(apiKeyProblem(KEY_ID, SECRET)).toBeNull();
    expect(apiKeyProblem(` ${KEY_ID} `, ` ${SECRET}\n`)).toBeNull();
  });

  it('says which half is wrong', () => {
    expect(apiKeyProblem('not-a-uuid', SECRET)).toMatch(/Key ID/);
    expect(apiKeyProblem(KEY_ID, 'short')).toMatch(/Secret Key/);
    expect(apiKeyProblem(KEY_ID, 'spaces are not base64 at all, not even a little')).toMatch(/Secret Key/);
  });
});

describe('market links', () => {
  it('builds the Polymarket US page from the slug', () => {
    expect(polymarketMarketUrl('btc-updown-15m-1730000000')).toBe('https://polymarket.us/market/btc-updown-15m-1730000000');
    expect(polymarketMarketUrl('')).toBeNull();
    expect(polymarketMarketUrl(null)).toBeNull();
  });

  it('drops the side suffix the engines carry on a ticker', () => {
    expect(polymarketUrl({ ticker: 'btc-updown-15m-1730000000#yes' })).toBe('https://polymarket.us/market/btc-updown-15m-1730000000');
    expect(polymarketUrl({})).toBe('');
  });
});
