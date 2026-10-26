import { defineConfig, devices } from '@playwright/test'

// Browser spec of the Add-ons page against the production build and a fake dashboard server (e2e/fakeHost.ts).
// Run (build first): npx vite build && (first: ln -s $HOME/orch-dev/e2e/dashboard/node_modules e2e/node_modules, git-ignored) \
//   $HOME/orch-dev/e2e/dashboard/node_modules/.bin/playwright test -c e2e/playwright.config.ts
export default defineConfig({
  testDir: '.',
  testMatch: '*.spec.ts',
  workers: 1,
  timeout: 60_000,
  reporter: [['list']],
  use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 900 }, launchOptions: { args: ['--use-mock-keychain', '--password-store=basic'] } },
})
