/**
 * Import panel (FE-3) — drag & drop mod-list files and ModsConfig.xml.
 *
 * Uploads are one-shot mutations with no cross-panel state, so the request
 * lives in this module (`request` is the shared typed HTTP client — panels
 * never call `fetch`). Results land as toasts, and the bus invalidates the
 * panels whose data changed (mods/queue/history/dashboard).
 *
 * Legacy anchors kept for `import.spec.ts`: `#import-file` (inside
 * `#drop-zone`), `#import-sort` (inside `#drop-zone-xml`).
 */
import { useRef, useState } from "react";

import { request, toMessage } from "../api/client";
import { bump } from "../store/bus";
import { useQueueStore } from "../store/queue";
import { notify } from "../store/ui";

interface ImportFileResult {
  total: number;
  results?: { id?: string; ok?: boolean }[];
}

interface ImportSortResult {
  total_packages: number;
  missing: number;
  unknown?: string[];
  downloaded: number;
}

interface DropZoneProps {
  zoneId: string;
  inputId: string;
  testid: string;
  inputTestid: string;
  accept: string;
  busy: boolean;
  title: string;
  hint: string;
  onFile: (file: File) => void;
}

function DropZone({
  zoneId,
  inputId,
  testid,
  inputTestid,
  accept,
  busy,
  title,
  hint,
  onFile,
}: DropZoneProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [dragOver, setDragOver] = useState(false);

  return (
    <label
      className={`file-drop${dragOver ? " drag-over" : ""}`}
      id={zoneId}
      data-testid={testid}
      aria-busy={busy}
      onDragOver={(event) => {
        event.preventDefault();
        setDragOver(true);
      }}
      onDragLeave={() => setDragOver(false)}
      onDrop={(event) => {
        event.preventDefault();
        setDragOver(false);
        const files = event.dataTransfer?.files;
        if (files?.length && inputRef.current) {
          inputRef.current.files = files;
          onFile(files[0]);
        }
      }}
    >
      <div>{busy ? "处理中..." : title}</div>
      <div style={{ fontSize: 11, marginTop: 4 }}>{hint}</div>
      <input
        ref={inputRef}
        type="file"
        id={inputId}
        data-testid={inputTestid}
        accept={accept}
        onChange={(event) => {
          const file = event.target.files?.[0];
          if (file) onFile(file);
          // Allow re-selecting the same file in a later round.
          event.target.value = "";
        }}
      />
    </label>
  );
}

export function ImportPanel() {
  const [busy, setBusy] = useState<"file" | "sort" | null>(null);

  const afterImport = () => {
    void useQueueStore
      .getState()
      .refresh()
      .catch(() => {
        /* queue errors surface in the queue panel */
      });
    bump("mods");
    bump("queue");
    bump("history");
    bump("dashboard");
  };

  const importFile = async (file: File) => {
    setBusy("file");
    try {
      const form = new FormData();
      form.append("file", file);
      const data = await request<ImportFileResult>("post", "/api/import/file", { form });
      const ok = (data.results ?? []).filter((row) => row.ok).length;
      notify.success(`导入完成: ${ok}/${data.total} 成功`);
      afterImport();
    } catch (error) {
      notify.error(`导入失败: ${toMessage(error)}`);
    } finally {
      setBusy(null);
    }
  };

  const importSort = async (file: File) => {
    setBusy("sort");
    try {
      const form = new FormData();
      form.append("file", file);
      const data = await request<ImportSortResult>("post", "/api/import/sort", { form });
      let message = `ModsConfig: ${data.total_packages} 个包, ${data.missing} 缺失, ${data.downloaded} 已下载`;
      if (data.unknown?.length) message += `, ${data.unknown.length} 无法解析`;
      notify.success(message);
      afterImport();
    } catch (error) {
      notify.error(`导入失败: ${toMessage(error)}`);
    } finally {
      setBusy(null);
    }
  };

  return (
    <>
      <div className="card">
        <div className="card-header">📋 导入 Mod 列表</div>
        <DropZone
          zoneId="drop-zone"
          inputId="import-file"
          testid="dropzone-import-file"
          inputTestid="field-import-file"
          accept=".txt"
          busy={busy === "file"}
          title="拖拽 .txt 文件到此处，或点击选择"
          hint="每行一个 Mod ID 或 Workshop 链接"
          onFile={(file) => void importFile(file)}
        />
      </div>

      <div className="card">
        <div className="card-header">📄 导入 ModsConfig.xml</div>
        <DropZone
          zoneId="drop-zone-xml"
          inputId="import-sort"
          testid="dropzone-import-sort"
          inputTestid="field-import-sort"
          accept=".xml"
          busy={busy === "sort"}
          title="拖拽 ModsConfig.xml 到此处，下载缺失的 mod"
          hint="解析 activeMods 并给出缺失 / 已下载统计"
          onFile={(file) => void importSort(file)}
        />
      </div>
    </>
  );
}

export default ImportPanel;
