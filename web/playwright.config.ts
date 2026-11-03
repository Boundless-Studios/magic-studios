import { defineConfig, devices } from '@playwright/test'

// CI hands each job two kernel-allocated free ports (scripts/e2e-ports.mjs) so runs on a
// shared self-hosted runner never collide; locally the historical defaults apply.
function port(name: string, fallback: number): number {
  const value = Number(process.env[name])
  return Number.isInteger(value) && value >= 1024 && value <= 65535 ? value : fallback
}
const apiPort = port('E2E_PORT', 8787)
const webPort = port('E2E_WEB_PORT', apiPort + 1)

export default defineConfig({
  testDir: './e2e',
  fullyParallel: true,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 2 : 0,
  // The CI runner is shared with other jobs. Its default of 16 workers started every spec at
  // once, and the cached-navigation budget measured 3096 ms beside 15 other browsers but
  // 1394 ms beside two.
  workers: process.env.CI ? 4 : undefined,
  reporter: 'html',
  use: {
    baseURL: `http://127.0.0.1:${webPort}`,
    trace: 'on-first-retry',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  webServer: [
    {
      command: `uv run --extra test uvicorn magic_video_editor.local_studio:app --host 127.0.0.1 --port ${apiPort}`,
      cwd: '..',
      url: `http://127.0.0.1:${apiPort}/health`,
      reuseExistingServer: !process.env.CI,
    },
    {
      // --strictPort: a busy port must fail loudly, not let Vite drift to the next one while
      // baseURL still points at the one we asked for.
      // STUDIO_E2E_WARMUP adds /__e2e_ready, which answers once every route is compiled, so
      // no spec times Vite's first compile (see vite.config.ts).
      command: `STUDIO_API_ORIGIN=http://127.0.0.1:${apiPort} STUDIO_E2E_WARMUP=1 npm run dev -- --host 127.0.0.1 --port ${webPort} --strictPort`,
      url: `http://127.0.0.1:${webPort}/__e2e_ready`,
      timeout: 180_000,
      // A plain dev server answers /__e2e_ready with its index.html fallback, so reusing one
      // would skip the gate. Reuse only when asked, with a server started the same way.
      reuseExistingServer: !process.env.CI && Boolean(process.env.STUDIO_E2E_REUSE_SERVER),
    },
  ],
})
