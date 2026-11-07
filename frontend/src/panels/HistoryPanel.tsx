/**
 * History panel — download history, aggregate stats and the auto-update runner.
 *
 * Parity contract with the legacy `src/panels/history.ts` and
 * `history.spec.ts`: `#history-list`, `#history-stats`, `#btn-history-refresh`,
 * `#btn-clear-history`, `#btn-auto-update`, `#auto-update-status`. The
 * canonical testids (`list-history`, `status-history-stats`, …) are emitted
 * alongside them.
 *
 * Auto-update state lives in the shared `updates` slice (FE-1); this panel
 * only mirrors its running flag into the legacy status line.
 */
import { useCallback, useEffect, useState } from "react";

import { isConflict, toMessage } from "../api/client";
import { EmptyState } from "../components/EmptyState";
import { ErrorState } from "../components/ErrorState";
import { LoadingState } from "../components/LoadingState";
import { useRefreshOn } from "../store/bus";
import { useHistoryStore } from "../store/history";
import { notify } from "../store/ui";
import { AUTO_UPDATE_CONFLICT_MESSAGE, useUpdatesStore } from "../store/updates";

const STATUS_ICONS: Record<string, string> = {
  success: "✅",
  failed: "❌",
  skipped: "⏭️",
  pending: "⏳",
};

const rowStyle = { display: "flex", gap: 10, alignItems: "flex-start", padding: "6px 4px" } as const;
const metaStyle = {
  display: "flex",
  gap: 10,
  flexWrap: "wrap",
  color: "var(--gray-text)",
  fontSize: 11,
} as const;

export function HistoryPanel() {
  const items = useHistoryStore((state) => state.items);
  const stats = useHistoryStore((state) => state.stats);
  const status = useHistoryStore((state) => state.status);
  const error = useHistoryStore((state) => state.error);
  const load = useHistoryStore((state) => state.load);
  const clear = useHistoryStore((state) => state.clear);

  const refreshStatus = useUpdatesStore((state) => state.refreshStatus);
  const runAutoUpdate = useUpdatesStore((state) => state.runAutoUpdate);

  const [autoText, setAutoText] = useState("就绪");
  const [autoBusy, setAutoBusy] = useState(false);

  const refresh = useCallback(() => {
    void load().catch(() => {
      /* the store keeps the error for the ErrorState */
    });
  }, [load]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  useRefreshOn("history", refresh);

  useEffect(() => {
    void refreshStatus().then(() => {
      const running = useUpdatesStore.getState().autoUpdateRunning;
      setAutoText(running ? "⏳ 检查中..." : "就绪");
    });
  }, [refreshStatus]);

  const onClear = async () => {
    try {
      await clear();
      notify.success("历史记录已清空");
    } catch (clearError) {
      notify.error(`清空失败: ${toMessage(clearError)}`);
    }
  };

  const onAutoUpdate = async () => {
    setAutoBusy(true);
    setAutoText("⏳ 正在检查...");
    try {
      const result = await runAutoUpdate();
      notify.success(`自动更新: ${result.outdated ?? 0} 个 Mod 已加入队列`);
      await refreshStatus();
      setAutoText(useUpdatesStore.getState().autoUpdateRunning ? "⏳ 检查中..." : "就绪");
    } catch (updateError) {
      if (isConflict(updateError)) {
        notify.info(AUTO_UPDATE_CONFLICT_MESSAGE);
        // A 409 means another runner owns the check — stay in "checking".
        await refreshStatus();
        setAutoText(useUpdatesStore.getState().autoUpdateRunning ? "⏳ 检查中..." : "就绪");
        return;
      }
      setAutoText(`失败: ${toMessage(updateError)}`);
      notify.error(`检查失败: ${toMessage(updateError)}`);
    } finally {
      setAutoBusy(false);
    }
  };

  const showEmpty = status !== "loading" && items.length === 0;

  return (
    <>
      <div className="card" style={{ flex: 1, overflowY: "auto" }}>
        <div className="card-header" style={{ justifyContent: "space-between" }}>
          <span>📜 下载历史</span>
          <div style={{ display: "flex", gap: 6 }}>
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              id="btn-history-refresh"
              data-testid="action-history-refresh"
              onClick={refresh}
            >
              🔄 刷新
            </button>
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              id="btn-clear-history"
              data-testid="action-history-clear"
              onClick={() => void onClear()}
            >
              清空历史
            </button>
          </div>
        </div>

        <div id="history-list" data-testid="list-history" style={{ fontSize: 12 }}>
          {status === "loading" && items.length === 0 ? (
            <LoadingState message="加载中..." testid="status-history-loading" />
          ) : status === "error" && items.length === 0 ? (
            <ErrorState
              message={error ?? "加载失败"}
              onRetry={refresh}
              testid="status-history-error"
            />
          ) : showEmpty ? (
            <EmptyState title="暂无下载记录" testid="status-history-empty" />
          ) : (
            items.map((item, index) => (
              <div
                className="mod-row"
                key={`${item.id}-${item.workshop_id}-${index}`}
                data-testid={`item-history-${item.id ?? index}`}
                style={rowStyle}
              >
                <div className="mod-icon" aria-hidden="true">
                  {STATUS_ICONS[item.status ?? ""] ?? "❓"}
                </div>
                <div className="mod-info" style={{ minWidth: 0 }}>
                  <div className="mod-name">
                    Workshop {item.workshop_id}
                    {item.mod_name ? ` — ${item.mod_name}` : ""}
                  </div>
                  <div className="mod-meta" style={metaStyle}>
                    <span>{item.status}</span>
                    <span>{item.created_at}</span>
                    {item.msg ? <span>{item.msg}</span> : null}
                  </div>
                </div>
              </div>
            ))
          )}
        </div>
      </div>

      <div className="card">
        <div className="card-header">📊 统计</div>
        <div
          id="history-stats"
          data-testid="status-history-stats"
          style={{ display: "flex", gap: 16, fontSize: 12, color: "var(--gray-text)" }}
        >
          <span>
            总计 <b>{stats.total ?? 0}</b>
          </span>
          <span style={{ color: "var(--green)" }}>
            成功 <b>{stats.success ?? 0}</b>
          </span>
          <span style={{ color: "var(--red)" }}>
            失败 <b>{stats.failed ?? 0}</b>
          </span>
        </div>
      </div>

      <div className="card">
        <div className="card-header" style={{ justifyContent: "space-between" }}>
          <span>🔄 自动更新</span>
          <button
            type="button"
            className="btn btn-primary btn-sm"
            id="btn-auto-update"
            data-testid="action-auto-update"
            disabled={autoBusy}
            onClick={() => void onAutoUpdate()}
          >
            ⬇ 检查并更新
          </button>
        </div>
        <div
          id="auto-update-status"
          data-testid="status-auto-update"
          style={{ fontSize: 12, color: "var(--gray-text)" }}
        >
          {autoText}
        </div>
      </div>
    </>
  );
}

export default HistoryPanel;
