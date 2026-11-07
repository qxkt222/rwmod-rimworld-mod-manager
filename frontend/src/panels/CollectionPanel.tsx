/**
 * Collection panel (FE-3) — Steam Workshop collection preview + download.
 *
 * The download log is the `collection` channel of `store/transfer`, so it
 * survives tab switches and only one stream can be open at a time. The preview
 * is a one-shot request kept in local state (legacy behaviour: it replaced the
 * console contents).
 *
 * Legacy anchors kept for `collection.spec.ts`: `#collection-input`,
 * `#btn-collection-preview`, `#btn-collection`, `#force-collection`,
 * `#log-collection`, `#collection-progress`, `#cp-total-bar`, `#cp-count`,
 * `#cp-current`.
 */
import { useCallback, useEffect, useRef, useState } from "react";

import { request, toMessage } from "../api/client";
import { bump } from "../store/bus";
import { useModsStore } from "../store/mods";
import { useQueueStore } from "../store/queue";
import { useTransferStore } from "../store/transfer";
import { notify } from "../store/ui";

interface CollectionPreview {
  collection_id?: string;
  total?: number;
  installed_count?: number;
  new_count?: number;
  failed_count?: number;
}

function lineColor(line: string): string | undefined {
  if (line.includes("✗") || line.includes("失败")) return "#f7768e";
  if (line.includes("⚠") || line.includes("🟡")) return "#e0af68";
  if (line.includes("✓") || line.includes("✅") || line.includes("🟢")) return "#9ece6a";
  if (line.includes("🔵") || line.includes("正在")) return "#7aa2f7";
  return undefined;
}

export function CollectionPanel() {
  const lines = useTransferStore((state) => state.channels.collection.lines);
  const streaming = useTransferStore((state) => state.channels.collection.streaming);
  const progress = useTransferStore((state) => state.channels.collection.progress);
  const channelError = useTransferStore((state) => state.channels.collection.error);

  const [input, setInput] = useState("");
  const [force, setForce] = useState(false);
  const [previewLines, setPreviewLines] = useState<string[] | null>(null);
  const [previewing, setPreviewing] = useState(false);
  const wasStreamingRef = useRef(false);

  const finishDownload = useCallback((failed: boolean) => {
    void useQueueStore
      .getState()
      .refresh()
      .catch(() => {
        /* queue errors surface in the queue panel */
      });
    void useModsStore
      .getState()
      .load()
      .catch(() => {
        /* sidebar badge refreshes on the next visit */
      });
    bump("mods");
    bump("queue");
    bump("history");
    bump("dashboard");
    if (failed) notify.error("合集下载失败");
    else notify.success("合集下载完成");
  }, []);

  useEffect(() => {
    if (streaming) {
      wasStreamingRef.current = true;
      return;
    }
    if (!wasStreamingRef.current) return;
    wasStreamingRef.current = false;
    finishDownload(Boolean(channelError));
  }, [streaming, channelError, finishDownload]);

  const preview = async () => {
    const collectionId = input.trim();
    if (!collectionId) return;
    setPreviewing(true);
    try {
      const data = await request<CollectionPreview>(
        "get",
        `/api/collection/preview/${encodeURIComponent(collectionId)}`,
      );
      const id = String(data.collection_id ?? collectionId);
      const rows = [
        `合集 ${id}: ${data.total ?? 0} 个 Mod`,
        `  🟢 已安装 ${data.installed_count ?? 0}`,
        `  🔵 新发现 ${data.new_count ?? 0}`,
      ];
      if (data.failed_count) rows.push(`  🟡 之前失败 ${data.failed_count}`);
      rows.push('点击"下载合集"开始下载');
      setPreviewLines(rows);
    } catch (error) {
      setPreviewLines([`预览失败: ${toMessage(error)}`]);
    } finally {
      setPreviewing(false);
    }
  };

  const download = () => {
    const collectionId = input.trim();
    if (!collectionId) return;
    setPreviewLines(null);
    // One in-flight SSE stream globally: a collection must not run beside a
    // download stream (the legacy "double stream" bug).
    useTransferStore.getState().closeStream("download");
    useTransferStore.getState().closeStream("import");
    useTransferStore
      .getState()
      .startStream(
        "collection",
        `/api/download/stream?id=${encodeURIComponent(collectionId)}&force=${force}`,
      );
  };

  const visibleLines = previewLines ?? lines;
  const showProgress = streaming || progress > 0;

  return (
    <>
      <div className="card">
        <div className="card-header">📦 下载 Steam 合集</div>
        <div className="form-row">
          <input
            type="text"
            id="collection-input"
            data-testid="field-collection-input"
            placeholder="Steam 合集 ID（如 3721899704）"
            aria-label="Steam 合集 ID"
            value={input}
            onChange={(event) => setInput(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") download();
            }}
          />
          <button
            type="button"
            className="btn btn-primary"
            id="btn-collection-preview"
            data-testid="action-collection-preview"
            style={{ marginRight: 4 }}
            disabled={previewing}
            onClick={() => void preview()}
          >
            {previewing ? "预览中..." : "预览"}
          </button>
          <button
            type="button"
            className="btn btn-primary"
            id="btn-collection"
            data-testid="action-collection-download"
            disabled={streaming}
            onClick={download}
          >
            下载合集
          </button>
        </div>
        <label className="check-label" style={{ marginTop: 8 }}>
          <input
            type="checkbox"
            id="force-collection"
            data-testid="field-force-collection"
            checked={force}
            onChange={(event) => setForce(event.target.checked)}
          />{" "}
          覆盖已存在的 mod
        </label>
      </div>

      <div
        className="card"
        style={{ flex: 1, display: "flex", flexDirection: "column", overflowY: "auto", minHeight: 0 }}
      >
        <div className="card-header">📋 日志</div>
        {showProgress ? (
          <div id="collection-progress" data-testid="status-collection-progress" style={{ padding: "8px 16px 0" }}>
            <div
              style={{
                display: "flex",
                justifyContent: "space-between",
                fontSize: 12,
                marginBottom: 4,
              }}
            >
              <span id="cp-total-label">合集下载进度</span>
              <span id="cp-count" style={{ color: "var(--gray-text)" }}>
                {streaming ? `${Math.round(progress)}%` : "完成"}
              </span>
            </div>
            <div className="queue-bar">
              <div
                className="queue-bar-inner"
                id="cp-total-bar"
                style={{ width: `${Math.min(100, progress)}%` }}
              />
            </div>
            <div id="cp-current" style={{ fontSize: 11, color: "var(--gray-text)", marginTop: 4 }}>
              {streaming ? "下载中…" : ""}
            </div>
          </div>
        ) : null}

        <div className="log-console" id="log-collection" data-testid="list-collection-log">
          {visibleLines.map((line, index) => (
            <span key={index} style={{ color: lineColor(line) }}>
              {line}
              {"\n"}
            </span>
          ))}
        </div>
      </div>
    </>
  );
}

export default CollectionPanel;
