/**
 * Queue panel (FE-3) — download queue plus the update-check card that the
 * legacy panel mounted in the same page (docs/ui-redesign.md §6.1).
 *
 * State: `store/queue` (items + WS state + pause), `store/updates` (outdated list).
 * The queue itself is fed by the singleton WS bridge in `api/ws.ts`; REST
 * polling is the fallback there. This panel only renders the store and calls
 * its actions, so the legacy "double stream / stale badge" class of bugs
 * cannot come back.
 *
 * Legacy anchors kept for `queue.spec.ts`: `#queue-list`, `.queue-item`,
 * `[data-remove]`, `[data-retry]`, `.queue-bar`, `#btn-queue-start`,
 * `#btn-queue-pause`, `#btn-queue-clear`, `#btn-check-updates`,
 * `#btn-update-all`, `#update-results`, `.changelog-toggle`, `.changelog-full`.
 */
import { useCallback, useEffect, useState } from "react";

import { isConflict, toMessage } from "../api/client";
import type { QueueItem } from "../api/types";
import { EmptyState } from "../components/EmptyState";
import { ErrorState } from "../components/ErrorState";
import { LoadingState } from "../components/LoadingState";
import { fmtBytes } from "../dom";
import { bump, useRefreshOn } from "../store/bus";
import { useModsStore } from "../store/mods";
import { useQueueStore } from "../store/queue";
import { AUTO_UPDATE_CONFLICT_MESSAGE, useUpdatesStore } from "../store/updates";
import { notify } from "../store/ui";

const EMPTY_TEXT = "队列为空，去「搜索」或「下载」添加 Mod";
const MAX_ERROR_DETAIL = 200;
const CHANGELOG_PREVIEW = 200;

const STATUS_ICONS: Record<string, string> = {
  pending: "⏳",
  downloading: "⬇️",
  done: "✅",
  failed: "❌",
  cancelled: "🚫",
};

const REMOVABLE = new Set(["pending", "downloading", "failed"]);

function fmtSpeed(bps: number): string {
  if (!bps || bps <= 0 || !Number.isFinite(bps)) return "";
  return `${fmtBytes(bps)}/s`;
}

interface QueueRowProps {
  item: QueueItem;
  onRemove: (id: string) => void;
  onRetry: (id: string) => void;
}

function QueueRow({ item, onRemove, onRetry }: QueueRowProps) {
  const status = item.status ?? "";
  const downloading = status === "downloading";
  const pct = Math.round((item.progress ?? 0) * 100);
  const speed = downloading ? fmtSpeed(item.speed_bps ?? 0) : "";
  const bytes =
    downloading && (item.downloaded ?? 0) > 0
      ? `${fmtBytes(item.downloaded ?? 0)} / ${fmtBytes(item.total ?? 0)}`
      : "";
  const detailBits = [speed, bytes].filter(Boolean).join(" · ");
  const errorDetail = (item.error_detail ?? "").trim().slice(0, MAX_ERROR_DETAIL);

  return (
    <>
      <div className="queue-item" data-testid={`item-queue-${item.id}`}>
        <span className="q-status">{STATUS_ICONS[status] ?? "❓"}</span>
        <span className="q-id">{item.id}</span>
        <span className="q-name">{item.name ?? ""}</span>
        <span className="q-progress" title={errorDetail || undefined}>
          {item.msg || status}
          {detailBits ? <span className="q-speed"> {detailBits}</span> : null}
        </span>
        {status === "failed" ? (
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            data-retry={item.id}
            data-testid={`action-queue-retry-${item.id}`}
            title="重新排队"
            onClick={() => onRetry(item.id)}
          >
            ↻
          </button>
        ) : null}
        {REMOVABLE.has(status) ? (
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            data-remove={item.id}
            data-testid={`action-queue-remove-${item.id}`}
            title="从队列移除"
            onClick={() => onRemove(item.id)}
          >
            ✕
          </button>
        ) : null}
      </div>
      {downloading ? (
        <>
          <div className="queue-bar">
            <div className="queue-bar-inner" style={{ width: `${pct}%` }} />
          </div>
          <div className="queue-meta">
            {pct}%{detailBits ? ` — ${detailBits}` : ""}
          </div>
        </>
      ) : null}
    </>
  );
}

