import { defineConfig } from '@playwright/test'

/**
 * Playwright config for behaviour and visual regression.
 *
 * Server 1 serves the normal production build from `dist` on 4173. It needs
 * `npm run build` first.
 * Server 2 serves the showcase build from `dist-showcase` on 4174. Its command
 * builds that bundle, so `e2e/showcase.spec.ts` owns its own artifact.
 * Baselines live next to the spec in `e2e/visual.spec.ts-snapshots/`.
 */
export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  forbidOnly: Boolean(process.env.CI),
  reporter: [['list']],
  use: {
    baseURL: 'http://127.0.0.1:4173',
    trace: 'retain-on-failure',
  },
  expect: {
    toHaveScreenshot: {
      animations: 'disabled',
      caret: 'hide',
      maxDiffPixelRatio: 0,
    },
  },
  webServer: [
    {
      command: 'npm run preview -- --host 127.0.0.1 --port 4173 --strictPort',
      url: 'http://127.0.0.1:4173',
      reuseExistingServer: !process.env.CI,
      timeout: 120_000,
    },
    {
      command: 'npm run build:showcase && npm run preview:showcase',
      url: 'http://127.0.0.1:4174',
      reuseExistingServer: !process.env.CI,
      timeout: 180_000,
    },
  ],
  projects: [
    {
      name: 'chromium',
      use: { browserName: 'chromium' },
    },
  ],
})
