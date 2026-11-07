/**
 * Mods panel — installed mods, health/compatibility badges, batch
 * enable/disable, the "only active" filter and collection export.
 *
 * Parity contract with the legacy `src/panels/mods.ts` / `main.ts` rendering
 * and `mods.spec.ts`: `#mod-list`, `#mod-count-label`, `#mods-only-active`,
 * `#btn-mods-select-all`, `#btn-mods-enable`, `#btn-mods-disable`,
 * `#btn-export-collection`, `.mod-row`, `.mod-select`, `data-folder`,
 * `data-active`, `.mod-active-badge`, `.health-badge`, `.compat-badge`,
 * `#btn-copy-ids`, `#btn-copy-md`, `#btn-collection-close`.
 *
 * The sidebar's `#mod-count` badge is fed by the shared `mods` slice, so this
 * panel only renders the list and forwards mutations through the store.
 */
import { useCallback, useEffect, useState } from "react";

import { apiRequest, toMessage } from "../api/client";
import { EmptyState } from "../components/EmptyState";
import { ErrorState } from "../components/ErrorState";
import { LoadingState } from "../components/LoadingState";
import { useRefreshOn } from "../store/bus";
import { useModsStore } from "../store/mods";
import { notify } from "../store/ui";

const HEALTH_LABELS: Record<string, string> = {
  maintained: "🟢 活跃",
  stale: "🟡 停更",
  abandoned: "🔴 废弃",
  removed: "⚫ 下架",
  unknown: "⚪ 未知",
};

interface CollectionExport {
  total: number;
  mods: { workshop_id: string; name: string; url: string }[];
  ids: string[];
  markdown: string;
}

const rowStyle = { display: "flex", gap: 10, alignItems: "flex-start", padding: "6px 4px" } as const;
const metaStyle = {
  display: "flex",
  gap: 10,
  flexWrap: "wrap",
  color: "var(--gray-text)",
  fontSize: 11,
} as const;

