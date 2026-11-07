/**
 * Config panel — paths, SteamCMD probe, multi-instance management and the
 * `.rwmod` transfer bundle.
 *
 * Parity contract with the legacy `src/panels/config.ts` and `config.spec.ts`:
 * `#config-display`, `#cfg-steamcmd`, `#cfg-mods`, `#cfg-rimworld`,
 * `#cfg-backup`, `#btn-save-config`, `#btn-check-steamcmd`, `#steamcmd-status`,
 * `#instance-card`, `#instance-list`, `#btn-add-instance`, `#new-instance-*`,
 * `button[data-action="activate"|"delete"]`, `#btn-export-bundle`,
 * `#import-bundle`, `#export-include-backups`, `#export-include-secrets`,
 * `#transfer-status`.
 *
 * The shared config slice owns loading/saving; instance deletion uses the
 * in-app `ConfirmDialog` instead of the legacy native `confirm()`.
 */
import { useCallback, useEffect, useState, type ChangeEvent } from "react";

import { toMessage } from "../api/client";
import type { components } from "../api/generated";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { ErrorState } from "../components/ErrorState";
import { LoadingState } from "../components/LoadingState";
import { useBackupsStore } from "../store/backups";
import { useRefreshOn } from "../store/bus";
import { useConfigStore } from "../store/config";
import { notify } from "../store/ui";

type InstanceInfo = components["schemas"]["InstanceInfo"];

interface ConfigForm {
  steamcmd_path: string;
  mods_dir: string;
  rimworld_dir: string;
  backup_dir: string;
}

const EMPTY_FORM: ConfigForm = {
  steamcmd_path: "",
  mods_dir: "",
  rimworld_dir: "",
  backup_dir: "",
};

const rowStyle = {
  display: "flex",
  gap: 8,
  alignItems: "center",
  padding: "6px 0",
  borderBottom: "1px solid var(--border)",
} as const;

