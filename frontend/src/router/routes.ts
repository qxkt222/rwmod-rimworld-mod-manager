/**
 * Canonical route table — labels, groups, icons and shortcuts
 * (docs/ui-redesign.md §1.2 / §1.4).
 *
 * Panel *names* stay owned by `src/panel-registry.ts` (the old entry imports the
 * same module). This file only adds presentation metadata; the shortcut map is
 * written out explicitly instead of derived from array order, so regrouping the
 * sidebar can never silently move a user's muscle memory.
 */
import { resolvePanel, type PanelName } from "../panel-registry";

export type PanelGroup = "概览" | "获取" | "整理" | "数据" | "系统";

export interface RouteDef {
  name: PanelName;
  label: string;
  group: PanelGroup;
  icon: string;
  shortcut?: string;
  /**
   * Optional palette label when it must differ from the sidebar label.
   * `history` uses it so filtering "下载" still yields exactly the three
   * download commands required by the legacy command-palette spec.
   */
  commandLabel?: string;
}

/** Ctrl+1..8, frozen by docs/ui-redesign.md §1.4. */
export const SHORTCUTS: Readonly<Record<string, PanelName>> = Object.freeze({
  "Ctrl+1": "download",
  "Ctrl+2": "collection",
  "Ctrl+3": "import",
  "Ctrl+4": "mods",
  "Ctrl+5": "search",
  "Ctrl+6": "queue",
  "Ctrl+7": "rimsort",
  "Ctrl+8": "profiles",
});

export const SHORTCUT_BY_PANEL: Readonly<Partial<Record<PanelName, string>>> = Object.freeze(
  Object.fromEntries(
    Object.entries(SHORTCUTS).map(([shortcut, panel]) => [panel, shortcut]),
  ) as Partial<Record<PanelName, string>>,
);

export const ROUTES: Readonly<Record<PanelName, RouteDef>> = Object.freeze({
  dashboard: { name: "dashboard", label: "首页", group: "概览", icon: "📊" },
  search: { name: "search", label: "搜索 Workshop", group: "获取", icon: "🔍", shortcut: SHORTCUT_BY_PANEL.search },
  download: { name: "download", label: "下载 Mod", group: "获取", icon: "📥", shortcut: SHORTCUT_BY_PANEL.download },
  collection: { name: "collection", label: "下载合集", group: "获取", icon: "📦", shortcut: SHORTCUT_BY_PANEL.collection },
  import: { name: "import", label: "导入列表", group: "获取", icon: "📋", shortcut: SHORTCUT_BY_PANEL.import },
  queue: { name: "queue", label: "下载队列", group: "获取", icon: "📋", shortcut: SHORTCUT_BY_PANEL.queue },
  mods: { name: "mods", label: "已安装 Mods", group: "整理", icon: "📊", shortcut: SHORTCUT_BY_PANEL.mods },
  rimsort: { name: "rimsort", label: "RimSort", group: "整理", icon: "📐", shortcut: SHORTCUT_BY_PANEL.rimsort },
  profiles: { name: "profiles", label: "配置档案", group: "整理", icon: "💾", shortcut: SHORTCUT_BY_PANEL.profiles },
  tags: { name: "tags", label: "标签", group: "整理", icon: "🏷" },
  history: {
    name: "history",
    label: "下载历史",
    commandLabel: "历史记录",
    group: "数据",
    icon: "📜",
  },
  backups: { name: "backups", label: "备份", group: "数据", icon: "💾" },
  saves: { name: "saves", label: "存档分析", group: "数据", icon: "🗂" },
  config: { name: "config", label: "配置", group: "系统", icon: "⚙" },
  settings: { name: "settings", label: "Mod 设置", group: "系统", icon: "🛠" },
});

export interface RouteGroup {
  group: PanelGroup;
  panels: readonly PanelName[];
}

/** Sidebar order — one navigation source, five groups, 15 entries. */
export const SIDEBAR_GROUPS: readonly RouteGroup[] = Object.freeze([
  { group: "概览", panels: ["dashboard"] },
  { group: "获取", panels: ["search", "download", "collection", "import", "queue"] },
  { group: "整理", panels: ["mods", "rimsort", "profiles", "tags"] },
  { group: "数据", panels: ["history", "backups", "saves"] },
  { group: "系统", panels: ["config", "settings"] },
]);

/** Flattened sidebar order (commands use it so palette order matches the UI). */
export const ROUTE_LIST: readonly RouteDef[] = SIDEBAR_GROUPS.flatMap(({ panels }) =>
  panels.map((panel) => ROUTES[panel]),
);

/** Every canonical panel, in sidebar order. */
export const PANEL_ORDER: readonly PanelName[] = ROUTE_LIST.map((route) => route.name);

export function routeFor(panel: PanelName): RouteDef {
  return ROUTES[panel];
}

export { resolvePanel };
export type { PanelName };
