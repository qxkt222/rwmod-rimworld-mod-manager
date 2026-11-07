/**
 * Download panel (FE-3) — mod id input, force toggle, dependency preview and
 * the SSE download log.
 *
 * The SSE stream itself lives in `store/transfer` (`startStream` / `closeStream`):
 * switching tabs keeps the connection and the log. Only one stream is ever in
 * flight — ids are processed sequentially, and `startStream` closes any stale
 * handle for the channel before opening a new one.
 *
 * Anchors are canonical testids: `field-mod-input`, `action-download`,
 * `field-force-download`, `status-dependency-preview`,
 * `status-download-progress`, `status-download-progress-bar`,
 * `status-download-current`, `status-download-error`, `list-log` and
 * `action-clear-log`. The legacy ids are gone.
 */
import { useCallback, useEffect, useRef, useState } from "react";

import { request, toMessage } from "../api/client";
import { bump } from "../store/bus";
import { useModsStore } from "../store/mods";
import { useQueueStore } from "../store/queue";
import { useTransferStore } from "../store/transfer";
import { notify } from "../store/ui";

interface ModDependency {
  id?: string;
  name?: string;
  installed?: boolean;
}

type DependencyMap = Record<string, ModDependency[]>;

const DEP_DEBOUNCE_MS = 500;
const STREAM_TIMEOUT_MS = 5 * 60 * 1000;

function lineColor(line: string): string | undefined {
  if (line.includes("✗") || line.includes("❌") || line.includes("失败")) return "#f7768e";
  if (line.includes("⚠")) return "#e0af68";
  if (line.includes("✓") || line.includes("✅")) return "#9ece6a";
  if (line.startsWith("▶") || line.startsWith("──") || line.includes("正在")) return "#7aa2f7";
  return undefined;
}

