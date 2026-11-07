import { defineConfig, devices } from "@playwright/test";

/**
 * Playwright E2E configuration.
 *
 * There is no `webServer` block on purpose: the backend must boot with a
 * *random* port and a throwaway HOME, so each worker starts it through
 * tests/fixtures/backend.ts (see tests/e2e/helpers.ts). Tests navigate with
 * the `backend.baseURL` fixture instead of a static `use.baseURL`.
 *
 * workers: 1 — all specs share one real backend; serial execution keeps
 * config/instance/profile state deterministic. Artifacts land in .scratch/
 * (gitignored) rather than polluting the repo root with test-results/.
 *
 * Frontend entry: React is the only entry since the cutover (2026-11-07);
 * every spec navigates to `/` (see tests/e2e/helpers.ts).
 */
const OUTPUT_DIR = "../.scratch/rwmod-react-refactor/CUTOVER/playwright-results";

export default defineConfig({
  testDir: "./tests/e2e",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 30_000,
  expect: { timeout: 8_000 },
  reporter: [["list"]],
  outputDir: OUTPUT_DIR,
  use: {
    headless: true,
    viewport: { width: 1440, height: 900 },
    actionTimeout: 10_000,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
});
