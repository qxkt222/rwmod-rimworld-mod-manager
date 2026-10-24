/**
 * Mod settings files panel — browse and edit RimWorld's Config/Mod_*.xml.
 *
 * The backend owns every safety rule (filename whitelist, 2 MB cap,
 * well-formedness, BOM/CRLF preservation, backup before write, sandbox-guarded
 * paths); this panel only renders and forwards user intent. Writes require an
 * explicit "game is closed" acknowledgement, mirroring the API's
 * `ack_game_closed` contract.
 *
 * Mounted as its own top-level panel (`#settings`), registered in
 * panel-registry.ts and rendered by layout.ts.
 */
import {
  api,
  type ModSettingBackup,
  type ModSettingEntry,
} from "../api";
import { esc, escAttr, fmtBytes, formatTs } from "../dom";
import { toast } from "../toast";

let currentFile = "";

export function initSettingsPanel() {
  bindToolbar();
  bindListActions();
  bindEditorActions();
  void loadList();
  void loadBackups();
}

// ── toolbar ───────────────────────────────────────────────────────

function bindToolbar() {
  document.getElementById("btn-mod-settings-refresh")?.addEventListener("click", () => {
    void loadList();
    void loadBackups();
  });

  document.getElementById("btn-mod-settings-backup-all")?.addEventListener("click", async () => {
    try {
      const res = await api.backupAllModSettings();
      toast(`已备份 ${res.count} 个设置文件`, "success");
      await loadBackups();
    } catch (e: any) {
      toast(`备份失败: ${e.message}`, "error");
    }
  });
}

// ── list ──────────────────────────────────────────────────────────

async function loadList() {
  const listEl = document.getElementById("mod-settings-list");
  if (!listEl) return;
  listEl.innerHTML =
    '<div style="padding:8px 16px;color:var(--gray-text)">加载中...</div>';
  try {
    const data = await api.listModSettings();
    renderList(data.files || [], data.config_dir);
  } catch (e: any) {
    listEl.innerHTML = `<div style="padding:8px 16px;color:#f7768e">读取失败: ${esc(e.message)}</div>`;
  }
}

function renderList(files: ModSettingEntry[], configDir: string | null) {
  const listEl = document.getElementById("mod-settings-list");
  if (!listEl) return;

  const status = document.getElementById("mod-settings-status");
  if (status) {
    status.textContent = configDir
      ? `配置目录: ${configDir}`
      : "未找到 RimWorld 配置目录（可在配置中设置 config_dir）";
  }

  if (!files.length) {
    listEl.innerHTML =
      '<div style="padding:8px 16px;color:var(--gray-text)">没有 Mod_*.xml 设置文件</div>';
    return;
  }

  listEl.innerHTML = files
    .map((f) => {
      const meta = [
        `<span>${esc(fmtBytes(f.size))}</span>`,
        `<span>${esc(formatTs(f.mtime))}</span>`,
        f.associated ? "" : "<span>未关联</span>",
        f.workshop_id ? `<span>Workshop ${esc(f.workshop_id)}</span>` : "",
        f.package_id ? `<span>${esc(f.package_id)}</span>` : "",
      ]
        .filter(Boolean)
        .join("");
      return /* html */ `
        <div class="mod-row">
          <div class="mod-info">
            <div class="mod-name">${esc(f.filename)}</div>
            <div class="mod-meta">${meta}</div>
          </div>
          <div style="display:flex;gap:6px;flex-shrink:0">
            <button class="btn btn-ghost btn-sm mod-setting-edit" data-file="${escAttr(f.filename)}">✏️ 编辑</button>
            <button class="btn btn-ghost btn-sm mod-setting-restore" data-file="${escAttr(f.filename)}">↩️ 恢复</button>
          </div>
        </div>`;
    })
    .join("");
}

function bindListActions() {
  document.getElementById("mod-settings-list")?.addEventListener("click", (e) => {
    const btn = (e.target as HTMLElement).closest(
      "button.mod-setting-edit, button.mod-setting-restore",
    ) as HTMLButtonElement | null;
    if (!btn) return;
    const file = btn.dataset.file || "";
    if (!file) return;

    if (btn.classList.contains("mod-setting-edit")) {
      void openEditor(file);
    } else {
      void restoreFile(file);
    }
  });
}

// ── editor ────────────────────────────────────────────────────────