export function DownloadPanel() {
  const lines = useTransferStore((state) => state.channels.download.lines);
  const streaming = useTransferStore((state) => state.channels.download.streaming);
  const progress = useTransferStore((state) => state.channels.download.progress);
  const channelError = useTransferStore((state) => state.channels.download.error);

  const [input, setInput] = useState("");
  const [force, setForce] = useState(false);
  const [deps, setDeps] = useState<DependencyMap | null>(null);
  const [depsLoading, setDepsLoading] = useState(false);
  const [logLines, setLogLines] = useState<string[]>([]);
  const [currentId, setCurrentId] = useState<string | null>(null);
  const [running, setRunning] = useState(false);

  const pendingRef = useRef<string[]>([]);
  const forceRef = useRef(false);
  const wasStreamingRef = useRef(false);
  const prevLinesRef = useRef<string[]>([]);
  const currentIdRef = useRef<string | null>(null);
  // Sticky for the whole batch: a later success must not hide an earlier fail.
  const batchFailedRef = useRef(false);

  const finishBatch = useCallback((failed: boolean) => {
    setRunning(false);
    setCurrentId(null);
    void useQueueStore
      .getState()
      .refresh()
      .catch(() => {
        /* queue error is surfaced inside the queue panel */
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
    if (failed) notify.error("下载失败");
    else notify.success("下载完成");
  }, []);

  const startNext = useCallback(() => {
    const next = pendingRef.current.shift();
    if (!next) {
      finishBatch(batchFailedRef.current);
      return;
    }
    // A fresh stream clears the channel; reset the diff baseline so the first
    // frame of this segment is appended even when it repeats the previous one.
    prevLinesRef.current = [];
    // The transfer slice keeps only frame text, so the panel synthesises the
    // legacy start/done lines (the frames themselves carry no message).
    setLogLines((current) => [...current, `── Mod ${next} ──`, `▶ 开始下载 ${next}`]);
    currentIdRef.current = next;
    setCurrentId(next);
    useTransferStore
      .getState()
      .startStream(
        "download",
        `/api/download/stream?id=${encodeURIComponent(next)}&force=${forceRef.current}`,
      );
  }, [finishBatch]);

  // Mirror the store channel into the accumulated console. `startStream`
  // clears the channel between ids, so detect a segment reset by comparing
  // the new array with the previous one (see startNext's baseline reset).
  useEffect(() => {
    const previous = prevLinesRef.current;
    const reset = lines.length === 0 || lines.length < previous.length || lines[0] !== previous[0];
    const start = reset ? 0 : previous.length;
    if (lines.length > start) {
      const fresh = lines.slice(start);
      setLogLines((current) => [...current, ...fresh]);
    }
    prevLinesRef.current = lines;
  }, [lines]);

  // A stream ends when the transfer slice flips `streaming` off (done/fail/
  // close). Queue the next id, or close the batch.
  useEffect(() => {
    if (streaming) {
      wasStreamingRef.current = true;
      return;
    }
    if (!wasStreamingRef.current) return;
    wasStreamingRef.current = false;
    if (channelError) batchFailedRef.current = true;
    else if (currentIdRef.current) {
      setLogLines((current) => [...current, `✅ 完成 ${currentIdRef.current}`]);
    }
    if (pendingRef.current.length > 0) startNext();
    else finishBatch(batchFailedRef.current);
  }, [streaming, channelError, startNext, finishBatch]);

  // Legacy timeout guard: 5 minutes without a frame aborts the stream.
  useEffect(() => {
    if (!streaming) return;
    const timer = setTimeout(() => {
      useTransferStore.getState().closeStream("download");
      setLogLines((current) => [...current, "  ✗ 超时：5 分钟无响应，已中止"]);
    }, STREAM_TIMEOUT_MS);
    return () => clearTimeout(timer);
  }, [streaming, lines.length]);

  // Dependency preview — 500ms debounce, same as the legacy panel.
  useEffect(() => {
    const raw = input.trim();
    if (!raw) {
      setDeps(null);
      setDepsLoading(false);
      return;
    }
    let cancelled = false;
    const timer = setTimeout(() => {
      const ids = raw.split(/[,;\s]+/).filter(Boolean);
      if (ids.length === 0) return;
      setDepsLoading(true);
      void request<{ deps?: DependencyMap }>("post", "/api/mods/dependencies", {
        body: { ids },
      })
        .then((data) => {
          if (!cancelled) setDeps(data.deps ?? {});
        })
        .catch(() => {
          if (!cancelled) setDeps(null);
        })
        .finally(() => {
          if (!cancelled) setDepsLoading(false);
        });
    }, DEP_DEBOUNCE_MS);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [input]);

  const handleDownload = () => {
    const raw = input.trim();
    if (!raw) return;
    const ids = raw.split(/[,;\s]+/).filter(Boolean);
    if (ids.length === 0) return;

    forceRef.current = force;
    pendingRef.current = [...ids];
    wasStreamingRef.current = false;
    batchFailedRef.current = false;
    // One in-flight SSE stream globally: a download must not run beside a
    // collection stream (the legacy "double stream" bug).
    useTransferStore.getState().closeStream("collection");
    useTransferStore.getState().closeStream("import");
    useTransferStore.getState().clear("download");
    prevLinesRef.current = [];
    setLogLines([]);
    setRunning(true);
    startNext();
  };

  const clearLog = () => {
    useTransferStore.getState().clear("download");
    prevLinesRef.current = [];
    setLogLines([]);
  };

  const depEntries = Object.entries(deps ?? {}).filter(([, list]) => (list ?? []).length > 0);
  let installedCount = 0;
  let missingCount = 0;
  for (const [, list] of depEntries) {
    for (const dep of list) {
      if (dep.installed) installedCount += 1;
      else missingCount += 1;
    }
  }

  return (
    <>
      <div className="card">
        <div className="card-header">📥 下载 Mod</div>
        <div className="form-row">
          <input
            type="text"
            data-testid="field-mod-input"
            placeholder="Mod ID 或 Workshop 链接，多个用逗号分隔"
            aria-label="Mod ID 或 Workshop 链接"
            value={input}
            onChange={(event) => setInput(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") handleDownload();
            }}
          />
          <button
            type="button"
            className="btn btn-primary"
            data-testid="action-download"
            disabled={running}
            onClick={handleDownload}
          >
            {running ? "下载中..." : "下载"}
          </button>
        </div>
        <label className="check-label" style={{ marginTop: 8 }}>
          <input
            type="checkbox"
            data-testid="field-force-download"
            checked={force}
            onChange={(event) => setForce(event.target.checked)}
          />{" "}
          覆盖已存在的 mod
        </label>

        <div
          data-testid="status-dependency-preview"
          style={{ marginTop: 8, fontSize: 12 }}
        >
          {depsLoading ? (
            <span style={{ fontSize: 11, color: "var(--gray-text)" }}>查询依赖中...</span>
          ) : depEntries.length === 0 ? (
            deps ? (
              <span style={{ fontSize: 11, color: "var(--gray-text)" }}>未检测到依赖关系</span>
            ) : null
          ) : (
            <>
              <div style={{ marginBottom: 4 }}>
                {missingCount > 0 ? (
                  <span style={{ color: "var(--yellow)", fontSize: 11 }}>
                    ⚠ {missingCount} 个依赖缺失，下载时将自动安装
                  </span>
                ) : installedCount > 0 ? (
                  <span style={{ color: "var(--green)", fontSize: 11 }}>
                    ✓ {installedCount} 个依赖已安装
                  </span>
                ) : null}
              </div>
              {depEntries.map(([modId, list]) => (
                <div key={modId}>
                  <div style={{ marginBottom: 6 }}>
                    <strong style={{ fontSize: 11 }}>{modId}</strong>
                    <span
                      style={{ fontSize: 10, color: "var(--gray-text)", marginLeft: 4 }}
                    >
                      {list.length} 个依赖
                    </span>
                  </div>
                  {list.map((dep, index) => (
                    <div
                      className="dep-item"
                      key={`${dep.id ?? index}-${index}`}
                      style={{ fontSize: 11, padding: "2px 0 2px 12px" }}
                    >
                      <span style={{ color: dep.installed ? "var(--green)" : "var(--yellow)" }}>
                        {dep.installed ? "✓" : "⚠"}
                      </span>{" "}
                      <span>{dep.name}</span>
                      <span
                        style={{ color: "var(--gray-text)", fontSize: 10, marginLeft: 4 }}
                      >
                        {dep.id}
                      </span>
                      {dep.installed ? null : (
                        <span style={{ color: "var(--red)", fontSize: 10 }}>未安装</span>
                      )}
                    </div>
                  ))}
                </div>
              ))}
            </>
          )}
        </div>
      </div>

      <div
        className="card"
        style={{ flex: 1, display: "flex", flexDirection: "column", overflowY: "auto", minHeight: 0 }}
      >
        <div className="card-header" style={{ display: "flex", justifyContent: "space-between" }}>
          <span>📋 下载日志</span>
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            data-testid="action-clear-log"
            onClick={clearLog}
          >
            清空
          </button>
        </div>

        {currentId ? (
          <div
            data-testid="status-download-progress"
            style={{ padding: "0 16px 8px" }}
          >
            <div
              style={{
                display: "flex",
                justifyContent: "space-between",
                fontSize: 12,
                marginBottom: 4,
              }}
            >
              <span>下载进度</span>
              <span data-testid="status-download-current" style={{ color: "var(--gray-text)" }}>
                {currentId} {progress.toFixed(1)}%
              </span>
            </div>
            <div className="queue-bar">
              <div
                className="queue-bar-inner"
                data-testid="status-download-progress-bar"
                style={{ width: `${Math.min(100, progress)}%` }}
              />
            </div>
          </div>
        ) : null}

        {channelError && !streaming ? (
          <div className="status-banner" data-testid="status-download-error" role="alert">
            {channelError}
          </div>
        ) : null}

        <div className="log-console" data-testid="list-log">
          {logLines.map((line, index) => (
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

export default DownloadPanel;
