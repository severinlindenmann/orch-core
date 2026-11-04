import path from 'node:path'
import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

export default defineConfig({
  base: process.env.DEMO_BASE ?? '/orch-core/app/',
  plugins: [react(), tailwindcss()],
  resolve: { alias: [{ find: '@', replacement: path.resolve(import.meta.dirname, 'src') }] },
  build: {
    outDir: process.env.DEMO_OUT ?? 'dist-demo',
    emptyOutDir: true,
    rolldownOptions: {
      input: {
        main: path.resolve(import.meta.dirname, 'index.html'),
        'preview-setup': path.resolve(import.meta.dirname, 'preview-setup.html'),
      },
    },
  },
})
