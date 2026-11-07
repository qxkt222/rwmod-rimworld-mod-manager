import { defineConfig } from "vitest/config";

/**
 * Component-layer test runner (Vitest + happy-dom + MSW).
 *
 * Scope: DOM-level helpers under src/ plus the React scaffold tests under
 * tests/unit/ (wave 0b). Panels remain covered end-to-end by Playwright.
 */
export default defineConfig({
  test: {
    environment: "happy-dom",
    include: ["tests/unit/**/*.test.{ts,tsx}"],
    setupFiles: ["./tests/unit/setup.ts"],
    restoreMocks: true,
    clearMocks: true,
  },
});
