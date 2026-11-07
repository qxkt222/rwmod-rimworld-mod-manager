/**
 * Dashboard panel — wave 2 pilot (docs/ui-redesign.md §7.2 item 1).
 *
 * Behavioural parity with the legacy `src/panels/dashboard.ts`: three stats,
 * the six most recent activity rows, quick links and the 一键更新 runner that
 * polls `/api/queue` every 2s until the queue drains.
 *
 * All anchors are canonical testids (`status-dashboard-*`, `list-activity`,
 * `action-auto-update`); the legacy ids are gone.
 */
import { useCallback, useEffect, useRef, useState } from "react";

import { api, isConflict, toMessage } from "../api/client";
import type { DashboardData } from "../api/types";
import { ErrorState } from "../components/ErrorState";
import { LoadingState } from "../components/LoadingState";
import { navigate } from "../router/hashRouter";
import { useRefreshOn } from "../store/bus";
import { notify } from "../store/ui";
import { AUTO_UPDATE_CONFLICT_MESSAGE } from "../store/updates";

const IDLE_AUTO_TEXT = "⬇ 一键更新";
const RESET_DELAY_MS = 3_000;
const POLL_INTERVAL_MS = 2_000;

function formatSize(mb: number): string {
  return mb < 1024 ? `${mb} MB` : `${(mb / 1024).toFixed(1)} GB`;
}

