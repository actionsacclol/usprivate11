import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import electron from 'vite-plugin-electron';
import rendererPlugin from 'vite-plugin-electron-renderer';
import path from 'node:path';

// Krypt PolyBot US is a single main window app. The Python trading backend
// is spawned as a child process by the main process; the renderer
// talks to the backend exclusively through window.krypt.* IPC.

export default defineConfig({
  plugins: [
    react(),
    electron([
      {
        entry: 'electron/main.ts',
        vite: {
          build: {
            outDir: 'dist-electron',
            // No source maps in the shipped build — they'd expose the main-process
            // source (IPC, backend spawn, tray) in plaintext. Dev still gets maps
            // via the vite dev server.
            sourcemap: false,
            rollupOptions: {
              external: ['discord-rpc'],
            },
          },
        },
      },
      {
        entry: 'electron/preload.ts',
        onstart(options) {
          options.reload();
        },
        vite: { build: { outDir: 'dist-electron', sourcemap: false } },
      },
    ]),
    rendererPlugin(),
  ],
  resolve: {
    alias: {
      '@shared': path.resolve(__dirname, 'shared'),
    },
  },
  server: {
    port: 5180,
    strictPort: true,
  },
  clearScreen: false,
});
