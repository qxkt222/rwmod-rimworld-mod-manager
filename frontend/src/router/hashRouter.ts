/**
 * Minimal hash router (docs/ui-redesign.md §3, option B — zero dependencies).
 *
 * Contract:
 *  - `#<canonical-panel>` is the only URL shape;
 *  - `#updates` resolves to `queue` and the address bar is rewritten in place;
 *  - empty / unknown hashes fall back to `dashboard`, also with `replaceState`
 *    (loading a bad deep link must not pollute the back stack);
 *  - `hashchange` and `popstate` both feed one sync path, deduplicated by the
 *    resolved panel, so a single back/forward never renders twice.
 */
import { useSyncExternalStore } from "react";

import { DEFAULT_PANEL, resolvePanel, type PanelName } from "../panel-registry";
import { ROUTES, type RouteDef } from "./routes";

export interface RouteState {
  panel: PanelName;
  route: RouteDef;
}

export interface HashResolution {
  panel: PanelName;
  /** True when the raw hash already equals the canonical panel name. */
  canonical: boolean;
}

type Listener = () => void;

const listeners = new Set<Listener>();
let current: PanelName = DEFAULT_PANEL;
let started = false;

/** Pure parser: raw hash (with or without `#`) → canonical panel. */
export function resolveHash(rawHash: string): HashResolution {
  const key = rawHash.startsWith("#") ? rawHash.slice(1) : rawHash;
  const panel = resolvePanel(key);
  if (panel) return { panel, canonical: key === panel };
  return { panel: DEFAULT_PANEL, canonical: false };
}

function notify(): void {
  for (const listener of [...listeners]) listener();
}

function setPanel(panel: PanelName): void {
  if (panel === current) return;
  current = panel;
  notify();
}

/**
 * Read the URL, normalise aliases/unknown hashes in place, and publish the
 * resolved panel. Returns the panel now active.
 */
export function syncFromLocation(): PanelName {
  const resolution = resolveHash(window.location.hash);
  if (!resolution.canonical) {
    window.history.replaceState({ panel: resolution.panel }, "", `#${resolution.panel}`);
  }
  setPanel(resolution.panel);
  return current;
}

/** Navigate to a panel. `replace` swaps the current entry instead of pushing. */
export function navigate(panel: PanelName, options: { replace?: boolean } = {}): void {
  if (!options.replace && panel === current && window.location.hash === `#${panel}`) return;

  if (options.replace) {
    window.history.replaceState({ panel }, "", `#${panel}`);
  } else {
    window.history.pushState({ panel }, "", `#${panel}`);
  }
  setPanel(panel);
}

/** Subscribe to panel changes (also used by `useSyncExternalStore`). */
export function subscribe(listener: Listener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function getCurrentPanel(): PanelName {
  return current;
}

/** Current route snapshot — `useRoute().panel` is the render key. */
export function useRoute(): RouteState {
  const panel = useSyncExternalStore(subscribe, getCurrentPanel, () => DEFAULT_PANEL);
  return { panel, route: ROUTES[panel] };
}

function handleLocationChange(): void {
  syncFromLocation();
}

/** Start listening. Called once on module load; exported for tests. */
export function startRouter(): void {
  if (started || typeof window === "undefined") return;
  started = true;
  window.addEventListener("hashchange", handleLocationChange);
  window.addEventListener("popstate", handleLocationChange);
  syncFromLocation();
}

/** Stop listening (tests / hot reload). */
export function stopRouter(): void {
  if (!started) return;
  started = false;
  window.removeEventListener("hashchange", handleLocationChange);
  window.removeEventListener("popstate", handleLocationChange);
}

if (typeof window !== "undefined") startRouter();
