/**
 * Tags panel — tag list, per-tag mod folders and (new in the React rewrite)
 * create / rename / delete management.
 *
 * Anchors are canonical testids: `list-tags`, `item-tag-<tag>`,
 * `status-tags-detail`, `field-tag-folder`, `field-tag-name`,
 * `action-tag-add`, `field-tag-rename`, `action-tag-rename` and
 * `action-tag-delete`. The legacy ids/classes are gone.
 */
import { useCallback, useEffect, useState } from "react";

import { toMessage } from "../api/client";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { EmptyState } from "../components/EmptyState";
import { ErrorState } from "../components/ErrorState";
import { LoadingState } from "../components/LoadingState";
import { useRefreshOn } from "../store/bus";
import { useTagsStore } from "../store/tags";
import { notify } from "../store/ui";

const rowStyle = {
  display: "flex",
  alignItems: "center",
  gap: 10,
  padding: "6px 8px",
  cursor: "pointer",
  borderRadius: "var(--radius-sm)",
} as const;

export function TagsPanel() {
  const tags = useTagsStore((state) => state.tags);
  const detail = useTagsStore((state) => state.detail);
  const status = useTagsStore((state) => state.status);
  const detailStatus = useTagsStore((state) => state.detailStatus);
  const error = useTagsStore((state) => state.error);
  const mutating = useTagsStore((state) => state.mutating);
  const load = useTagsStore((state) => state.load);
  const loadDetail = useTagsStore((state) => state.loadDetail);
  const clearDetail = useTagsStore((state) => state.clearDetail);
  const addTag = useTagsStore((state) => state.addTag);
  const removeTag = useTagsStore((state) => state.removeTag);
  const renameTag = useTagsStore((state) => state.renameTag);

  const [folder, setFolder] = useState("");
  const [newTag, setNewTag] = useState("");
  const [renameTo, setRenameTo] = useState("");
  const [deleteOpen, setDeleteOpen] = useState(false);

  const refresh = useCallback(() => {
    void load().catch(() => {
      /* the store keeps the error for the ErrorState */
    });
  }, [load]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  useRefreshOn("tags", refresh);

  const selectTag = async (tag: string) => {
    setRenameTo(tag);
    try {
      await loadDetail(tag);
    } catch (detailError) {
      notify.error(`读取标签失败: ${toMessage(detailError)}`);
    }
  };

  const onCreate = async () => {
    const targetFolder = folder.trim();
    const tag = newTag.trim();
    if (!targetFolder || !tag) {
      notify.error("请填写 Mod 文件夹与标签名");
      return;
    }
    try {
      await addTag(targetFolder, tag);
      notify.success(`已添加标签 #${tag}`);
      setNewTag("");
    } catch (createError) {
      notify.error(`添加失败: ${toMessage(createError)}`);
    }
  };

  const onRename = async () => {
    if (!detail) return;
    const next = renameTo.trim();
    if (!next) {
      notify.error("请输入新的标签名");
      return;
    }
    try {
      const count = await renameTag(detail.tag, next);
      notify.success(`已重命名标签 #${detail.tag} → #${next}（${count} 个 Mod）`);
      setRenameTo(next);
    } catch (renameError) {
      notify.error(`重命名失败: ${toMessage(renameError)}`);
    }
  };

  const onDelete = async () => {
    if (!detail) return;
    const target = detail;
    setDeleteOpen(false);
    try {
      for (const modFolder of target.folders) {
        await removeTag(modFolder, target.tag);
      }
      clearDetail();
      setRenameTo("");
      notify.success(`已删除标签 #${target.tag}`);
    } catch (deleteError) {
      notify.error(`删除失败: ${toMessage(deleteError)}`);
    }
  };

  return (
    <>
      <div className="card" style={{ flex: 1, overflowY: "auto" }}>
        <div className="card-header">🏷 Mod 标签</div>
        <div data-testid="list-tags" style={{ fontSize: 12 }}>
          {status === "loading" && tags.length === 0 ? (
            <LoadingState message="加载中..." testid="status-tags-loading" />
          ) : status === "error" && tags.length === 0 ? (
            <ErrorState
              message={`Error: ${error ?? "加载失败"}`}
              onRetry={refresh}
              testid="status-tags-error"
            />
          ) : tags.length === 0 ? (
            <EmptyState title="No tags yet" testid="status-tags-empty" />
          ) : (
            tags.map((entry) => {
              const selected = detail?.tag === entry.tag;
              return (
                <div
                  key={entry.tag}
                  data-testid={`item-tag-${entry.tag}`}
                  data-selected={selected ? "true" : "false"}
                  role="button"
                  tabIndex={0}
                  style={{
                    ...rowStyle,
                    background: selected ? "var(--gray)" : undefined,
                  }}
                  onClick={() => void selectTag(entry.tag)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" || event.key === " ") {
                      event.preventDefault();
                      void selectTag(entry.tag);
                    }
                  }}
                >
                  <span>#{entry.tag}</span>
                  <span style={{ color: "var(--gray-text)" }}>{entry.count ?? 0} mods</span>
                </div>
              );
            })
          )}
        </div>
      </div>

      <div className="card">
        <div className="card-header">标签详情</div>
        <div data-testid="status-tags-detail" style={{ fontSize: 12 }}>
          {detailStatus === "loading" ? (
            <LoadingState message="加载中..." testid="status-tag-detail-loading" />
          ) : detail ? (
            <>
              <h4 style={{ marginBottom: 6 }}>#{detail.tag}</h4>
              {detail.folders.length === 0 ? (
                <div style={{ color: "var(--gray-text)" }}>None</div>
              ) : (
                detail.folders.map((modFolder) => <div key={modFolder}>{modFolder}</div>)
              )}
            </>
          ) : (
            <div style={{ color: "var(--gray-text)" }}>点击标签查看 Mod 列表</div>
          )}
        </div>
      </div>

      <div className="card">
        <div className="card-header">标签管理</div>

        <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "flex-end" }}>
          <div className="form-group">
            <label>
              Mod 文件夹
              <input
                data-testid="field-tag-folder"
                type="text"
                placeholder="例如：CoolModFolder"
                value={folder}
                onChange={(event) => setFolder(event.target.value)}
              />
            </label>
          </div>
          <div className="form-group">
            <label>
              标签名
              <input
                data-testid="field-tag-name"
                type="text"
                placeholder="例如：combat"
                value={newTag}
                onChange={(event) => setNewTag(event.target.value)}
              />
            </label>
          </div>
          <button
            type="button"
            className="btn btn-primary btn-sm"
            data-testid="action-tag-add"
            disabled={mutating}
            onClick={() => void onCreate()}
          >
            ➕ 添加标签
          </button>
        </div>

        <div
          style={{
            display: "flex",
            gap: 8,
            flexWrap: "wrap",
            alignItems: "flex-end",
            marginTop: 12,
            paddingTop: 12,
            borderTop: "1px solid var(--border)",
          }}
        >
          <div className="form-group">
            <label>
              重命名当前标签
              <input
                data-testid="field-tag-rename"
                type="text"
                placeholder="新的标签名"
                value={renameTo}
                disabled={!detail}
                onChange={(event) => setRenameTo(event.target.value)}
              />
            </label>
          </div>
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            data-testid="action-tag-rename"
            disabled={!detail || mutating}
            onClick={() => void onRename()}
          >
            ✏️ 重命名
          </button>
          <button
            type="button"
            className="btn btn-danger btn-sm"
            data-testid="action-tag-delete"
            disabled={!detail || mutating}
            onClick={() => setDeleteOpen(true)}
          >
            🗑 删除标签
          </button>
        </div>
      </div>

      <ConfirmDialog
        open={deleteOpen}
        name="tag-delete"
        title="删除标签"
        message={
          detail
            ? `删除标签 #${detail.tag}？将从 ${detail.folders.length} 个 Mod 上移除。`
            : ""
        }
        confirmLabel="删除"
        danger
        onConfirm={() => void onDelete()}
        onCancel={() => setDeleteOpen(false)}
      />
    </>
  );
}

export default TagsPanel;