export function ConfigPanel() {
  const config = useConfigStore((state) => state.config);
  const instances = useConfigStore((state) => state.instances);
  const activeInstanceId = useConfigStore((state) => state.activeInstanceId);
  const status = useConfigStore((state) => state.status);
  const error = useConfigStore((state) => state.error);
  const load = useConfigStore((state) => state.load);
  const save = useConfigStore((state) => state.save);
  const createInstance = useConfigStore((state) => state.createInstance);
  const activateInstance = useConfigStore((state) => state.activateInstance);
  const deleteInstance = useConfigStore((state) => state.deleteInstance);
  const checkSteamcmd = useConfigStore((state) => state.checkSteamcmd);
  const steamcmdCheck = useConfigStore((state) => state.steamcmdCheck);

  const exportBundle = useBackupsStore((state) => state.exportBundle);
  const importBundle = useBackupsStore((state) => state.importBundle);
  const transferring = useBackupsStore((state) => state.transferring);

  const [form, setForm] = useState<ConfigForm>(EMPTY_FORM);
  const [newName, setNewName] = useState("");
  const [newMods, setNewMods] = useState("");
  const [newRimworld, setNewRimworld] = useState("");
  const [includeBackups, setIncludeBackups] = useState(true);
  const [includeSecrets, setIncludeSecrets] = useState(false);
  const [transferStatus, setTransferStatus] = useState("");
  const [deleteTarget, setDeleteTarget] = useState<InstanceInfo | null>(null);

  const refresh = useCallback(() => {
    void load().catch(() => {
      /* the store keeps the error for the status line */
    });
  }, [load]);

  useEffect(() => {
    refresh();
    void checkSteamcmd();
  }, [refresh, checkSteamcmd]);

  useRefreshOn("config", refresh);

  useEffect(() => {
    if (!config) return;
    setForm({
      steamcmd_path: config.steamcmd_path ?? "",
      mods_dir: config.mods_dir ?? "",
      rimworld_dir: config.rimworld_dir ?? "",
      backup_dir: config.backup_dir ?? "",
    });
  }, [config]);

  const patch = (key: keyof ConfigForm) => (event: ChangeEvent<HTMLInputElement>) =>
    setForm((current) => ({ ...current, [key]: event.target.value }));

  const onSave = async () => {
    try {
      await save({
        steamcmd_path: form.steamcmd_path,
        mods_dir: form.mods_dir,
        rimworld_dir: form.rimworld_dir,
        backup_dir: form.backup_dir,
      });
      notify.success("配置已保存");
      void checkSteamcmd();
    } catch (saveError) {
      notify.error(`保存失败: ${toMessage(saveError)}`);
    }
  };

  const onCreateInstance = async () => {
    const modsDir = newMods.trim();
    if (!modsDir) {
      notify.error("请填写 Mods 目录（绝对路径）");
      return;
    }
    const name = newName.trim();
    try {
      await createInstance({
        name: name || undefined,
        mods_dir: modsDir,
        rimworld_dir: newRimworld.trim() || undefined,
      });
      notify.success(`已创建实例 ${name || modsDir}`);
      setNewName("");
      setNewMods("");
      setNewRimworld("");
      void checkSteamcmd();
    } catch (createError) {
      notify.error(`创建失败: ${toMessage(createError)}`);
    }
  };

  const onActivate = async (instance: InstanceInfo) => {
    try {
      await activateInstance(instance.id);
      notify.success(`已切换到实例 ${instance.name}`);
      void checkSteamcmd();
    } catch (activateError) {
      notify.error(`切换失败: ${toMessage(activateError)}`);
    }
  };

  const onConfirmDelete = async () => {
    if (!deleteTarget) return;
    const instance = deleteTarget;
    setDeleteTarget(null);
    try {
      await deleteInstance(instance.id);
      notify.success(`已删除实例 ${instance.name}`);
    } catch (deleteError) {
      notify.error(`删除失败: ${toMessage(deleteError)}`);
    }
  };

  const onExport = async () => {
    setTransferStatus("⏳ 正在导出...");
    try {
      await exportBundle(includeBackups, includeSecrets);
      setTransferStatus("✅ 导出完成");
      notify.success("导出完成");
    } catch (exportError) {
      const message = toMessage(exportError);
      setTransferStatus(`❌ 导出失败: ${message}`);
      notify.error(`导出失败: ${message}`);
    }
  };

  const onImport = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    if (!file) return;
    setTransferStatus("⏳ 正在导入...");
    try {
      const message = await importBundle(file);
      setTransferStatus(`✅ ${message}`);
      notify.success(message);
      refresh();
      void checkSteamcmd();
    } catch (importError) {
      const message = toMessage(importError);
      setTransferStatus(`❌ 导入失败: ${message}`);
      notify.error(`导入失败: ${message}`);
    } finally {
      event.target.value = "";
    }
  };

  const verifyIcon = (ok: boolean) => (ok ? "✅" : "❌ 未找到");

  return (
    <>
      <div className="card" id="instance-card">
        <div className="card-header">🗂 游戏实例</div>
        <div
          id="instance-list"
          data-testid="list-instances"
          style={{ fontSize: 12, color: "var(--gray-text)", marginBottom: 10 }}
        >
          {status === "loading" && instances.length === 0
            ? "加载中..."
            : instances.length === 0
              ? "没有配置实例"
              : instances.map((instance) => {
                  const isActive = instance.id === activeInstanceId;
                  return (
                    <div
                      key={instance.id}
                      data-testid={`item-instance-${instance.id}`}
                      style={rowStyle}
                    >
                      <div style={{ flex: 1, minWidth: 0 }}>
                        <div>
                          {isActive ? "✅ " : ""}
                          <strong>{instance.name}</strong> <code>{instance.id}</code>
                        </div>
                        <div
                          style={{
                            color: "var(--gray-text)",
                            overflow: "hidden",
                            textOverflow: "ellipsis",
                            whiteSpace: "nowrap",
                          }}
                        >
                          Mods: {instance.mods_dir}
                        </div>
                      </div>
                      <div style={{ display: "flex", gap: 6, flexShrink: 0 }}>
                        {isActive ? (
                          <span style={{ color: "var(--green)", fontSize: 12 }}>当前实例</span>
                        ) : (
                          <>
                            <button
                              type="button"
                              className="btn btn-ghost btn-sm"
                              data-action="activate"
                              data-instance={instance.id}
                              data-name={instance.name}
                              data-testid={`action-instance-activate-${instance.id}`}
                              onClick={() => void onActivate(instance)}
                            >
                              切换
                            </button>
                            <button
                              type="button"
                              className="btn btn-ghost btn-sm"
                              data-action="delete"
                              data-instance={instance.id}
                              data-name={instance.name}
                              data-testid={`action-instance-delete-${instance.id}`}
                              onClick={() => setDeleteTarget(instance)}
                            >
                              删除
                            </button>
                          </>
                        )}
                      </div>
                    </div>
                  );
                })}
        </div>

        <div style={{ fontSize: 12, color: "var(--gray-text)", marginBottom: 6 }}>
          新增实例（路径需为绝对路径）
        </div>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "flex-end" }}>
          <div className="form-group">
            <label htmlFor="new-instance-name">名称</label>
            <input
              id="new-instance-name"
              data-testid="field-new-instance-name"
              type="text"
              placeholder="例如：测试环境"
              value={newName}
              onChange={(event) => setNewName(event.target.value)}
            />
          </div>
          <div className="form-group">
            <label htmlFor="new-instance-mods">Mods 目录</label>
            <input
              id="new-instance-mods"
              data-testid="field-new-instance-mods"
              type="text"
              placeholder="D:/RimWorld2/Mods"
              value={newMods}
              onChange={(event) => setNewMods(event.target.value)}
            />
          </div>
          <div className="form-group">
            <label htmlFor="new-instance-rimworld">游戏目录</label>
            <input
              id="new-instance-rimworld"
              data-testid="field-new-instance-rimworld"
              type="text"
              placeholder="D:/RimWorld2"
              value={newRimworld}
              onChange={(event) => setNewRimworld(event.target.value)}
            />
          </div>
          <button
            type="button"
            className="btn btn-ghost"
            id="btn-add-instance"
            data-testid="action-add-instance"
            onClick={() => void onCreateInstance()}
          >
            ➕ 新建实例
          </button>
        </div>
      </div>

      <div className="card">
        <div className="card-header">⚙ 配置</div>
        <div
          id="config-display"
          data-testid="status-config"
          style={{ fontSize: 12, color: "var(--gray-text)", marginBottom: 12 }}
        >
          {config ? (
            <>
              <div>
                SteamCMD: {config.steamcmd_path} {verifyIcon(Boolean(config.steamcmd_exists))}
              </div>
              <div>
                Mods 目录: {config.mods_dir} {verifyIcon(Boolean(config.mods_dir_exists))}
              </div>
              <div>游戏目录: {config.rimworld_dir}</div>
              <div>备份目录: {config.backup_dir || "未设置"}</div>
            </>
          ) : status === "error" ? (
            <ErrorState
              message={`加载失败: ${error ?? "未知错误"}`}
              onRetry={refresh}
              testid="status-config-error"
            />
          ) : (
            <LoadingState message="加载中..." testid="status-config-loading" />
          )}
        </div>

        <div className="form-group">
          <label htmlFor="cfg-steamcmd">SteamCMD 路径</label>
          <input
            id="cfg-steamcmd"
            data-testid="field-steamcmd-path"
            type="text"
            value={form.steamcmd_path}
            onChange={patch("steamcmd_path")}
          />
        </div>
        <div className="form-group">
          <label htmlFor="cfg-mods">Mods 目录</label>
          <input
            id="cfg-mods"
            data-testid="field-mods-dir"
            type="text"
            value={form.mods_dir}
            onChange={patch("mods_dir")}
          />
        </div>
        <div className="form-group">
          <label htmlFor="cfg-rimworld">游戏目录</label>
          <input
            id="cfg-rimworld"
            data-testid="field-rimworld-dir"
            type="text"
            value={form.rimworld_dir}
            onChange={patch("rimworld_dir")}
          />
        </div>
        <div className="form-group">
          <label htmlFor="cfg-backup">备份目录</label>
          <input
            id="cfg-backup"
            data-testid="field-backup-dir"
            type="text"
            value={form.backup_dir}
            onChange={patch("backup_dir")}
          />
        </div>

        <div style={{ display: "flex", gap: 8, marginTop: 12 }}>
          <button
            type="button"
            className="btn btn-primary"
            id="btn-save-config"
            data-testid="action-save-config"
            onClick={() => void onSave()}
          >
            保存
          </button>
          <button
            type="button"
            className="btn btn-ghost"
            id="btn-check-steamcmd"
            data-testid="action-check-steamcmd"
            onClick={() => void checkSteamcmd()}
          >
            检测 SteamCMD
          </button>
        </div>
        <div
          id="steamcmd-status"
          data-testid="status-steamcmd"
          style={{ marginTop: 8, fontSize: 12 }}
        >
          {steamcmdCheck === null ? (
            <span style={{ color: "var(--gray-text)" }}>⏳ 检测中...</span>
          ) : steamcmdCheck.ok ? (
            <span style={{ color: "var(--green)" }}>✅ {steamcmdCheck.msg || "检测通过"}</span>
          ) : (
            <span style={{ color: "var(--red)" }}>❌ {steamcmdCheck.msg || "检测失败"}</span>
          )}
        </div>
      </div>

      <div className="card">
        <div className="card-header">📦 一键迁移</div>
        <div style={{ fontSize: 12, color: "var(--gray-text)", marginBottom: 8 }}>
          将配置档案、备份、标签与设置打包为 <code>.rwmod</code> 文件，可跨机器迁移。
        </div>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          <button
            type="button"
            className="btn btn-primary"
            id="btn-export-bundle"
            data-testid="action-export-bundle"
            disabled={transferring}
            onClick={() => void onExport()}
          >
            📤 导出 .rwmod
          </button>
          <label
            className="btn btn-ghost"
            style={{ cursor: "pointer", margin: 0 }}
            data-testid="action-import-bundle"
          >
            📥 导入 .rwmod
            <input
              type="file"
              id="import-bundle"
              data-testid="field-import-bundle"
              accept=".rwmod"
              style={{ display: "none" }}
              onChange={(event) => void onImport(event)}
            />
          </label>
        </div>
        <label className="check-label" style={{ marginTop: 8, display: "block" }}>
          <input
            type="checkbox"
            id="export-include-backups"
            data-testid="field-export-include-backups"
            checked={includeBackups}
            onChange={(event) => setIncludeBackups(event.target.checked)}
          />{" "}
          导出时包含备份文件
        </label>
        <label className="check-label" style={{ marginTop: 4, display: "block" }}>
          <input
            type="checkbox"
            id="export-include-secrets"
            data-testid="field-export-include-secrets"
            checked={includeSecrets}
            onChange={(event) => setIncludeSecrets(event.target.checked)}
          />{" "}
          包含 Steam API 密钥（仅限自己迁移用，打包分享时勿勾选）
        </label>
        <div
          id="transfer-status"
          data-testid="status-transfer"
          style={{ marginTop: 8, fontSize: 12 }}
        >
          {transferStatus}
        </div>
      </div>

      <ConfirmDialog
        open={deleteTarget !== null}
        name="instance-delete"
        title="删除实例"
        message={
          deleteTarget
            ? `确定删除实例 “${deleteTarget.name}” 吗？该实例的配置将被移除，mod 文件不会被删除。`
            : ""
        }
        confirmLabel="删除"
        danger
        onConfirm={() => void onConfirmDelete()}
        onCancel={() => setDeleteTarget(null)}
      />
    </>
  );
}

export default ConfigPanel;
