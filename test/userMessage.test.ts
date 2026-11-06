import { describe, expect, it } from 'vitest';
import { encodeRpcError, parseErrorText } from '../shared/errors';
import { errorCode, userMessage } from '../src/utils/errors';

describe('errors that cross from the backend to a person', () => {
  it('peels Electron\'s wrapper and Python / JS class names', () => {
    const raw = "Error invoking remote method 'mcp:decide': Error: ValueError: No such agent order.";
    expect(parseErrorText(raw)).toEqual({ code: null, message: 'No such agent order.' });
    expect(userMessage(new Error(raw))).toBe('No such agent order.');
  });

  it('a [code] rides in the message and comes back off', () => {
    const enc = encodeRpcError('This version cannot do that.', 'unsupported');
    expect(enc).toBe('[unsupported] This version cannot do that.');
    expect(errorCode(new Error(`Error invoking remote method 'x': Error: ${enc}`))).toBe('unsupported');
    expect(encodeRpcError('m', 'bad code!')).toBe('m');
  });

  it('known codes become plain words about Polymarket US, not Kalshi', () => {
    expect(userMessage(new Error('[no_credentials] x'))).toMatch(/API key/i);
    expect(userMessage(new Error('[no_credentials] x'))).not.toMatch(/wallet/i);
    expect(userMessage(new Error('[no_credentials] x'))).not.toMatch(/kalshi/i);
  });

  it.each([
    'Kill switch is on: no new orders.',
    'Order throttle: too many orders this minute.',
    'Price collar: 91c is too far from the last price.',
    'Refused: this order would cross your own resting order.',
    'Buying NO is locked until the live check has passed.',
  ])('a backend safety message reaches the person verbatim: %s', (m) => {
    expect(userMessage(new Error(m))).toBe(m);
    expect(userMessage(new Error(`Error invoking remote method 'terminal:place': Error: ${m}`))).toBe(m);
  });

  it('transport failures become plain words', () => {
    expect(userMessage(new Error('RPC mcpStatus timed out after 20000ms'))).toMatch(/too long/);
    expect(userMessage(new Error('backend exited'))).toMatch(/stopped/);
    expect(userMessage(new Error('unknown method: healthCheck'))).toMatch(/can't do that yet/);
  });

  it('nothing usable -> the fallback', () => {
    expect(userMessage(undefined, 'fallback')).toBe('fallback');
    expect(userMessage({ error: 'from a result object' })).toBe('from a result object');
  });
});