function UpdateSection() {
  const outdated = useUpdatesStore((state) => state.outdated);
  const checkStatus = useUpdatesStore((state) => state.checkStatus);
  const checkError = useUpdatesStore((state) => state.error);
  const partial = useUpdatesStore((state) => state.partial);
  const failedBatches = useUpdatesStore((state) => state.failedBatches);
  const totalBatches = useUpdatesStore((state) => state.totalBatches);
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [allBusy, setAllBusy] = useState(false);
  const [allText, setAllText] = useState("⬇ 全部更新");
  const [allMessage, setAllMessage] = useState<string | null>(null);

  const check = async () => {
    try {
      await useUpdatesStore.getState().check();
    } catch (error) {
      notify.error(`检查失败: ${toMessage(error)}`);
    }
  };

  const updateOne = async (id: string) => {
    try {
      await useQueueStore.getState().add([id], true);
      notify.success(`已加入队列并标记为更新: ${id}`);
    } catch (error) {
      notify.error(`失败: ${toMessage(error)}`);
    }
  };

  const updateAll = async () => {
    setAllBusy(true);
    setAllText("⏳ 更新中...");
    setAllMessage("正在全部加入队列并开始下载...");
    try {
      const result = await useUpdatesStore.getState().runAutoUpdate();
      const queued = result.queued ?? 0;
      const outdatedCount = result.outdated ?? 0;
      const requeued = Math.max(0, outdatedCount - queued);
      notify.success(
        `已检查 ${result.checked ?? 0} 个 Mod：${queued} 个新加入队列，${requeued} 个已在队列中，开始下载`,
      );
      setAllMessage(
        `✅ 已开始处理 ${queued + requeued} 个待更新 Mod（新加入 ${queued} 个，队列中已有 ${requeued} 个）`,
      );
      bump("queue");
      bump("dashboard");
    } catch (error) {
      if (isConflict(error)) {
        notify.warn(AUTO_UPDATE_CONFLICT_MESSAGE);
        setAllMessage(AUTO_UPDATE_CONFLICT_MESSAGE);
        return;
      }
      setAllMessage(`全部更新失败: ${toMessage(error)}`);
      notify.error(`更新失败: ${toMessage(error)}`);
    } finally {
      setAllBusy(false);
      setAllText("⬇ 全部更新");
    }
  };

  return (
    <div className="card">
      <div className="card-header" style={{ display: "flex", justifyContent: "space-between" }}>
        <span>🔄 更新检查</span>
        <div style={{ display: "flex", gap: 6 }}>
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            id="btn-check-updates"
            data-testid="action-check-updates"
            disabled={checkStatus === "checking"}
            onClick={() => void check()}
          >
            {checkStatus === "checking" ? "⏳ 检查中..." : "检查更新"}
          </button>
          {outdated.length > 0 ? (
            <button
              type="button"
              className="btn btn-primary btn-sm"
              id="btn-update-all"
              data-testid="action-update-all"
              disabled={allBusy}
              onClick={() => void updateAll()}
            >
              {allText}
            </button>
          ) : null}
        </div>
      </div>
      <div id="update-results" data-testid="list-update-results">
        {allMessage ? <div data-testid="status-update-all">{allMessage}</div> : null}
        {partial ? (
          // Incomplete, not wrong: warn-coloured line, never the error state.
          <div
            className="status-banner"
            data-testid="status-updates-partial"
            role="status"
            style={{ color: "var(--yellow)" }}
          >
            {failedBatches}/{totalBatches} 批未取到，结果可能不完整
          </div>
        ) : null}
        {checkStatus === "checking" ? (
          <LoadingState message="正在检查更新..." testid="status-updates-loading" />
        ) : checkStatus === "error" ? (
          <ErrorState
            message={`检查失败: ${checkError ?? ""}`}
            onRetry={() => void check()}
            testid="status-updates-error"
          />
        ) : checkStatus === "idle" ? (
          <span className="muted">点击「检查更新」</span>
        ) : outdated.length === 0 ? (
          // A partial check cannot claim "everything is current" — the
          // missing batches may contain updates, so only the banner shows.
          partial ? null : (
            <span className="update-ok" data-testid="status-updates-current">
              ✅ 所有 Mod 均为最新版本
            </span>
          )
        ) : (
          outdated.map((item, index) => {
            const desc = (item.file_description ?? "").trim();
            const preview = desc.slice(0, CHANGELOG_PREVIEW);
            const hasMore = desc.length > CHANGELOG_PREVIEW;
            const uid = `changelog-${index}`;
            const isExpanded = Boolean(expanded[uid]);
            return (
              <div
                className="mod-row"
                style={{ alignItems: "flex-start" }}
                key={`${item.workshop_id}-${index}`}
                data-testid={`item-update-${item.workshop_id}`}
              >
                <div className="mod-icon" style={{ marginTop: 4 }}>
                  🔄
                </div>
                <div className="mod-info" style={{ flex: 1, minWidth: 0 }}>
                  <div className="mod-name">{item.name}</div>
                  <div className="mod-meta">
                    <span>{item.folder}</span>
                    <span>Workshop {item.workshop_id}</span>
                    {item.time_updated ? (
                      <span>
                        更新于 {new Date(item.time_updated * 1000).toLocaleDateString("zh-CN")}
                      </span>
                    ) : null}
                  </div>
                  {desc ? (
                    <div className="changelog-block">
                      <div className="changelog-preview" hidden={isExpanded}>
                        {preview}
                        {hasMore ? "..." : ""}
                      </div>
                      {hasMore ? (
                        <>
                          <div className="changelog-full" hidden={!isExpanded}>
                            {desc}
                          </div>
                          <button
                            type="button"
                            className="btn btn-ghost btn-sm changelog-toggle"
                            onClick={() =>
                              setExpanded((state) => ({ ...state, [uid]: !state[uid] }))
                            }
                          >
                            {isExpanded ? "📖 收起" : "📖 展开全文"}
                          </button>
                        </>
                      ) : null}
                    </div>
                  ) : null}
                </div>
                <button
                  type="button"
                  className="btn btn-primary btn-sm"
                  data-id={item.workshop_id}
                  data-testid={`action-update-one-${item.workshop_id}`}
                  style={{ flexShrink: 0 }}
                  onClick={() => void updateOne(item.workshop_id)}
                >
                  ⬇ 更新
                </button>
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}

export function QueuePanel() {
  const items = useQueueStore((state) => state.items);
  const paused = useQueueStore((state) => state.paused);
  const status = useQueueStore((state) => state.status);
  const error = useQueueStore((state) => state.error);
  const wsState = useQueueStore((state) => state.wsState);
  const [starting, setStarting] = useState(false);

  const refreshQueue = useCallback(() => {
    void useQueueStore
      .getState()
      .refresh()
      .catch(() => {
        /* the store already holds the error — the panel renders it */
      });
  }, []);

  useEffect(() => {
    refreshQueue();
  }, [refreshQueue]);

  // Other panels (search/download/import) and the WS bridge invalidate the queue.
  useRefreshOn("queue", refreshQueue);
  useRefreshOn("updates", () => {
    void useUpdatesStore.getState().refreshStatus();
  });

  const handleStart = async () => {
    if (starting) return;
    setStarting(true);
    try {
      await useQueueStore.getState().start();
      // The queue runner changed the installed mod set.
      void useModsStore
        .getState()
        .load()
        .catch(() => {
          /* badges refresh on the next visit */
        });
      bump("mods");
      notify.success("队列完成");
    } catch (startError) {
      notify.error(`队列执行失败: ${toMessage(startError)}`);
    } finally {
      setStarting(false);
    }
  };

  const handlePause = async () => {
    try {
      if (paused) {
        await useQueueStore.getState().resume();
        notify.info("队列已恢复");
      } else {
        await useQueueStore.getState().pause();
        notify.warn("队列已暂停（当前批次完成后停止）");
      }
    } catch {
      notify.error("队列状态切换失败");
    }
  };

  const handleClear = async () => {
    try {
      await useQueueStore.getState().clear();
    } catch (clearError) {
      notify.error(`清空失败: ${toMessage(clearError)}`);
    }
  };

  const handleRemove = async (id: string) => {
    try {
      await useQueueStore.getState().remove(id);
    } catch (removeError) {
      notify.error(`移除失败: ${toMessage(removeError)}`);
    }
  };

  const handleRetry = async (id: string) => {
    try {
      await useQueueStore.getState().retry([id]);
      notify.info(`已重新排队: ${id}`);
    } catch (retryError) {
      notify.error(`重试失败: ${toMessage(retryError)}`);
    }
  };

  const wsHint =
    wsState === "connected"
      ? null
      : wsState === "connecting"
        ? "正在连接实时队列…"
        : "实时连接已断开，队列状态可能不是最新（每 5 秒自动重试）";

  return (
    <>
      <div className="card" style={{ flex: 1, overflowY: "auto" }}>
        <div className="card-header" style={{ display: "flex", justifyContent: "space-between" }}>
          <span>📋 下载队列</span>
          <div style={{ display: "flex", gap: 6 }}>
            <button
              type="button"
              className="btn btn-primary btn-sm"
              id="btn-queue-start"
              data-testid="action-queue-start"
              disabled={starting}
              onClick={() => void handleStart()}
            >
              {starting ? "⏳ 处理中..." : "▶ 开始"}
            </button>
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              id="btn-queue-pause"
              data-testid="action-queue-pause"
              title={paused ? "继续处理队列" : "当前批次完成后暂停"}
              onClick={() => void handlePause()}
            >
              {paused ? "▶ 恢复" : "⏸ 暂停"}
            </button>
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              id="btn-queue-clear"
              data-testid="action-queue-clear"
              onClick={() => void handleClear()}
            >
              清空已完成
            </button>
          </div>
        </div>

        {wsHint ? (
          <div className="status-banner" data-testid="status-queue-ws" role="status">
            {wsHint}
          </div>
        ) : null}

        <div id="queue-list" data-testid="list-queue">
          {status === "loading" && items.length === 0 ? (
            <LoadingState message="加载队列…" testid="status-queue-loading" />
          ) : status === "error" && items.length === 0 ? (
            <ErrorState
              message={error ?? "加载失败"}
              onRetry={() => void refreshQueue()}
              testid="status-queue-error"
            />
          ) : items.length === 0 ? (
            <EmptyState icon="📋" title={EMPTY_TEXT} testid="status-queue-empty" />
          ) : (
            items.map((item) => (
              <QueueRow
                key={item.id}
                item={item}
                onRemove={(id) => void handleRemove(id)}
                onRetry={(id) => void handleRetry(id)}
              />
            ))
          )}
        </div>
      </div>

      <UpdateSection />
    </>
  );
}

export default QueuePanel;
