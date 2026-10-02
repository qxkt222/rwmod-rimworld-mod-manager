/**
 * Canonical panel registry — the single source of truth for panel names.
 *
 * The name list used to live in four places (main.ts lazy-init switch, nav
 * buttons, panel divs, router.ts known-set) and had drifted: router.ts knew
 * "updates" but no `panel-updates` div and no init branch existed, so opening
 * #updates deactivated every panel and rendered a blank page with no error.
 * The update-check UI actually lives inside the queue panel, so "updates" is
 * declared an alias of "queue" here instead of a panel of its own.
 */

/** Canonical panel names, in nav order. */
export const PANEL_NAMES = [
  "dashboard",
  "download",
  "collection",
  "import",
  "mods",
  "search",
  "queue",
  "rimsort",
  "profiles",
  "history",
  "backups",
  "saves",
  "tags",
  "config",
] as const;

export type PanelName = (typeof PANEL_NAMES)[number];

/** Panel shown when the URL carries no hash or an unknown one. */
export const DEFAULT_PANEL: PanelName = "dashboard";

/**
 * Extra URL hashes that resolve to an existing panel, so old deep links keep
 * working without pretending a panel of that name exists.
 */
export const PANEL_ALIASES: Readonly<Record<string, PanelName>> = {
  updates: "queue",
};

const PANEL_NAME_SET: ReadonlySet<string> = new Set(PANEL_NAMES);

/** Resolve a raw hash/nav name to a canonical panel, or null when unknown. */
export function resolvePanel(name: string): PanelName | null {
  if (PANEL_NAME_SET.has(name)) return name as PanelName;
  return PANEL_ALIASES[name] ?? null;
}
