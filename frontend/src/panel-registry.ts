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

/**
 * Panel shortcuts: Ctrl+1..Ctrl+8 open the panels in nav order, skipping
 * DEFAULT_PANEL (dashboard is where you already land, so it needs no key).
 *
 * Derived from PANEL_NAMES on purpose — a hand-written shortcut list is one
 * more place for panel names to drift out of sync, which is the exact bug the
 * registry was introduced to kill. Display (cmd.ts) and handling (main.ts)
 * both read this one map, so "shown but not implemented" is impossible.
 */
export const PANEL_SHORTCUTS: ReadonlyMap<string, PanelName> = new Map(
  PANEL_NAMES.filter((name) => name !== DEFAULT_PANEL)
    .slice(0, 8)
    .map((name, i) => [`Ctrl+${i + 1}`, name] as const),
);

/** The shortcut that opens `panel`, or undefined when it has none. */
export function panelShortcut(panel: PanelName): string | undefined {
  for (const [shortcut, name] of PANEL_SHORTCUTS) {
    if (name === panel) return shortcut;
  }
  return undefined;
}

const PANEL_NAME_SET: ReadonlySet<string> = new Set(PANEL_NAMES);

/** Resolve a raw hash/nav name to a canonical panel, or null when unknown. */
export function resolvePanel(name: string): PanelName | null {
  if (PANEL_NAME_SET.has(name)) return name as PanelName;
  return PANEL_ALIASES[name] ?? null;
}
