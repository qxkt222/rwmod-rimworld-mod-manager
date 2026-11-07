/**
 * Command palette data source (docs/ui-redesign.md §5.1).
 *
 * Panel commands come from `routes.ts` so label/group/icon/shortcut have one
 * source of truth; action commands add global verbs. Order is deliberate: the
 * legacy `command-palette.spec.ts` filters "下载" and expects
 * 下载 Mod → 下载合集 → 下载队列.
 */
import { api, toMessage } from "../api/client";
import { getCurrentPanel, navigate } from "../router/hashRouter";
import { ROUTE_LIST, type PanelGroup, type PanelName } from "../router/routes";
import { bump, type Topic } from "../store/bus";
import { useTransferStore } from "../store/transfer";
import { notify, useUIStore } from "../store/ui";
import { useUpdatesStore } from "../store/updates";

export type CommandGroup = PanelGroup | "动作";

export interface Command {
  id: string;
  label: string;
  group: CommandGroup;
  icon: string;
  shortcut?: string;
  run: () => void | Promise<void>;
}

/** Which bus topic a "refresh current panel" should invalidate. */
export const TOPIC_BY_PANEL: Readonly<Record<PanelName, Topic>> = {
  dashboard: "dashboard",
  search: "mods",
  download: "queue",
  collection: "queue",
  import: "queue",
  queue: "queue",
  mods: "mods",
  rimsort: "rimsort",
  profiles: "profiles",
  tags: "tags",
  history: "history",
  backups: "backups",
  saves: "saves",
  config: "config",
  settings: "settings",
};

async function exportMods(): Promise<void> {
  try {
    const data = await api.exportMods();
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = "rwmod-export.json";
    anchor.click();
    URL.revokeObjectURL(url);
    notify.success("导出完成");
  } catch (error) {
    notify.error(`导出失败: ${toMessage(error)}`);
  }
}

/** Bump the active panel's topic — the refresh button equals this. */
export function refreshCurrentPanel(): void {
  bump(TOPIC_BY_PANEL[getCurrentPanel()]);
  notify.success("已刷新");
}

export const COMMANDS: readonly Command[] = Object.freeze([
  ...ROUTE_LIST.map(
    (route): Command => ({
      id: `panel-${route.name}`,
      label: route.commandLabel ?? route.label,
      group: route.group,
      icon: route.icon,
      shortcut: route.shortcut,
      run: () => navigate(route.name),
    }),
  ),
  {
    id: "export-mods",
    label: "导出 Mod 列表",
    group: "动作",
    icon: "📤",
    run: exportMods,
  },
  {
    id: "toggle-theme",
    label: "切换深色模式",
    group: "动作",
    icon: "🌙",
    run: () => useUIStore.getState().toggleTheme(),
  },
  {
    id: "refresh-panel",
    label: "刷新当前面板",
    group: "动作",
    icon: "🔄",
    run: refreshCurrentPanel,
  },
  {
    id: "clear-log",
    label: "清空日志",
    group: "动作",
    icon: "🗑",
    run: () => useTransferStore.getState().clear("download"),
  },
  {
    id: "check-updates",
    label: "检查更新",
    group: "动作",
    icon: "🔄",
    run: () => {
      void useUpdatesStore
        .getState()
        .check()
        .catch((error: unknown) => notify.error(toMessage(error)));
    },
  },
] as const);

/** Substring filter, case-insensitive; relative order is preserved. */
export function filterCommands(query: string, commands: readonly Command[] = COMMANDS): Command[] {
  const needle = query.trim().toLowerCase();
  if (!needle) return [...commands];
  return commands.filter((command) => command.label.toLowerCase().includes(needle));
}
