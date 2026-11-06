import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
import path from 'node:path';

/**
 * Unit tests for the TypeScript half (renderer logic, hooks, the Agent Hub's
 * pure modules, the glass kit's behaviour). jsdom, no Electron: driving the
 * real app is the run-app skill's job (.claude/skills/run-app) and the
 * node:test e2e suite (`npm run test:unit`, e2e/*.test.mjs) stays separate.
 *
 * Tests live in test/**. The `@shared` alias matches vite.config.ts and the
 * tsconfig `paths`.
 */
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { '@shared': path.resolve(__dirname, 'shared') },
  },
  test: {
    environment: 'jsdom',
    include: ['test/**/*.test.{ts,tsx}'],
    globals: true,
    restoreMocks: true,
  },
});
