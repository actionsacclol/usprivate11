// @vitest-environment jsdom
import { existsSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { afterEach, describe, expect, it } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { GUIDE_VIDEOS, GuideVideoLibrary, videoLength } from '../src/components/GuideVideo';

const assets = join(__dirname, '..', 'src', 'assets', 'guide');
const scripts = JSON.parse(readFileSync(join(__dirname, '..', 'tools', 'guide-videos', 'videos.json'), 'utf8')) as Array<{
  id: string;
  title: string;
  scenes: Array<{ say: string }>;
}>;

afterEach(cleanup);

describe('the guide videos', () => {
  it('ships a finished video and poster for every script, in order', () => {
    expect(GUIDE_VIDEOS.map((v) => v.id)).toEqual(scripts.map((s) => s.id));
    for (const v of GUIDE_VIDEOS) {
      expect(existsSync(join(assets, `${v.id}.mp4`)), `${v.id}.mp4`).toBe(true);
      expect(existsSync(join(assets, `${v.id}.jpg`)), `${v.id}.jpg`).toBe(true);
      expect(v.seconds).toBeGreaterThan(10);
    }
  });

  it('starts with the quickstart', () => {
    expect(GUIDE_VIDEOS[0].id).toBe('quickstart');
  });

  it('keeps the owner-required VPN and legal line in the quickstart', () => {
    const said = scripts[0].scenes.map((s) => s.say).join(' ');
    expect(said).toMatch(/turn off your VPN/);
    expect(said).toMatch(/legal for you/);
  });

  it('never promises a profit in a script', () => {
    const said = scripts.flatMap((s) => s.scenes.map((x) => x.say)).join(' ').toLowerCase();
    for (const claim of ['guaranteed', 'risk free', 'risk-free', 'free money', 'make money', 'passive income']) {
      expect(said).not.toContain(claim);
    }
  });

  it('formats a length', () => {
    expect(videoLength(65)).toBe('1:05');
    expect(videoLength(119)).toBe('1:59');
  });
});

describe('the video library', () => {
  it('opens a video, moves to the next one, and closes on Escape', () => {
    render(<GuideVideoLibrary />);
    expect(screen.queryByTestId('guide-video-player')).toBeNull();

    fireEvent.click(screen.getByTestId('guide-video-quickstart'));
    const player = screen.getByTestId('guide-video-player');
    expect(player.getAttribute('aria-label')).toBe(GUIDE_VIDEOS[0].title);
    expect(player.querySelector('video')).not.toBeNull();

    fireEvent.click(screen.getByTestId('guide-video-next'));
    expect(screen.getByTestId('guide-video-player').getAttribute('aria-label')).toBe(GUIDE_VIDEOS[1].title);

    fireEvent.keyDown(window, { key: 'Escape' });
    expect(screen.queryByTestId('guide-video-player')).toBeNull();
  });

  it('offers no next video after the last one', () => {
    render(<GuideVideoLibrary />);
    fireEvent.click(screen.getByTestId(`guide-video-${GUIDE_VIDEOS[GUIDE_VIDEOS.length - 1].id}`));
    expect(screen.queryByTestId('guide-video-next')).toBeNull();
  });
});
