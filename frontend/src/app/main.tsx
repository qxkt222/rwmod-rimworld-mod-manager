/**
 * React entry point for the redesigned UI (wave 2).
 *
 * Loaded by `frontend/index.html`, the single entry since the cutover
 * (2026-11-07). The legacy Vanilla entry no longer exists.
 */
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import "../styles/tokens.css";
import "../styles/base.css";
import "../styles/layout.css";

import { App } from "./App";
import { bootstrap } from "./bootstrap";

/**
 * Injected by vite.config.ts from `pyproject.toml` — never hardcode the
 * version here, the backend is the single source of truth.
 */
declare const __APP_VERSION__: string;
export const APP_VERSION = __APP_VERSION__;

const container = document.getElementById("app");
if (container) {
  createRoot(container).render(
    <StrictMode>
      <App version={APP_VERSION} />
    </StrictMode>,
  );
}

void bootstrap().catch((error: unknown) => {
  console.error("[rwmod] bootstrap failed", error);
});
