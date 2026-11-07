/**
 * Backups panel — grouped mod backups with per-version restore and cleanup.
 *
 * Anchors are canonical testids: `list-backups`, `status-backup-dir`,
 * `action-backup-refresh`, `action-backup-cleanup`, `item-backup-<workshopId>`,
 * `status-backup-folder`, `list-backup-versions-<workshopId>` and the
 * per-action `action-backup-*` testids. The legacy ids are gone.
 *
 * Confirmation dialogs use the shared `ConfirmDialog` instead of native
 * `confirm()`; the legacy spec never drives them, and the cleanup prompt
 * became an inline "keep N" field.
 */
import { useCallback, useEffect, useMemo, useState } from "react";

import { toMessage } from "../api/client";
import type { components } from "../api/generated";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { EmptyState } from "../components/EmptyState";
import { ErrorState } from "../components/ErrorState";
import { LoadingState } from "../components/LoadingState";
import { useBackupsStore } from "../store/backups";
import { useRefreshOn } from "../store/bus";
import { notify } from "../store/ui";

type BackupItem = components["schemas"]["BackupItem"];

interface RestoreTarget {
  workshopId: string;
  filename?: string;
  label: string;
}

/** ISO timestamp → zh-CN local string; returns the input unchanged if unparsable. */
function formatTimestamp(iso: string): string {
  try {
    return new Date(iso).toLocaleString("zh-CN");
  } catch {
    return iso;
  }
}

const rowStyle = { display: "flex", gap: 10, alignItems: "center", padding: "6px 4px" } as const;
const metaStyle = {
  display: "flex",
  gap: 10,
  flexWrap: "wrap",
  color: "var(--gray-text)",
  fontSize: 11,
} as const;

