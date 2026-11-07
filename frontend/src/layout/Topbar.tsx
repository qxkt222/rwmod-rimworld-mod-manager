/**
 * Global top bar: breadcrumb, live connection state, Steam state, theme,
 * command palette and refresh (docs/ui-redesign.md §1.3).
 *
 * The legacy 15-tab row is intentionally gone — navigation lives only in the
 * sidebar. Every control is addressed by its canonical testid.
 */
import { refreshCurrentPanel } from "../app/commands";
import { useRoute } from "../router/hashRouter";
import { useConfigStore } from "../store/config";
import { useQueueStore } from "../store/queue";
import { useUIStore } from "../store/ui";

export interface TopbarProps {
  version: string;
  /** Runtime status line kept for the wave-0b App smoke contract. */
  status: string;
}

export function Topbar({ version, status }: TopbarProps) {
  const { route } = useRoute();
  const theme = useUIStore((state) => state.theme);
  const toggleTheme = useUIStore((state) => state.toggleTheme);
  const togglePalette = useUIStore((state) => state.toggleCommandPalette);
  const toggleSidebar = useUIStore((state) => state.toggleSidebar);
  const wsState = useQueueStore((state) => state.wsState);
  const steamOnline = useConfigStore((state) => state.steamOnline);

  const wsLabel =
    wsState === "connected" ? "实时连接正常" : wsState === "connecting" ? "正在连接实时通道" : "实时连接已断开";
  const steamLabel =
    steamOnline === null ? "Steam API 状态未知" : steamOnline ? "Steam API 在线" : "Steam API 离线（使用本地缓存）";

  return (
    <header className="topbar" data-testid="topbar">
      <button
        type="button"
        className="nav-btn topbar__menu"
        aria-label="切换侧栏"
        onClick={toggleSidebar}
      >
        ☰
      </button>

      <div className="topbar__crumb" data-testid="status-breadcrumb">
        <span className="topbar__group">{route.group}</span>
        <span className="topbar__sep" aria-hidden="true">
          /
        </span>
        <span className="topbar__panel">{route.label}</span>
      </div>

      <h1 className="topbar__brand">
        🔧 rwmod <span className="topbar__version">v{version}</span>
      </h1>

      <div className="topbar__actions">
        <span
          data-testid="status-steam"
          className="topbar__indicator"
          title={steamLabel}
          aria-label={steamLabel}
        >
          {steamOnline === null ? "⚪" : steamOnline ? "🟢" : "🔴"}
        </span>
        <span
          className={`topbar__indicator topbar__ws topbar__ws--${wsState}`}
          data-testid="status-ws"
          data-state={wsState}
          title={wsLabel}
          aria-label={wsLabel}
        >
          ●
        </span>
        <button
          type="button"
          className="nav-btn"
          data-testid="action-command-palette"
          title="命令面板 Ctrl+K"
          aria-label="打开命令面板"
          onClick={togglePalette}
        >
          ⚡
        </button>
        <button
          type="button"
          className="nav-btn"
          data-testid="action-theme"
          title="切换主题"
          aria-label="切换主题"
          onClick={toggleTheme}
        >
          {theme === "dark" ? "☀️" : "🌙"}
        </button>
        <button
          type="button"
          className="nav-btn"
          data-testid="action-refresh"
          title="刷新当前面板"
          aria-label="刷新当前面板"
          onClick={refreshCurrentPanel}
        >
          🔄
        </button>
      </div>

      <span className="sr-only" data-testid="status-app" role="status">
        {status}
      </span>
    </header>
  );
}
