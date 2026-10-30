import path from 'node:path'
import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vitest/config'

let sonnerPatched = 0
const styleHooks = new Set<string>()

export default defineConfig(({ command }) => ({
  // Absolute asset paths: the app owns real paths (/w/DEMO/settings/addon/x), so a reload there must still find /assets.
  base: '/',
  plugins: [react(), tailwindcss(), {
    // The server's CSP is `style-src 'self'`, which blocks the <style> element sonner injects when it loads. Its
    // stylesheet is imported in tokens.css instead (built into the page's CSS), and the injection becomes a no-op.
    name: 'sonner-no-inline-style',
    transform(code, id) {
      if (!/node_modules\/sonner\/dist\/index\.mjs$/.test(id)) return null
      const out = code.replace(/^__insertCSS\(/m, 'void (')
      if (out === code) throw new Error('sonner no longer calls __insertCSS at its top level: update vite.config.ts')
      sonnerPatched += 1
      return { code: out, map: null }
    },
    // A build that bundles sonner must have patched exactly one module: a renamed entry file would otherwise pass silently
    // and the CSP would block sonner's injected <style> at runtime.
    buildEnd() {
      if (command !== 'build') return
      const inGraph = [...this.getModuleIds()].some((id) => /node_modules\/sonner\//.test(id))
      if (inGraph && sonnerPatched !== 1) throw new Error(`sonner is bundled but ${sonnerPatched} modules were patched (expected 1): its entry path changed, update vite.config.ts`)
    },
  }, {
    // The server's CSP is `style-src 'self'`: a <style> element with text, or a style="" attribute, is blocked (B11 34).
    // Three libraries inject one anyway (react-style-singleton for the scroll lock of every modal dialog, and xterm's
    // DOM renderer for its two per-terminal rules). Their css goes into constructable stylesheets instead (src/lib/cssom.ts),
    // which the CSP allows (xterm's per-cell style attributes become el.style writes); the element they would have used becomes a <span>, because even an empty <style> is blocked. Each replacement must
    // match exactly once: a library update that moves the code fails the build instead of silently re-opening the hole.
    name: 'csp-no-inline-style',
    transform(code, id) {
      const set = 'globalThis.__orchSetCss'
      const swap = (from: string, to: string) => {
        if (code.split(from).length !== 2) throw new Error(`csp-no-inline-style: ${id} no longer contains ${JSON.stringify(from)} exactly once: update vite.config.ts`)
        code = code.replace(from, to)
      }
      if (/node_modules\/@xterm\/xterm\/lib\/xterm\.js$/.test(id)) {
        // The element is made a <span> (a <style> element is blocked even when empty) and its css is adopted.
        swap('this._themeStyleElement=this._document.createElement("style")', 'this._themeStyleElement=this._document.createElement("span")')
        swap('this._dimensionsStyleElement=this._document.createElement("style")', 'this._dimensionsStyleElement=this._document.createElement("span")')
        swap('this._themeStyleElement.textContent=t', `${set}(this._themeStyleElement,t)`)
        swap('this._dimensionsStyleElement.textContent=t', `${set}(this._dimensionsStyleElement,t)`)
        // The DOM renderer also colours single cells (min-contrast, 256/RGB colours) with setAttribute("style", ...), which the CSP
        // blocks (one violation per cell, hence dozens in the console). The same declarations through the CSSOM (el.style) are allowed.
        swap('e.setAttribute("style",`${e.getAttribute("style")||""}${t};`)', 'e.style.cssText=`${e.style.cssText||""}${t};`')
        if (code.includes('setAttribute("style"')) throw new Error(`csp-no-inline-style: ${id} still sets a style attribute: update vite.config.ts`)
        styleHooks.add('xterm')
        return { code, map: null }
      }
      if (/node_modules\/react-style-singleton\/dist\/es2015\/singleton\.js$/.test(id)) {
        swap("document.createElement('style')", "document.createElement('span')")
        swap('tag.appendChild(document.createTextNode(css));', `${set}(tag, css);`)
        swap('stylesheet.parentNode.removeChild(stylesheet);', 'stylesheet.parentNode.removeChild(stylesheet); globalThis.__orchDropCss(stylesheet);')
        styleHooks.add('singleton')
        return { code, map: null }
      }
      return null
    },
    buildEnd() {
      if (command !== 'build') return
      const ids = [...this.getModuleIds()]
      const need = [['xterm', /node_modules\/@xterm\/xterm\/lib\/xterm\.js$/], ['singleton', /node_modules\/react-style-singleton\/dist\/es2015\/singleton\.js$/]] as const
      for (const [name, re] of need) if (ids.some((i) => re.test(i)) && !styleHooks.has(name)) throw new Error(`csp-no-inline-style: ${name} is bundled but was not patched: update vite.config.ts`)
    },
  }, {
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
      // Focus mode's demo data (mock host only) is swapped for empty stubs in the production build, like the api module.
      ...(command === 'build' ? [
        { find: /^@\/mocks\/addons\/focus\/demo$/, replacement: path.resolve(import.meta.dirname, './src/api/focusDemo.prod.ts') },
        { find: /^@\/mocks\/addons\/focus\/(?:sources|source-extract)\.json$/, replacement: path.resolve(import.meta.dirname, './src/api/focusJson.prod.ts') },
      ] : []),
      { find: '@', replacement: path.resolve(import.meta.dirname, './src') },
    ],
  },
  build: {
    rolldownOptions: {
      // Two pages: the dashboard, and the remote client's own screens (served by orch_dashboard.remote_client on 127.0.0.1).
      input: { main: path.resolve(import.meta.dirname, 'index.html'), 'remote-client': path.resolve(import.meta.dirname, 'remote-client.html') },
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
    exclude: ['**/node_modules/**', '**/dist/**', 'e2e/**'], // the Playwright specs run under playwright, not vitest
    // Headroom for loaded machines: jsdom renders are CPU-bound, and with several runs/CPU-starved workers even 300 ms tests passed 5 s.
    testTimeout: 10000,
  },
}))