async function openEditor(file: string) {
  try {
    const detail = await api.getModSetting(file);
    currentFile = file;
    const editor = document.getElementById("mod-settings-editor");
    if (editor) editor.style.display = "";
    const title = document.getElementById("mod-settings-editor-title");
    if (title) title.textContent = `${file}（${detail.root_tag || "XML"}）`;
    (document.getElementById("mod-settings-xml") as HTMLTextAreaElement).value = detail.xml;
    (document.getElementById("mod-settings-ack") as HTMLInputElement).checked = false;
    if (detail.game_running) {
      toast("检测到 RimWorld 正在运行：保存前请先退出游戏", "warn");
    }
    // The editor lives above the (long) file list and may sit below the
    // viewport after the panel has been scrolled; bring it into view.
    editor?.scrollIntoView({ block: "nearest" });
  } catch (e: any) {
    toast(`读取失败: ${e.message}`, "error");
  }
}

function bindEditorActions() {
  document.getElementById("btn-mod-settings-save")?.addEventListener("click", () => {
    void saveEditor();
  });
  document.getElementById("btn-mod-settings-restore-latest")?.addEventListener("click", () => {
    if (currentFile) void restoreFile(currentFile);
  });
  document.getElementById("btn-mod-settings-close")?.addEventListener("click", () => {
    const editor = document.getElementById("mod-settings-editor");
    if (editor) editor.style.display = "none";
    currentFile = "";
  });
}

function acknowledged(): boolean {
  const ack = (document.getElementById("mod-settings-ack") as HTMLInputElement | null)?.checked;
  if (!ack) {
    toast("请先勾选「我已关闭 RimWorld」", "error");
  }
  return Boolean(ack);
}

async function saveEditor() {
  if (!currentFile) return;
  if (!acknowledged()) return;

  const xml = (document.getElementById("mod-settings-xml") as HTMLTextAreaElement).value;
  try {
    const res = await api.saveModSetting(currentFile, xml, true);
    if (res.game_running) {
      toast(`已保存，但检测到游戏正在运行，重启后生效（备份 ${res.backup}）`, "warn");
    } else {
      toast(`已保存（备份 ${res.backup}）`, "success");
    }
    await loadList();
    await loadBackups();
  } catch (e: any) {
    toast(`保存失败: ${e.message}`, "error");
  }
}

// ── restore ───────────────────────────────────────────────────────

async function restoreFile(file: string, backup = "") {
  const ok = window.confirm(
    `将用备份覆盖 ${file}。请先完全退出 RimWorld，否则游戏退出时会覆盖回去。继续？`,
  );
  if (!ok) return;

  try {
    const res = await api.restoreModSetting(file, backup, true);
    toast(`已从 ${res.restored_from} 恢复 ${file}`, "success");
    await loadList();
    await loadBackups();
    if (currentFile === file) await openEditor(file);
  } catch (e: any) {
    toast(`恢复失败: ${e.message}`, "error");
  }
}

// ── backups ───────────────────────────────────────────────────────

async function loadBackups() {
  const el = document.getElementById("mod-settings-backups");
  if (!el) return;
  el.innerHTML = "加载中...";
  try {
    const data = await api.listModSettingBackups();
    renderBackups(data.backups || []);
  } catch (e: any) {
    el.innerHTML = `<span style="color:#f7768e">读取失败: ${esc(e.message)}</span>`;
  }
}

function renderBackups(backups: ModSettingBackup[]) {
  const el = document.getElementById("mod-settings-backups");
  if (!el) return;

  if (!backups.length) {
    el.innerHTML = '<span style="color:var(--gray-text)">暂无备份</span>';
    return;
  }

  el.innerHTML = backups
    .slice(0, 30)
    .map(
      (b) => /* html */ `
        <div style="display:flex;gap:8px;align-items:center;padding:2px 0">
          <span style="flex:1;word-break:break-all">${esc(b.source)}</span>
          <span style="color:var(--gray-text)">${esc(formatTs(b.mtime))}</span>
          <span style="color:var(--gray-text)">${esc(fmtBytes(b.size))}</span>
          <button class="btn btn-ghost btn-sm mod-backup-restore"
                  data-file="${escAttr(b.source)}"
                  data-backup="${escAttr(b.filename)}">↩️ 恢复</button>
        </div>`,
    )
    .join("");

  el.querySelectorAll<HTMLButtonElement>(".mod-backup-restore").forEach((btn) => {
    btn.addEventListener("click", () => {
      const file = btn.dataset.file || "";
      const backup = btn.dataset.backup || "";
      if (file && backup) void restoreFile(file, backup);
    });
  });
}
