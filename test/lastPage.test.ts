import { beforeEach, describe, expect, it } from 'vitest';
import {
  FRONT_DOOR, LAST_PAGE_KEY, PAGE_IDS, forgetLastPage, isPageId, loadLastPage, saveLastPage,
} from '../src/state/lastPage';

/**
 * Where the app opens. A new install lands on AI Agents (the front door); a
 * returning user lands where they left off; a stored id this build doesn't
 * know (a renamed page, a hand edit) is forgotten rather than trusted.
 */
describe('lastPage', () => {
  beforeEach(() => localStorage.clear());

  it('a fresh install opens on the front door, AI Agents', () => {
    expect(FRONT_DOOR).toBe('agents');
    expect(loadLastPage()).toBe('agents');
  });

  it('a returning user opens where they left off', () => {
    saveLastPage('track');
    expect(loadLastPage()).toBe('track');
  });

  it('an unknown stored id is dropped, not trusted', () => {
    localStorage.setItem(LAST_PAGE_KEY, 'aiAgents');
    expect(loadLastPage()).toBe('agents');
    expect(localStorage.getItem(LAST_PAGE_KEY)).toBeNull();
  });

  it('forgetting a crashed page sends the next launch to the front door', () => {
    saveLastPage('market');
    forgetLastPage();
    expect(loadLastPage()).toBe('agents');
  });

  it('knows every page, and nothing else', () => {
    expect(new Set(PAGE_IDS).size).toBe(PAGE_IDS.length);
    expect(PAGE_IDS).toContain('agents');
    expect(PAGE_IDS).toContain('visualizer');
    expect(isPageId('agents')).toBe(true);
    expect(isPageId('nope')).toBe(false);
    expect(isPageId(null)).toBe(false);
  });
});