export function DashboardPanel() {
  const [data, setData] = useState<DashboardData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [autoText, setAutoText] = useState(IDLE_AUTO_TEXT);
  const [autoDisabled, setAutoDisabled] = useState(false);

  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const resetRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const load = useCallback(async () => {
    try {
      const next = await api.getDashboard();
      setData(next);
      setError(null);
    } catch (loadError) {
      setError(toMessage(loadError));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // Downloads finishing elsewhere invalidate the dashboard (ui-redesign §4.1).
  useRefreshOn("dashboard", () => {
    void load();
  });

  const stopPolling = useCallback(() => {
    if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
  }, []);

  const scheduleIdle = useCallback((delay = RESET_DELAY_MS) => {
    if (resetRef.current) clearTimeout(resetRef.current);
    resetRef.current = setTimeout(() => {
      setAutoText(IDLE_AUTO_TEXT);
      setAutoDisabled(false);
    }, delay);
  }, []);

  useEffect(
    () => () => {
      stopPolling();
      if (resetRef.current) clearTimeout(resetRef.current);
    },
    [stopPolling],
  );

  const startQueuePolling = useCallback(() => {
    stopPolling();
    pollRef.current = setInterval(async () => {
      try {
        const queue = await api.getQueue();
        const items = queue.items ?? [];
        const active = items.filter(
          (item) => item.status === "pending" || item.status === "downloading",
        );
        const done = items.filter((item) => item.status === "done").length;
        const failed = items.filter((item) => item.status === "failed").length;

        if (active.length === 0) {
          stopPolling();
          setAutoDisabled(false);
          if (items.length === 0) {
            setAutoText(IDLE_AUTO_TEXT);
          } else {
            const total = done + failed;
            setAutoText(
              failed > 0 ? `⚠ ${done}/${total} 完成 (${failed} 失败)` : `✅ ${total} 个已完成`,
            );
          }
          scheduleIdle(5_000);
          void load();
        } else {
          setAutoText(`⏳ ${done}/${items.length} 完成...`);
        }
      } catch {
        /* transient network error — keep polling */
      }
    }, POLL_INTERVAL_MS);
  }, [load, scheduleIdle, stopPolling]);

  const runAutoUpdate = async () => {
    setAutoDisabled(true);
    setAutoText("⏳ 检查更新中...");
    try {
      const result = await api.runAutoUpdate();

      if ((result.outdated ?? 0) === 0) {
        setAutoText("✅ 全部最新");
        notify.success("所有 Mod 均为最新版本");
        scheduleIdle();
        return;
      }

      setAutoText(`✅ ${result.queued ?? 0} 个已入队 · 监控中...`);
      notify.success(`发现 ${result.outdated} 个待更新 Mod，已加入下载队列`);
      startQueuePolling();
    } catch (updateError) {
      if (isConflict(updateError)) {
        setAutoText(`⏳ ${AUTO_UPDATE_CONFLICT_MESSAGE}`);
        notify.info(AUTO_UPDATE_CONFLICT_MESSAGE);
        scheduleIdle();
        return;
      }
      setAutoText("❌ 失败，重试");
      setAutoDisabled(false);
      notify.error(`检查失败: ${toMessage(updateError)}`);
      scheduleIdle();
    }
  };

  const modsCount = data ? String(data.mods_count ?? 0) : "—";
  const updatesCount = data ? String(data.updates_pending ?? 0) : "—";
  const diskUsage = data ? formatSize(data.disk_usage_mb ?? 0) : "—";
  const activity = data?.recent_activity ?? [];

  return (
    <>
      <div className="card dashboard__hero">
        <div className="dashboard__logo" aria-hidden="true">
          🔧
        </div>
        <h2 className="dashboard__title">rwmod · RimWorld Mod 管理器</h2>
        <p className="dashboard__subtitle">全离线 SteamCMD 匿名下载</p>

        <div className="stat-grid">
          <div className="card stat">
            <div className="stat__value stat__value--blue" data-testid="status-dashboard-mods">
              {modsCount}
            </div>
            <div className="stat__label">已安装 Mod</div>
          </div>
          <div className="card stat">
            <div className="stat__value stat__value--yellow" data-testid="status-dashboard-updates">
              {updatesCount}
            </div>
            <div className="stat__label">待更新</div>
          </div>
          <div className="card stat">
            <div className="stat__value" data-testid="status-dashboard-disk">
              {diskUsage}
            </div>
            <div className="stat__label">磁盘占用</div>
          </div>
        </div>
      </div>

      <div className="card">
        <div className="card-header">最近活动</div>
        <div className="dashboard__activity" data-testid="list-activity">
          {loading && !data ? (
            <LoadingState message="加载中..." testid="status-activity-loading" />
          ) : error && !data ? (
            <ErrorState message={error} onRetry={() => void load()} testid="status-activity-error" />
          ) : activity.length === 0 ? (
            <span className="dashboard__muted">暂无活动记录</span>
          ) : (
            activity.slice(0, 6).map((entry, index) => (
              <div
                className="activity-row"
                key={`${entry.workshop_id}-${entry.created_at ?? index}`}
                data-testid={`item-activity-${entry.workshop_id || index}`}
              >
                <span aria-hidden="true">{entry.status === "success" ? "✅" : "❌"}</span>
                <span className="activity-row__name">
                  {entry.mod_name || entry.workshop_id}
                </span>
                <span className="activity-row__time">{entry.created_at ?? ""}</span>
              </div>
            ))
          )}
        </div>
      </div>

      <div className="card">
        <div className="card-header">⚡ 快速操作</div>
        <div className="dashboard__actions">
          <button
            type="button"
            className="btn btn-primary"
            data-testid="action-quick-search"
            onClick={() => navigate("search")}
          >
            🔍 搜索 Workshop
          </button>
          <button
            type="button"
            className="btn btn-primary"
            data-testid="action-quick-download"
            onClick={() => navigate("download")}
          >
            📥 下载 Mod
          </button>
          <button
            type="button"
            className="btn btn-accent"
            data-testid="action-auto-update"
            disabled={autoDisabled}
            onClick={() => void runAutoUpdate()}
          >
            {autoText}
          </button>
          <button
            type="button"
            className="btn btn-ghost"
            data-testid="action-quick-queue"
            onClick={() => navigate("queue")}
          >
            📋 下载队列
          </button>
        </div>
      </div>
    </>
  );
}

export default DashboardPanel;
