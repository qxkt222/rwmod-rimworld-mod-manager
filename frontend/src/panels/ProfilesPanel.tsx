/**
 * Profiles panel (FE-3) — save / restore / delete ModsConfig.xml snapshots
 * plus the one-click undo of the last destructive operation.
 *
 * The legacy panel used native `window.prompt` / `window.confirm`; the React
 * version uses in-app dialogs (ui-redesign §5.4 says the old spec keeps
 * running against the legacy entry, so switching here is safe).
 *
 * Anchors are canonical testids: `action-profile-save`, `list-profiles`,
 * `item-profile-<name>`, `action-profile-restore-<name>`,
 * `action-profile-delete-<name>`, `status-profile-path`, `status-undo`,
 * `action-undo`, `dialog-profile-save`, `field-profile-name` and
 * `action-profile-save-confirm`. The legacy ids/classes are gone.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

import { toMessage } from "../api/client";
import { EmptyState } from "../components/EmptyState";
import { ErrorState } from "../components/ErrorState";
import { LoadingState } from "../components/LoadingState";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { formatTs } from "../dom";
import { useRefreshOn } from "../store/bus";
import { useProfilesStore } from "../store/profiles";
import { notify } from "../store/ui";

type PendingAction =
  | { kind: "restore"; name: string }
  | { kind: "delete"; name: string }
  | { kind: "undo" }
  | null;

interface NameDialogProps {
  open: boolean;
  saving: boolean;
  onCancel: () => void;
  onConfirm: (name: string) => void;
}

function NameDialog({ open, saving, onCancel, onConfirm }: NameDialogProps) {
  const [name, setName] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (open) {
      setName("");
      inputRef.current?.focus();
    }
  }, [open]);

  if (!open) return null;

  const submit = () => {
    const trimmed = name.trim();
    if (!trimmed || saving) return;
    onConfirm(trimmed);
  };

  return createPortal(
    <div className="dialog-overlay" data-testid="dialog-profile-save" role="presentation">
      <div className="dialog-backdrop" onClick={onCancel} />
      <div
        className="dialog-box"
        role="dialog"
        aria-modal="true"
        aria-label="保存 Profile"
        onKeyDown={(event) => {
          if (event.key === "Escape") {
            event.stopPropagation();
            onCancel();
          }
        }}
      >
        <h2 className="dialog-box__title">Profile 名称</h2>
        <input
          ref={inputRef}
          className="dialog-box__input"
          data-testid="field-profile-name"
          aria-label="Profile 名称"
          placeholder="Profile 名称（如：原版、中世纪、魔改）"
          value={name}
          onChange={(event) => setName(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") submit();
          }}
        />
        <div className="dialog-box__actions">
          <button type="button" className="btn btn-ghost" onClick={onCancel}>
            取消
          </button>
          <button
            type="button"
            className="btn btn-primary"
            data-testid="action-profile-save-confirm"
            disabled={!name.trim() || saving}
            onClick={submit}
          >
            {saving ? "保存中..." : "保存"}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}

export function ProfilesPanel() {
  const profiles = useProfilesStore((state) => state.profiles);
  const modsconfigPath = useProfilesStore((state) => state.modsconfigPath);
  const status = useProfilesStore((state) => state.status);
  const error = useProfilesStore((state) => state.error);
  const undoSnapshots = useProfilesStore((state) => state.undoSnapshots);
  const undoStatus = useProfilesStore((state) => state.undoStatus);
  const undoError = useProfilesStore((state) => state.undoError);

  const [saveOpen, setSaveOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [pending, setPending] = useState<PendingAction>(null);
  const [pendingBusy, setPendingBusy] = useState(false);
  const [undoMessage, setUndoMessage] = useState<string | null>(null);

  const load = useCallback(() => {
    void useProfilesStore
      .getState()
      .load()
      .catch(() => {
        /* error state rendered below */
      });
    void useProfilesStore.getState().loadUndo();
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  useRefreshOn("profiles", load);

  const handleSave = async (name: string) => {
    setSaving(true);
    try {
      const result = await useProfilesStore.getState().save(name);
      if (result.ok === false) {
        notify.error(result.msg || "保存失败");
      } else {
        notify.success(result.msg || "已保存");
        setSaveOpen(false);
      }
    } catch (saveError) {
      notify.error(`保存失败: ${toMessage(saveError)}`);
    } finally {
      setSaving(false);
    }
  };

  const confirmPending = async () => {
    if (!pending || pendingBusy) return;
    setPendingBusy(true);
    try {
      if (pending.kind === "delete") {
        await useProfilesStore.getState().remove(pending.name);
        notify.success("已删除");
      } else if (pending.kind === "restore") {
        const result = await useProfilesStore.getState().restore(pending.name);
        if (result.ok === false) {
          notify.error(result.msg || "恢复失败");
        } else {
          notify.success(`已启用: ${pending.name}（${result.mod_count ?? 0} 个 Mod）`);
        }
      } else {
        const result = await useProfilesStore.getState().undo();
        setUndoMessage(result.ok ? `✅ ${result.msg}` : `❌ ${result.msg}`);
        if (result.ok) notify.success(result.msg);
        else notify.error(result.msg);
      }
    } catch (actionError) {
      const label =
        pending.kind === "delete" ? "删除" : pending.kind === "restore" ? "切换" : "撤销";
      notify.error(`${label}失败: ${toMessage(actionError)}`);
      if (pending.kind === "undo") setUndoMessage(`❌ 撤销失败: ${toMessage(actionError)}`);
    } finally {
      setPendingBusy(false);
      setPending(null);
    }
  };

  const latestUndo = undoSnapshots[0];
  const confirmTitle =
    pending?.kind === "delete"
      ? `删除 profile "${pending.name}"？`
      : pending?.kind === "restore"
        ? `切换到 profile "${pending.name}"？`
        : "撤销上次操作？";
  const confirmMessage =
    pending?.kind === "restore"
      ? "当前 ModsConfig.xml 将被覆盖（会自动备份）。"
      : pending?.kind === "undo"
        ? "将恢复最近一次操作前的 ModsConfig.xml。"
        : undefined;

  return (
    <>
      <div className="card" style={{ flex: 1, overflowY: "auto" }}>
        <div className="card-header" style={{ display: "flex", justifyContent: "space-between" }}>
          <span>💾 Mod 配置档案</span>
          <button
            type="button"
            className="btn btn-primary btn-sm"
            data-testid="action-profile-save"
            onClick={() => setSaveOpen(true)}
          >
            💾 保存当前配置
          </button>
        </div>

        <div
          data-testid="status-profile-path"
          style={{ fontSize: 11, color: "var(--gray-text)", padding: "4px 16px" }}
        >
          {modsconfigPath ? `ModsConfig.xml: ${modsconfigPath}` : "未找到 ModsConfig.xml"}
        </div>

        <div data-testid="list-profiles">
          {status === "loading" && profiles.length === 0 ? (
            <LoadingState message="加载中..." testid="status-profiles-loading" />
          ) : status === "error" && profiles.length === 0 ? (
            <ErrorState
              message={error ?? "加载失败"}
              onRetry={() => void load()}
              testid="status-profiles-error"
            />
          ) : profiles.length === 0 ? (
            <EmptyState
              icon="💾"
              title="暂无存档"
              message="点击「保存当前配置」创建第一个 profile。"
              testid="status-profiles-empty"
            />
          ) : (
            profiles.map((profile) => (
              <div
                className="mod-row"
                key={profile.name}
                data-testid={`item-profile-${profile.name}`}
              >
                <div className="mod-icon">💾</div>
                <div className="mod-info">
                  <div className="mod-name">{profile.name}</div>
                  <div className="mod-meta">
                    <span>{profile.mod_count ?? 0} 个 Mod</span>
                    <span>{profile.saved_at ? formatTs(profile.saved_at) : ""}</span>
                    <span>{profile.size_kb ?? 0} KB</span>
                  </div>
                </div>
                <div style={{ display: "flex", gap: 4 }}>
                  <button
                    type="button"
                    className="btn btn-primary btn-sm"
                    data-testid={`action-profile-restore-${profile.name}`}
                    onClick={() => setPending({ kind: "restore", name: profile.name })}
                  >
                    ↩ 启用
                  </button>
                  <button
                    type="button"
                    className="btn btn-ghost btn-sm"
                    data-testid={`action-profile-delete-${profile.name}`}
                    onClick={() => setPending({ kind: "delete", name: profile.name })}
                  >
                    ✕
                  </button>
                </div>
              </div>
            ))
          )}
        </div>
      </div>

      <div className="card">
        <div className="card-header" style={{ display: "flex", justifyContent: "space-between" }}>
          <span>↩️ 操作撤销</span>
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            data-testid="action-undo"
            onClick={() => setPending({ kind: "undo" })}
          >
            ↩️ 撤销上次操作
          </button>
        </div>
        <div
          data-testid="status-undo"
          style={{ fontSize: 12, color: "var(--gray-text)", padding: "4px 16px" }}
        >
          {undoMessage ? (
            <span>{undoMessage}</span>
          ) : undoStatus === "loading" ? (
            <span>加载中...</span>
          ) : undoStatus === "error" ? (
            <span className="text-error">加载失败: {undoError ?? ""}</span>
          ) : latestUndo ? (
            <span>
              最近快照: <code>{latestUndo.name}</code>（{latestUndo.size_kb} KB）
            </span>
          ) : (
            <span>暂无撤销快照。破坏性操作（排序/恢复）前会自动备份。</span>
          )}
        </div>
      </div>

      <NameDialog
        open={saveOpen}
        saving={saving}
        onCancel={() => setSaveOpen(false)}
        onConfirm={(name) => void handleSave(name)}
      />

      <ConfirmDialog
        open={pending !== null}
        title={confirmTitle}
        message={confirmMessage}
        confirmLabel={pending?.kind === "delete" ? "删除" : pending?.kind === "undo" ? "撤销" : "切换"}
        danger={pending?.kind === "delete"}
        name="profile-confirm"
        onConfirm={() => void confirmPending()}
        onCancel={() => setPending(null)}
      />
    </>
  );
}

export default ProfilesPanel;
