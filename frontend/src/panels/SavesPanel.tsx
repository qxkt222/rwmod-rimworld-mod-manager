/**
 * Saves panel — scan the configured Saves directory and analyse an uploaded
 * .rws file.
 *
 * Parity contract with the legacy `src/panels/saves.ts` and `saves.spec.ts`:
 * `#saves-results`, `#btn-scan-saves`, `#saves-file-upload`, `#drop-zone-saves`.
 * Empty scans read exactly "No save files found"; the upload toast reads
 * "Found <n> mods in <file>" like the legacy panel.
 */
import { useCallback, useEffect, useRef, type ChangeEvent } from "react";

import { toMessage } from "../api/client";
import { EmptyState } from "../components/EmptyState";
import { ErrorState } from "../components/ErrorState";
import { LoadingState } from "../components/LoadingState";
import { useRefreshOn } from "../store/bus";
import { useSavesStore } from "../store/saves";
import { notify } from "../store/ui";

const rowStyle = {
  display: "flex",
  alignItems: "center",
  gap: 8,
  padding: "8px 4px",
  borderBottom: "1px solid var(--border)",
} as const;

export function SavesPanel() {
  const saves = useSavesStore((state) => state.saves);
  const status = useSavesStore((state) => state.status);
  const error = useSavesStore((state) => state.error);
  const analyzing = useSavesStore((state) => state.analyzing);
  const scan = useSavesStore((state) => state.scan);
  const analyze = useSavesStore((state) => state.analyze);

  const inputRef = useRef<HTMLInputElement>(null);

  const rescan = useCallback(() => {
    void scan().catch(() => {
      /* the store keeps the error for the ErrorState */
    });
  }, [scan]);

  useEffect(() => {
    rescan();
  }, [rescan]);

  useRefreshOn("saves", rescan);

  const onUpload = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    if (!file) return;
    try {
      const result = await analyze(file);
      notify.success(`Found ${result.mod_count ?? 0} mods in ${result.filename ?? file.name}`);
    } catch (uploadError) {
      notify.error(`Upload failed: ${toMessage(uploadError)}`);
    } finally {
      event.target.value = "";
    }
  };

  return (
    <div className="card">
      <div className="card-header" style={{ justifyContent: "space-between" }}>
        <span>Saves 存档分析</span>
        <button
          type="button"
          className="btn btn-primary btn-sm"
          id="btn-scan-saves"
          data-testid="action-scan-saves"
          disabled={status === "loading"}
          onClick={rescan}
        >
          🔄 扫描
        </button>
      </div>

      <div style={{ fontSize: 12, color: "var(--gray-text)", marginBottom: 8 }}>
        检测存档所需的 Mod 是否已安装；可上传 .rws 存档文件进行分析。
      </div>

      <label className="file-drop" id="drop-zone-saves" data-testid="drop-saves-file">
        <div>{analyzing ? "分析中..." : "上传 .rws 存档文件"}</div>
        <input
          ref={inputRef}
          type="file"
          id="saves-file-upload"
          data-testid="field-saves-upload"
          accept=".rws"
          onChange={(event) => void onUpload(event)}
        />
      </label>

      <div id="saves-results" data-testid="list-saves" style={{ fontSize: 12, marginTop: 8 }}>
        {status === "loading" && saves.length === 0 ? (
          <LoadingState message="Scanning..." testid="status-saves-loading" />
        ) : status === "error" && saves.length === 0 ? (
          <ErrorState
            message={`Error: ${error ?? "扫描失败"}`}
            onRetry={rescan}
            testid="status-saves-error"
          />
        ) : saves.length === 0 ? (
          <EmptyState title="No save files found" testid="status-saves-empty" />
        ) : (
          saves.map((save, index) => (
            <div
              key={`${save.name}-${index}`}
              data-testid={`item-save-${index}`}
              style={rowStyle}
            >
              <strong>{save.name}</strong>
              <span style={{ color: save.loadable ? "var(--green)" : "var(--orange)" }}>
                {save.loadable ? "Ready" : `Missing ${save.missing_count ?? 0} mods`}
              </span>
              <small style={{ color: "var(--gray-text)" }}>{save.total_mods ?? 0} mods</small>
            </div>
          ))
        )}
      </div>
    </div>
  );
}

export default SavesPanel;
