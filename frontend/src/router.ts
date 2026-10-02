/**
 * Client-side router using History API.
 *
 * Panel names map to URL hashes (e.g., #download, #mods).
 * Clicking a nav tab updates the URL; browser back/forward
 * restores the correct panel.
 *
 * Usage:
 *   import { initRouter } from "./router";
 *   initRouter(onPanelChange);
 */
import { DEFAULT_PANEL, resolvePanel, type PanelName } from "./panel-registry";

type PanelChangeHandler = (panel: PanelName) => void;

/**
 * Initialize client-side routing.
 *
 * @param onPanelChange — called when the active panel changes
 * @returns a cleanup function
 */
export function initRouter(onPanelChange: PanelChangeHandler): () => void {
  // ── listen for nav clicks (delegated to document) ────────────
  const clickHandler = (e: MouseEvent) => {
    const target = (e.target as HTMLElement).closest(
      "[data-panel]",
    ) as HTMLElement | null;
    if (!target) return;

    const raw = target.dataset.panel;
    if (!raw) return;
    const panel = resolvePanel(raw);
    // Unknown data-panel → ignore the click instead of navigating to a name
    // that has no DOM (the old #updates blank-page failure mode).
    if (!panel) return;

    e.preventDefault();
    navigate(panel);
  };

  document.addEventListener("click", clickHandler);

  // ── listen for browser back/forward ──────────────────────────
  const popHandler = () => {
    onPanelChange(readPanelFromHash() ?? DEFAULT_PANEL);
  };

  window.addEventListener("popstate", popHandler);

  // ── initial load — restore from URL or default ───────────────
  const initial = readPanelFromHash() ?? DEFAULT_PANEL;
  replaceState(initial); // don't push a new history entry on load

  // Trigger initial panel
  onPanelChange(initial);

  return () => {
    document.removeEventListener("click", clickHandler);
    window.removeEventListener("popstate", popHandler);
  };
}

/** Navigate to a panel — updates URL, triggers callback. */
export function navigate(panel: PanelName): void {
  window.history.pushState({ panel }, "", `#${panel}`);
  // popstate doesn't fire on pushState, so we manually trigger
  window.dispatchEvent(new PopStateEvent("popstate", { state: { panel } }));
}

/** Replace current history entry — used for initial load. */
function replaceState(panel: PanelName): void {
  window.history.replaceState({ panel }, "", `#${panel}`);
}

/**
 * Read the current panel from the URL hash, resolved through the shared
 * registry (aliases included); null for empty/unknown hashes.
 */
function readPanelFromHash(): PanelName | null {
  return resolvePanel(window.location.hash.slice(1)); // remove #
}
