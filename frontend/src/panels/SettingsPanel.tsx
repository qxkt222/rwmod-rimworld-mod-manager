/**
 * Mod settings panel — browse and edit RimWorld's `Config/Mod_*.xml`.
 *
 * Parity contract with the legacy `src/panels/settings.ts` and
 * `settings.spec.ts`: `#mod-settings-list`, `#mod-settings-status`,
 * `#mod-settings-backups`, `#mod-settings-editor`,
 * `#mod-settings-editor-title`, `#mod-settings-xml`, `#mod-settings-ack`,
 * `#btn-mod-settings-refresh`, `#btn-mod-settings-backup-all`,
 * `#btn-mod-settings-save`, `#btn-mod-settings-restore-latest`,
 * `#btn-mod-settings-close`, `.mod-setting-edit[data-file]`,
 * `.mod-setting-restore[data-file]`, `.mod-backup-restore[data-file]`.
 *
 * Saving requires the explicit "game is closed" acknowledgement, mirroring the
 * backend's `ack_game_closed` contract. Restore confirmation uses the shared
 * `ConfirmDialog` instead of native `confirm()`.
 */
import { useCallback, useEffect, useState } from "react";

import { toMessage } from "../api/client";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { ErrorState } from "../components/ErrorState";
import { LoadingState } from "../components/LoadingState";
import { useRefreshOn } from "../store/bus";
import { useSettingsStore } from "../store/settings";
import { notify } from "../store/ui";

/** Human-readable byte size (B/KB/MB/GB), e.g. 1536 → "1.5 KB". */
function formatBytes(value: number): string {
  if (!value || value <= 0 || !Number.isFinite(value)) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  let index = 0;
  let size = value;
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024;
    index += 1;
  }
  return `${size >= 100 ? size.toFixed(0) : size.toFixed(1)} ${units[index]}`;
}

/** ISO timestamp → zh-CN local string; returns the input unchanged if unparsable. */
function formatTimestamp(iso: string): string {
  try {
    return new Date(iso).toLocaleString("zh-CN");
  } catch {
    return iso;
  }
}

interface RestoreTarget {
  file: string;
  backup: string;
}

const rowStyle = { display: "flex", gap: 10, alignItems: "center", padding: "6px 4px" } as const;
const metaStyle = {
  display: "flex",
  gap: 10,
  flexWrap: "wrap",
  color: "var(--gray-text)",
  fontSize: 11,
} as const;