export function BackupsPanel() {
  const backups = useBackupsStore((state) => state.backups);
  const backupDir = useBackupsStore((state) => state.backupDir);
  const status = useBackupsStore((state) => state.status);
  const error = useBackupsStore((state) => state.error);
  const load = useBackupsStore((state) => state.load);
  const restore = useBackupsStore((state) => state.restore);
  const remove = useBackupsStore((state) => state.remove);
  const cleanup = useBackupsStore((state) => state.cleanup);

  const [expanded, setExpanded] = useState<ReadonlySet<string>>(new Set());
  const [keepCount, setKeepCount] = useState("5");
  const [restoreTarget, setRestoreTarget] = useState<RestoreTarget | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<string | null>(null);

  const refresh = useCallback(() => {
    void load().catch(() => {
      /* the store keeps the error for the ErrorState */
    });
  }, [load]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  useRefreshOn("backups", refresh);

  const groups = useMemo(() => {
    const map = new Map<string, BackupItem[]>();
    for (const backup of backups) {
      const bucket = map.get(backup.workshop_id) ?? [];
      bucket.push(backup);
      map.set(backup.workshop_id, bucket);
    }
    return map;
  }, [backups]);

  const toggleVersions = (workshopId: string) => {
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(workshopId)) next.delete(workshopId);
      else next.add(workshopId);
      return next;
    });
  };

  const onCleanup = async () => {
    const keep = Number.parseInt(keepCount, 10);
    if (!Number.isFinite(keep) || keep < 0) {
      notify.error("请输入有效的保留数量");
      return;
    }
    try {
      const deleted = await cleanup(keep);
      notify.success(`已清理 ${deleted} 个旧备份`);
    } catch (cleanupError) {
      notify.error(`清理失败: ${toMessage(cleanupError)}`);
    }
  };

  const confirmRestore = async () => {
    if (!restoreTarget) return;
    const target = restoreTarget;
    setRestoreTarget(null);
    try {
      const result = await restore(target.workshopId, target.filename);
      if (result.ok) notify.success(`已恢复: ${result.restored_folder ?? target.label}`);
      else notify.error(result.msg || "回滚失败");
    } catch (restoreError) {
      notify.error(`回滚失败: ${toMessage(restoreError)}`);
    }
  };

  const confirmDelete = async () => {
    if (!deleteTarget) return;
    const filename = deleteTarget;
    setDeleteTarget(null);
    try {
      await remove(filename);
      notify.success("已删除");
    } catch (deleteError) {
      notify.error(`失败: ${toMessage(deleteError)}`);
    }
  };

  const showEmpty = status !== "loading" && backups.length === 0;

  return (
    <>
      <div className="card" style={{ flex: 1, overflowY: "auto" }}>
        <div className="card-header" style={{ justifyContent: "space-between" }}>
          <span>💾 Mod 备份</span>
          <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
            <input
              type="number"
              min={0}
              data-testid="field-backup-keep"
              aria-label="每个 Mod 保留的备份数量"
              value={keepCount}
              onChange={(event) => setKeepCount(event.target.value)}
              style={{ width: 64, padding: "4px 6px", fontSize: 12 }}
            />
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              data-testid="action-backup-refresh"
              onClick={refresh}
            >
              🔄 刷新
            </button>
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              data-testid="action-backup-cleanup"
              onClick={() => void onCleanup()}
            >
              🧹 清理旧备份
            </button>
          </div>
        </div>

        <div
          data-testid="status-backup-dir"
          style={{ fontSize: 11, color: "var(--gray-text)", padding: "4px 0" }}
        >
          {backups.length > 0 && backupDir ? `备份目录: ${backupDir}` : ""}
        </div>

        <div data-testid="list-backups" style={{ fontSize: 12 }}>
          {status === "loading" && backups.length === 0 ? (
            <LoadingState message="加载中..." testid="status-backups-loading" />
          ) : status === "error" && backups.length === 0 ? (
            <ErrorState
              message={`加载失败: ${error ?? "未知错误"}`}
              onRetry={refresh}
              testid="status-backups-error"
            />
          ) : showEmpty ? (
            <EmptyState
              title="暂无备份。更新 Mod 时会自动创建备份。"
              testid="status-backups-empty"
            />
          ) : (
            Array.from(groups.entries()).map(([workshopId, versions]) => {
              const latest = versions[0];
              const isOpen = expanded.has(workshopId);
              return (
                <div key={workshopId}>
                  <div
                    className="mod-row"
                    data-testid={`item-backup-${workshopId}`}
                    style={rowStyle}
                  >
                    <div className="mod-icon" aria-hidden="true">
                      💾
                    </div>
                    <div className="mod-info" style={{ minWidth: 0, flex: 1 }}>
                      <div data-testid="status-backup-folder">{latest.folder_name}</div>
                      <div className="mod-meta" style={metaStyle}>
                        <span>ID: {workshopId}</span>
                        <span>{versions.length} 个版本</span>
                        <span>最新: {formatTimestamp(latest.timestamp)}</span>
                        <span>{latest.size_mb} MB</span>
                      </div>
                    </div>
                    <div style={{ display: "flex", gap: 4, alignItems: "center" }}>
                      <button
                        type="button"
                        className="btn btn-primary btn-sm"
                        data-testid={`action-backup-restore-${workshopId}`}
                        onClick={() =>
                          setRestoreTarget({
                            workshopId,
                            label: latest.folder_name,
                            filename: undefined,
                          })
                        }
                      >
                        ↩ 回滚
                      </button>
                      <button
                        type="button"
                        className="btn btn-ghost btn-sm"
                        data-testid={`action-backup-versions-${workshopId}`}
                        aria-expanded={isOpen}
                        onClick={() => toggleVersions(workshopId)}
                      >
                        📋 {versions.length}版
                      </button>
                    </div>
                  </div>

                  <div
                    className="backup-versions"
                    data-testid={`list-backup-versions-${workshopId}`}
                    style={{ display: isOpen ? undefined : "none", padding: "0 16px 12px" }}
                  >
                    {versions.map((version) => (
                      <div
                        key={version.filename}
                        data-testid={`item-backup-version-${version.filename}`}
                        style={{
                          display: "flex",
                          alignItems: "center",
                          gap: 8,
                          padding: "4px 8px",
                          fontSize: 11,
                        }}
                      >
                        <span style={{ color: "var(--gray-text)" }}>
                          {formatTimestamp(version.timestamp)}
                        </span>
                        <span>{version.size_mb} MB</span>
                        <span style={{ marginLeft: "auto", display: "flex", gap: 4 }}>
                          <button
                            type="button"
                            className="btn btn-ghost btn-sm"
                            data-restore-id={version.filename}
                            data-testid={`action-backup-restore-version-${version.filename}`}
                            aria-label={`恢复版本 ${version.filename}`}
                            onClick={() =>
                              setRestoreTarget({
                                workshopId,
                                filename: version.filename,
                                label: version.filename,
                              })
                            }
                          >
                            ↩
                          </button>
                          <button
                            type="button"
                            className="btn btn-ghost btn-sm"
                            data-testid={`action-backup-delete-${version.filename}`}
                            aria-label={`删除备份 ${version.filename}`}
                            onClick={() => setDeleteTarget(version.filename)}
                          >
                            ✕
                          </button>
                        </span>
                      </div>
                    ))}
                  </div>
                </div>
              );
            })
          )}
        </div>
      </div>

      <ConfirmDialog
        open={restoreTarget !== null}
        name="backup-restore"
        title="回滚备份"
        message={
          restoreTarget
            ? restoreTarget.filename
              ? `恢复 ${restoreTarget.filename}？当前版本将被覆盖。`
              : `恢复到 ${restoreTarget.workshopId} 的最新备份？当前版本将被覆盖。`
            : ""
        }
        confirmLabel="回滚"
        danger
        onConfirm={() => void confirmRestore()}
        onCancel={() => setRestoreTarget(null)}
      />
      <ConfirmDialog
        open={deleteTarget !== null}
        name="backup-delete"
        title="删除备份"
        message={deleteTarget ? `删除备份: ${deleteTarget}？` : ""}
        confirmLabel="删除"
        danger
        onConfirm={() => void confirmDelete()}
        onCancel={() => setDeleteTarget(null)}
      />
    </>
  );
}

export default BackupsPanel;
