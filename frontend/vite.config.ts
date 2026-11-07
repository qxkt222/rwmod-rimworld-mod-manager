import react from "@vitejs/plugin-react";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";

/**
 * Version single source: `pyproject.toml` (the same value the backend exposes).
 * Injected at build time so the React bundle never hardcodes a version.
 */
function readAppVersion(): string {
  try {
    const pyproject = readFileSync(
      fileURLToPath(new URL("../pyproject.toml", import.meta.url)),
      "utf8",
    );
    return /^version\s*=\s*"([^"]+)"/m.exec(pyproject)?.[1] ?? "0.0.0";
  } catch {
    return "0.0.0";
  }
}

const APP_VERSION = readAppVersion();

export default defineConfig({
  root: ".",
  define: {
    __APP_VERSION__: JSON.stringify(APP_VERSION),
  },
  // React is the only frontend entry (cutover 2026-11-07): index.html loads
  // src/app/main.tsx and is the single HTML page emitted to ../static.
  plugins: [react()],
  build: {
    outDir: "../static",
    emptyOutDir: true,
    // Single entry: the FastAPI bundle serves static/ at /static/** and
    // assets/ at /assets/**, and `/` returns index.html.
    rollupOptions: {
      input: "index.html",
      // Keep the React runtime in its own vendor chunk. Dynamic `import()`
      // still splits every panel; the former panel-registry chunk existed
      // only to isolate the two entries and is no longer needed.
      output: {
        manualChunks(id: string) {
          if (id.includes("node_modules")) return "vendor-react";
        },
      },
    },
    // Enable minification with target ES2022
    target: "es2022",
    minify: "esbuild",
    // Generate smaller sourcemaps for production
    sourcemap: false,
  },
  server: {
    port: 5173,
    proxy: {
      "/api": "http://localhost:8000",
      "/ws": { target: "ws://localhost:8000", ws: true },
    },
  },
});