export function SettingsPanel() {
  const files = useSettingsStore((state) => state.files);
  const configDir = useSettingsStore((state) => state.configDir);
  const backups = useSettingsStore((state) => state.backups);
  const status = useSettingsStore((state) => state.status);
  const error = useSettingsStore((state) => state.error);
  const editor = useSettingsStore((state) => state.editor);
  const editorLoading = useSettingsStore((state) => state.editorLoading);
  const saving = useSettingsStore((state) => state.saving);
  const load = useSettingsStore((state) => state.load);
  const open = useSettingsStore((state) => state.open);
  const closeEditor = useSettingsStore((state) => state.closeEditor);
  const save = useSettingsStore((state) => state.save);
  const restore = useSettingsStore((state) => state.restore);
  const backupAll = useSettingsStore((state) => state.backupAll);

  const [xml, setXml] = useState("");
  const [acknowledged, setAcknowledged] = useState(false);
  const [restoreTarget, setRestoreTarget] = useState<RestoreTarget | null>(null);

  const refresh = useCallback(() => {
    void load().catch(() => {
      /* the store keeps the error for the ErrorState */
    });
  }, [load]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  useRefreshOn("settings", refresh);

  const openEditor = async (filename: string) => {
    try {
      const detail = await open(filename);
      setXml(detail.xml ?? "");
      setAcknowledged(false);
      if (detail.game_running) {
        notify.warn("检测到 RimWorld 正在运行：保存前请先退出游戏");
      }
    } catch (openError) {
      notify.error(`读取失败: ${toMessage(openError)}`);
    }
  };

  const onSave = async () => {
    if (!editor) return;
    if (!acknowledged) {
      notify.error("请先勾选「我已关闭 RimWorld」");
      return;
    }
    try {
      const result = await save(editor.filename, xml, true);
      if (result.game_running) {
        notify.warn(`已保存，但检测到游戏正在运行，重启后生效（备份 ${result.backup}）`);
      } else {
        notify.success(`已保存（备份 ${result.backup}）`);
      }
    } catch (saveError) {
      notify.error(`保存失败: ${toMessage(saveError)}`);
    }
  };

  const confirmRestore = async () => {
    if (!restoreTarget) return;
    const target = restoreTarget;
    setRestoreTarget(null);
    try {
      const result = await restore(target.file, target.backup, true);
      notify.success(`已从 ${result.restored_from} 恢复 ${target.file}`);
      if (editor?.filename === target.file) await openEditor(target.file);
    } catch (restoreError) {
      notify.error(`恢复失败: ${toMessage(restoreError)}`);
    }
  };

  const onBackupAll = async () => {
    try {
      const count = await backupAll();
      notify.success(`已备份 ${count} 个设置文件`);
    } catch (backupError) {
      notify.error(`备份失败: ${toMessage(backupError)}`);
    }
  };

  const showEmpty = status !== "loading" && files.length === 0;

  return (
    <div className="card" id="mod-settings-card">
      <div className="card-header" style={{ justifyContent: "space-between" }}>
        <span>🛠 Mod 设置文件（Config/Mod_*.xml）</span>
        <div style={{ display: "flex", gap: 6 }}>
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            id="btn-mod-settings-refresh"
            data-testid="action-mod-settings-refresh"
            onClick={refresh}
          >
            🔄 刷新
          </button>
          <button
            type="button"
            className="btn btn-primary btn-sm"
            id="btn-mod-settings-backup-all"
            data-testid="action-mod-settings-backup-all"
            disabled={saving}
            onClick={() => void onBackupAll()}
          >
            💾 备份全部
          </button>
        </div>
      </div>

      <div
        id="mod-settings-status"
        data-testid="status-settings"
        style={{ fontSize: 11, color: "var(--gray-text)", padding: "4px 0" }}
      >
        {configDir
          ? `配置目录: ${configDir}`
          : "未找到 RimWorld 配置目录（可在配置中设置 config_dir）"}
      </div>

      {editor ? (
        <div
          id="mod-settings-editor"
          data-testid="dialog-mod-settings-editor"
          style={{ padding: "8px 0", borderTop: "1px solid var(--border)" }}
        >
          <div
            id="mod-settings-editor-title"
            data-testid="status-mod-settings-editor-title"
            style={{ fontSize: 12, fontWeight: 600, marginBottom: 6 }}
          >
            {editor.filename}（{editor.rootTag || "XML"}）
          </div>
          <textarea
            id="mod-settings-xml"
            data-testid="field-mod-settings-xml"
            spellCheck={false}
            value={xml}
            onChange={(event) => setXml(event.target.value)}
            style={{
              width: "100%",
              height: 260,
              fontFamily: "var(--font-mono)",
              fontSize: 12,
              background: "var(--card)",
              color: "var(--text)",
              border: "1px solid var(--border)",
              borderRadius: 4,
              padding: 6,
            }}
          />
          <label className="check-label" style={{ marginTop: 8, display: "block" }}>
            <input
              type="checkbox"
              id="mod-settings-ack"
              data-testid="field-mod-settings-ack"
              checked={acknowledged}
              onChange={(event) => setAcknowledged(event.target.checked)}
            />{" "}
            我已关闭 RimWorld（写入前必须确认）
          </label>
          <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
            <button
              type="button"
              className="btn btn-primary"
              id="btn-mod-settings-save"
              data-testid="action-mod-settings-save"
              disabled={saving || editorLoading}
              onClick={() => void onSave()}
            >
              💾 保存
            </button>
            <button
              type="button"
              className="btn btn-ghost"
              id="btn-mod-settings-restore-latest"
              data-testid="action-mod-settings-restore-latest"
              disabled={saving}
              onClick={() => setRestoreTarget({ file: editor.filename, backup: "" })}
            >
              ↩️ 恢复最近备份
            </button>
            <button
              type="button"
              className="btn btn-ghost"
              id="btn-mod-settings-close"
              data-testid="action-mod-settings-close"
              onClick={closeEditor}
            >
              ✕ 关闭
            </button>
          </div>
        </div>
      ) : null}

      <div id="mod-settings-list" data-testid="list-mod-settings" style={{ fontSize: 12 }}>
        {status === "loading" && files.length === 0 ? (
          <LoadingState message="加载中..." testid="status-settings-loading" />
        ) : status === "error" && files.length === 0 ? (
          <ErrorState
            message={`读取失败: ${error ?? "未知错误"}`}
            onRetry={refresh}
            testid="status-settings-error"
          />
        ) : showEmpty ? (
          <div style={{ color: "var(--gray-text)", padding: "8px 0" }}>
            没有 Mod_*.xml 设置文件
          </div>
        ) : (
          files.map((file) => (
            <div
              className="mod-row"
              key={file.filename}
              data-testid={`item-mod-setting-${file.filename}`}
              style={rowStyle}
            >
              <div className="mod-info" style={{ minWidth: 0, flex: 1 }}>
                <div className="mod-name">{file.filename}</div>
                <div className="mod-meta" style={metaStyle}>
                  <span>{formatBytes(file.size ?? 0)}</span>
                  <span>{formatTimestamp(file.mtime ?? "")}</span>
                  {file.associated ? null : <span>未关联</span>}
                  {file.workshop_id ? <span>Workshop {file.workshop_id}</span> : null}
                  {file.package_id ? <span>{file.package_id}</span> : null}
                </div>
              </div>
              <div style={{ display: "flex", gap: 6, flexShrink: 0 }}>
                <button
                  type="button"
                  className="btn btn-ghost btn-sm mod-setting-edit"
                  data-file={file.filename}
                  data-testid={`action-mod-setting-edit-${file.filename}`}
                  onClick={() => void openEditor(file.filename)}
                >
                  ✏️ 编辑
                </button>
                <button
                  type="button"
                  className="btn btn-ghost btn-sm mod-setting-restore"
                  data-file={file.filename}
                  data-testid={`action-mod-setting-restore-${file.filename}`}
                  onClick={() => setRestoreTarget({ file: file.filename, backup: "" })}
                >
                  ↩️ 恢复
                </button>
              </div>
            </div>
          ))
        )}
      </div>

      <div style={{ fontSize: 12, color: "var(--gray-text)", padding: "8px 0", borderTop: "1px solid var(--border)" }}>
        <div style={{ fontWeight: 600, marginBottom: 4 }}>🗂 设置备份</div>
        <div id="mod-settings-backups" data-testid="list-mod-setting-backups">
          {backups.length === 0 ? (
            <span style={{ color: "var(--gray-text)" }}>暂无备份</span>
          ) : (
            backups.slice(0, 30).map((backup) => (
              <div
                key={backup.filename}
                data-testid={`item-mod-backup-${backup.filename}`}
                style={{ display: "flex", gap: 8, alignItems: "center", padding: "2px 0" }}
              >
                <span style={{ flex: 1, wordBreak: "break-all" }}>{backup.source}</span>
                <span style={{ color: "var(--gray-text)" }}>
                  {formatTimestamp(backup.mtime ?? "")}
                </span>
                <span style={{ color: "var(--gray-text)" }}>
                  {formatBytes(backup.size ?? 0)}
                </span>
                <button
                  type="button"
                  className="btn btn-ghost btn-sm mod-backup-restore"
                  data-file={backup.source}
                  data-backup={backup.filename}
                  data-testid={`action-mod-backup-restore-${backup.filename}`}
                  onClick={() =>
                    setRestoreTarget({ file: backup.source ?? "", backup: backup.filename })
                  }
                >
                  ↩️ 恢复
                </button>
              </div>
            ))
          )}
        </div>
      </div>

      <ConfirmDialog
        open={restoreTarget !== null}
        name="mod-settings-restore"
        title="恢复设置文件"
        message={
          restoreTarget
            ? `将用备份覆盖 ${restoreTarget.file}。请先完全退出 RimWorld，否则游戏退出时会覆盖回去。继续？`
            : ""
        }
        confirmLabel="恢复"
        danger
        onConfirm={() => void confirmRestore()}
        onCancel={() => setRestoreTarget(null)}
      />
    </div>
  );
}

export default SettingsPanel;
