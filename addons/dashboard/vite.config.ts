import path from 'node:path'
import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vitest/config'

export default defineConfig(({ command }) => ({
  // Absolute asset paths: the app owns real paths (/w/DEMO/settings/addon/x), so a reload there must still find /assets.
  base: '/',
  plugins: [react(), tailwindcss(), {
    name: 'production-entry',
    transformIndexHtml: { order: 'pre', handler(html) {
      if (command !== 'build') return html
      return html.replace('/src/main.tsx', '/src/main.prod.tsx')
        .replace(/    <link rel="preconnect"[^>]*>\n/g, '')
        .replace(/    <link\s+href="https:\/\/fonts\.googleapis\.com[\s\S]*?\/>\n/g, '')
    } },
    generateBundle() {
      for (const id of this.getModuleIds()) {
        if (id.includes('/src/mocks/') || id.includes('/api/mockApi.')) throw new Error(`Mock module in production: ${id}`)
      }
    },
  }],
  resolve: {
    alias: [
      // The production build swaps the api module (and so every page's `api`) for the real one; dev, tests and the
      // layout guard keep the in-process mock. The module-graph check above fails the build if a mock slips in.
      ...(command === 'build' ? [{ find: /^@\/api\/client$/, replacement: path.resolve(import.meta.dirname, './src/api/client.prod.ts') }] : []),
      { find: '@', replacement: path.resolve(import.meta.dirname, './src') },
    ],
  },
  build: {
    rolldownOptions: {
      output: {
        // Stable vendor chunks: the entry stays small and the framework code caches across page-chunk changes.
        codeSplitting: {
          groups: [
            { name: 'react-vendor', test: /node_modules[\\/](react|react-dom|scheduler)[\\/]/, priority: 30 },
            { name: 'router-vendor', test: /node_modules[\\/]@tanstack[\\/]/, priority: 20 },
            { name: 'ui-vendor', test: /node_modules[\\/](radix-ui|@radix-ui|cmdk|sonner|lucide-react)[\\/]/, priority: 10 },
          ],
        },
      },
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test-setup.ts'],
    css: false,
    // Headroom for loaded machines: jsdom renders are CPU-bound, and with several runs/CPU-starved workers even 300 ms tests passed 5 s.
    testTimeout: 10000,
  },
}))
