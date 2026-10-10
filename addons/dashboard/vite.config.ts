import path from 'node:path'
import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vitest/config'

export default defineConfig({
  // Absolute asset paths: the app owns real paths (/w/DEMO/settings/addon/x), so a reload there must still find /assets.
  base: '/',
  plugins: [react(), tailwindcss()],
  resolve: { alias: { '@': path.resolve(import.meta.dirname, './src') } },
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
})