export function ModsPanel() {
  const items = useModsStore((state) => state.items);
  const onlyActive = useModsStore((state) => state.onlyActive);
  const selectedIds = useModsStore((state) => state.selectedIds);
  const health = useModsStore((state) => state.health);
  const compat = useModsStore((state) => state.compat);
  const status = useModsStore((state) => state.status);
  const error = useModsStore((state) => state.error);
  const load = useModsStore((state) => state.load);
  const loadBadges = useModsStore((state) => state.loadBadges);
  const setOnlyActive = useModsStore((state) => state.setOnlyActive);
  const toggleSelected = useModsStore((state) => state.toggleSelected);
  const selectAll = useModsStore((state) => state.selectAll);
  const clearSelection = useModsStore((state) => state.clearSelection);
  const setActive = useModsStore((state) => state.setActive);

  const [exporting, setExporting] = useState(false);
  const [exportData, setExportData] = useState<CollectionExport | null>(null);

  const refresh = useCallback(() => {
    void load().catch(() => {
      /* the store keeps the error for the ErrorState */
    });
  }, [load]);

  useEffect(() => {
    refresh();
    void loadBadges();
  }, [refresh, loadBadges]);

  useRefreshOn("mods", refresh);

  const visible = onlyActive ? items.filter((item) => item.active) : items;
  const allSelected =
    visible.length > 0 && visible.every((item) => selectedIds.includes(item.package_id));

  const toggleAll = () => {
    if (allSelected) clearSelection();
    else selectAll();
  };

  const applyActive = async (enable: boolean) => {
    if (selectedIds.length === 0) {
      notify.error("请先勾选要操作的 Mod");
      return;
    }
    try {
      await setActive(selectedIds, enable);
      notify.success(`${enable ? "启用" : "禁用"} ${selectedIds.length} 个 Mod`);
    } catch (applyError) {
      notify.error(`操作失败: ${toMessage(applyError)}`);
    }
  };

  const onExport = async () => {
    setExporting(true);
    try {
      // One-shot action with no shared state: the shared `mods` slice has no
      // export action and FE-2 may not edit it, so the request helper is used
      // directly (never raw `fetch`).
      const data = (await apiRequest("get", "/api/mods/export-collection")) as CollectionExport;
      setExportData(data);
    } catch (exportError) {
      notify.error(`导出失败: ${toMessage(exportError)}`);
    } finally {
      setExporting(false);
    }
  };

  const copyText = async (text: string, successMessage: string) => {
    try {
      await navigator.clipboard.writeText(text);
      notify.success(successMessage);
    } catch {
      notify.error("复制失败");
    }
  };

  const incompatible = new Set(
    (compat?.groups?.incompatible ?? []).map((entry) => entry.folder),
  );
  const unknown = new Set((compat?.groups?.unknown ?? []).map((entry) => entry.folder));

  const renderBadges = (folder: string) => {
    const healthStatus = health[folder];
    const healthBadge = healthStatus ? (
      <span className="health-badge" style={{ fontSize: 11, marginLeft: 8 }}>
        {HEALTH_LABELS[healthStatus] ?? healthStatus}
      </span>
    ) : null;
    let compatBadge = null;
    if (incompatible.has(folder)) {
      compatBadge = (
        <span
          className="compat-badge"
          style={{ fontSize: 10, marginLeft: 6, padding: "1px 4px", color: "#f7768e" }}
        >
          ❌ 不兼容
        </span>
      );
    } else if (unknown.has(folder)) {
      compatBadge = (
        <span
          className="compat-badge"
          style={{
            fontSize: 10,
            marginLeft: 6,
            padding: "1px 4px",
            color: "var(--gray-text)",
          }}
        >
          ❓ 未知
        </span>
      );
    }
    return (
      <>
        {healthBadge}
        {compatBadge}
      </>
    );
  };

  const showEmpty = status !== "loading" && items.length === 0;

  return (
    <div className="card" style={{ flex: 1, minHeight: 0 }}>
      <div
        className="card-header"
        id="mods-header"
        style={{ justifyContent: "space-between" }}
        data-testid="status-mods-header"
      >
        <span>
          📊 已安装 Mod
          {compat?.rimworld_version ? (
            <span className="compat-summary" style={{ fontSize: 11, color: "var(--gray-text)", marginLeft: 8 }}>
              RW {compat.rimworld_version} · {incompatible.size} 不兼容
            </span>
          ) : null}
        </span>
        <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            id="btn-export-collection"
            data-testid="action-export-collection"
            disabled={exporting}
            onClick={() => void onExport()}
          >
            {exporting ? "⏳ 生成中..." : "📤 导出合集"}
          </button>
          <span
            id="mod-count-label"
            data-testid="status-mod-count"
            style={{ fontWeight: 400, fontSize: 12, color: "var(--gray-text)" }}
          >
            {items.length} 个
          </span>
        </div>
      </div>

      <div
        className="mods-toolbar"
        style={{
          display: "flex",
          gap: 6,
          alignItems: "center",
          padding: "6px 0",
          borderBottom: "1px solid var(--border)",
        }}
      >
        <label className="check-label" style={{ margin: 0 }}>
          <input
            type="checkbox"
            id="mods-only-active"
            data-testid="field-mods-only-active"
            checked={onlyActive}
            onChange={(event) => setOnlyActive(event.target.checked)}
          />{" "}
          仅看已启用
        </label>
        <button
          type="button"
          className="btn btn-ghost btn-sm"
          id="btn-mods-select-all"
          data-testid="action-mods-select-all"
          onClick={toggleAll}
        >
          全选
        </button>
        <button
          type="button"
          className="btn btn-primary btn-sm"
          id="btn-mods-enable"
          data-testid="action-mods-enable"
          onClick={() => void applyActive(true)}
        >
          ✅ 启用
        </button>
        <button
          type="button"
          className="btn btn-ghost btn-sm"
          id="btn-mods-disable"
          data-testid="action-mods-disable"
          onClick={() => void applyActive(false)}
        >
          ⛔ 禁用
        </button>
      </div>

      <div id="mod-list" data-testid="list-mods" style={{ fontSize: 12 }}>
        {exportData ? (
          <div style={{ padding: 12 }} data-testid="status-collection-export">
            <div style={{ marginBottom: 12, display: "flex", gap: 8, alignItems: "center" }}>
              <strong>📦 Steam 合集 — {exportData.total} 个 Mod</strong>
              <button
                type="button"
                className="btn btn-primary btn-sm"
                id="btn-copy-ids"
                data-testid="action-collection-copy-ids"
                onClick={() =>
                  void copyText(
                    exportData.ids.join(" "),
                    `已复制 ${exportData.ids.length} 个 Workshop ID`,
                  )
                }
              >
                📋 复制 {exportData.ids.length} 个 ID
              </button>
              <button
                type="button"
                className="btn btn-ghost btn-sm"
                id="btn-copy-md"
                data-testid="action-collection-copy-markdown"
                onClick={() => void copyText(exportData.markdown, "已复制 Markdown")}
              >
                📝 复制 Markdown
              </button>
              <button
                type="button"
                className="btn btn-ghost btn-sm"
                id="btn-collection-close"
                data-testid="action-collection-close"
                onClick={() => setExportData(null)}
              >
                ✕ 关闭
              </button>
            </div>
            <div style={{ fontSize: 11, color: "var(--gray-text)", marginBottom: 8 }}>
              粘贴到 Steam 合集编辑器 → 添加项目 → 批量粘贴 Workshop ID
            </div>
            <div
              style={{
                background: "var(--gray)",
                borderRadius: 4,
                padding: 8,
                fontSize: 11,
                fontFamily: "var(--font-mono)",
                maxHeight: 200,
                overflowY: "auto",
                whiteSpace: "pre-wrap",
                wordBreak: "break-all",
              }}
            >
              {exportData.ids.join(" ")}
            </div>
            <div style={{ marginTop: 8, fontSize: 11, maxHeight: 300, overflowY: "auto" }}>
              {exportData.mods.map((mod) => (
                <div key={mod.workshop_id} style={{ padding: "2px 0" }}>
                  <a href={mod.url} target="_blank" rel="noreferrer">
                    {mod.name}
                  </a>
                  <span style={{ color: "var(--gray-text)", marginLeft: 4 }}>
                    {mod.workshop_id}
                  </span>
                </div>
              ))}
            </div>
          </div>
        ) : status === "loading" && items.length === 0 ? (
          <LoadingState message="加载中..." testid="status-mods-loading" />
        ) : status === "error" && items.length === 0 ? (
          <ErrorState
            message={error ?? "加载失败"}
            onRetry={refresh}
            testid="status-mods-error"
          />
        ) : showEmpty ? (
          <EmptyState title="没有安装的 Mod" testid="status-mods-empty" />
        ) : visible.length === 0 ? (
          <EmptyState
            title="没有匹配的 Mod"
            message="当前筛选条件下没有 Mod"
            testid="status-mods-filtered"
          />
        ) : (
          visible.map((item) => (
            <div
              className="mod-row"
              key={item.package_id || item.folder}
              data-folder={item.folder}
              data-active={item.active ? "1" : "0"}
              data-testid={`item-mod-${item.package_id || item.folder}`}
              style={rowStyle}
            >
              <input
                type="checkbox"
                className="mod-select"
                data-package-id={item.package_id}
                title="选中以批量启用/禁用"
                aria-label={`选择 ${item.name || item.folder}`}
                checked={selectedIds.includes(item.package_id)}
                onChange={() => toggleSelected(item.package_id)}
              />
              <div className="mod-icon" aria-hidden="true">
                📦
              </div>
              <div className="mod-info" style={{ minWidth: 0, flex: 1 }}>
                <div className="mod-name">
                  {item.name}
                  {renderBadges(item.folder)}
                </div>
                <div className="mod-meta" style={metaStyle}>
                  <span>{item.folder}</span>
                  {item.workshop_id ? <span>Workshop {item.workshop_id}</span> : null}
                  {item.package_id ? <span>{item.package_id}</span> : null}
                  <span
                    className="mod-active-badge"
                    style={{ color: item.active ? "var(--green)" : "var(--gray-text)" }}
                  >
                    {item.active ? "✅ 已启用" : "⬜ 未启用"}
                  </span>
                </div>
              </div>
            </div>
          ))
        )}
      </div>
    </div>
  );
}

export default ModsPanel;
