/**
 * Config panel — view and edit rwmod settings, including multi-instance
 * management (one entry per RimWorld installation).
 */
import { api } from "../api";
import type { ConfigData, InstanceData } from "../api";
import { esc, escAttr } from "../dom";
import { toast } from "../toast";

export function initConfigPanel() {
  ensureInstanceCard();
  document.getElementById("btn-save-config")?.addEventListener("click", saveConfig);
  document.getElementById("btn-check-steamcmd")?.addEventListener("click", checkSteamCMD);
  document.getElementById("btn-export-bundle")?.addEventListener("click", exportBundle);
  document.getElementById("import-bundle")?.addEventListener("change", importBundle);
  document.getElementById("btn-add-instance")?.addEventListener("click", addInstance);
  document.getElementById("instance-list")?.addEventListener("click", onInstanceAction);
  loadConfig();
  checkSteamCMD();
}

/**
 * Inject the instance manager card at the top of the config panel.
 *
 * The static markup lives in layout.ts, which deliberately has no instance
 * section yet; building it here keeps the panel self-contained. The template
 * is a constant string (no interpolation), so innerHTML is safe.
 */
function ensureInstanceCard() {
  if (document.getElementById("instance-card")) return;
  const panel = document.getElementById("panel-config");
  if (!panel) return;
  const card = document.createElement("div");
  card.className = "card";
  card.id = "instance-card";
  card.innerHTML = `
    <div class="card-header">🗂 游戏实例</div>
    <div id="instance-list" style="font-size:12px;color:var(--gray-text);margin-bottom:10px">加载中...</div>
    <div style="font-size:12px;color:var(--gray-text);margin-bottom:6px">新增实例（路径需为绝对路径）</div>
    <div class="form-group">
      <label>名称</label>
      <input type="text" id="new-instance-name" placeholder="例如：测试环境" />
    </div>
    <div class="form-group">
      <label>Mods 目录</label>
      <input type="text" id="new-instance-mods" placeholder="D:/RimWorld2/Mods" />
    </div>
    <div class="form-group">
      <label>游戏目录</label>
      <input type="text" id="new-instance-rimworld" placeholder="D:/RimWorld2" />
    </div>
    <div style="display:flex;gap:8px">
      <button class="btn btn-ghost" id="btn-add-instance">➕ 新建实例</button>
    </div>
  `;
  const firstCard = panel.querySelector(".card");
  if (firstCard) panel.insertBefore(card, firstCard);
  else panel.appendChild(card);
}

function instanceRow(instance: InstanceData, activeId: string): string {
  const isActive = instance.id === activeId;
  const actions = isActive
    ? '<span style="color:var(--green);font-size:12px">当前实例</span>'
    : `<button class="btn btn-ghost" data-action="activate" data-instance="${escAttr(instance.id)}" data-name="${escAttr(instance.name)}">切换</button>
       <button class="btn btn-ghost" data-action="delete" data-instance="${escAttr(instance.id)}" data-name="${escAttr(instance.name)}">删除</button>`;
  return `
    <div style="display:flex;gap:8px;align-items:center;padding:6px 0;border-bottom:1px solid rgba(128,128,128,.25)">
      <div style="flex:1;min-width:0">
        <div>${isActive ? "✅ " : ""}<strong>${esc(instance.name)}</strong> <code>${esc(instance.id)}</code></div>
        <div style="color:var(--gray-text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">Mods: ${esc(instance.mods_dir)}</div>
      </div>
      <div style="display:flex;gap:6px;flex-shrink:0">${actions}</div>
    </div>`;
}

function renderInstances(cfg: ConfigData) {
  const list = document.getElementById("instance-list");
  if (!list) return;
  const instances = cfg.instances ?? [];
  if (!instances.length) {
    list.textContent = "没有配置实例";
    return;
  }
  list.innerHTML = instances.map((i) => instanceRow(i, cfg.active_instance)).join("");
}

async function onInstanceAction(evt: Event) {
  const target = evt.target as HTMLElement | null;
  const btn = target?.closest<HTMLButtonElement>("button[data-action]");
  if (!btn) return;
  const instanceId = btn.dataset.instance || "";
  const name = btn.dataset.name || instanceId;
  const action = btn.dataset.action;
  if (!instanceId || !action) return;

  if (action === "delete" && !confirm(`确定删除实例 “${name}” 吗？\n该实例的配置将被移除，mod 文件不会被删除。`)) {
    return;
  }

  btn.disabled = true;
  try {
    if (action === "activate") {
      await api.activateInstance(instanceId);
      toast(`已切换到实例 ${name}`, "success");
    } else if (action === "delete") {
      await api.deleteInstance(instanceId);
      toast(`已删除实例 ${name}`, "success");
    }
    await loadConfig();
    checkSteamCMD();
  } catch (e: any) {
    toast(`${action === "activate" ? "切换" : "删除"}失败: ${e.message}`, "error");
  } finally {
    btn.disabled = false;
  }
}

async function addInstance() {
  const name = (document.getElementById("new-instance-name") as HTMLInputElement)?.value.trim() ?? "";
  const modsDir = (document.getElementById("new-instance-mods") as HTMLInputElement)?.value.trim() ?? "";
  const rimworldDir = (document.getElementById("new-instance-rimworld") as HTMLInputElement)?.value.trim() ?? "";
  if (!modsDir) {
    toast("请填写 Mods 目录（绝对路径）", "error");
    return;
  }
  try {
    const result = await api.createInstance({
      name: name || undefined,
      mods_dir: modsDir,
      rimworld_dir: rimworldDir || undefined,
    });
    toast(`已创建实例 ${result.instance.name}`, "success");
    (document.getElementById("new-instance-name") as HTMLInputElement).value = "";
    (document.getElementById("new-instance-mods") as HTMLInputElement).value = "";
    (document.getElementById("new-instance-rimworld") as HTMLInputElement).value = "";
    await loadConfig();
  } catch (e: any) {
    toast(`创建失败: ${e.message}`, "error");
  }
}

async function exportBundle() {
  const status = document.getElementById("transfer-status");
  const includeBackups = (document.getElementById("export-include-backups") as HTMLInputElement)?.checked ?? true;
  // Off by default: the bundle is meant to be shared, and the Steam API key
  // is a credential. Opt in explicitly for own-machine migration.
  const includeSecrets = (document.getElementById("export-include-secrets") as HTMLInputElement)?.checked ?? false;
  if (status) status.innerHTML = '<span style="color:var(--gray-text)">⏳ 正在导出...</span>';
  try {
    await api.exportBundle(includeBackups, includeSecrets);
    if (status) status.innerHTML = '<span style="color:var(--green)">✅ 导出完成</span>';
    toast("导出完成", "success");
  } catch (e: any) {
    if (status) status.innerHTML = `<span style="color:var(--red)">❌ 导出失败: ${esc(e.message)}</span>`;
    toast(`导出失败: ${e.message}`, "error");
  }
}

async function importBundle(evt: Event) {
  const input = evt.target as HTMLInputElement;
  const file = input.files?.[0];
  if (!file) return;
  const status = document.getElementById("transfer-status");
  if (status) status.innerHTML = '<span style="color:var(--gray-text)">⏳ 正在导入...</span>';
  try {
    const result = await api.importBundle(file);
    if (status) status.innerHTML = `<span style="color:var(--green)">✅ ${esc(result.msg)}</span>`;
    toast(result.msg, "success");
  } catch (e: any) {
    if (status) status.innerHTML = `<span style="color:var(--red)">❌ 导入失败: ${esc(e.message)}</span>`;
    toast(`导入失败: ${e.message}`, "error");
  } finally {
    input.value = "";
  }
}

async function loadConfig() {
  try {
    const cfg = await api.getConfig();
    (document.getElementById("cfg-steamcmd") as HTMLInputElement).value = cfg.steamcmd_path;
    (document.getElementById("cfg-mods") as HTMLInputElement).value = cfg.mods_dir;
    (document.getElementById("cfg-rimworld") as HTMLInputElement).value = cfg.rimworld_dir;
    (document.getElementById("cfg-backup") as HTMLInputElement).value = cfg.backup_dir || "";
    renderInstances(cfg);

    const icon = (ok: boolean) => ok ? "✅" : "❌ 未找到";
    document.getElementById("config-display")!.innerHTML = `
      <div>SteamCMD: ${esc(cfg.steamcmd_path)} ${icon(cfg.steamcmd_exists)}</div>
      <div>Mods 目录: ${esc(cfg.mods_dir)} ${icon(cfg.mods_dir_exists)}</div>
      <div>游戏目录: ${esc(cfg.rimworld_dir)}</div>
      <div>备份目录: ${esc(cfg.backup_dir || "未设置")}</div>
    `;
  } catch (e: any) {
    document.getElementById("config-display")!.textContent = `加载失败: ${e.message}`;
  }
}

async function saveConfig() {
  try {
    await api.saveConfig({
      steamcmd_path: (document.getElementById("cfg-steamcmd") as HTMLInputElement).value,
      mods_dir: (document.getElementById("cfg-mods") as HTMLInputElement).value,
      rimworld_dir: (document.getElementById("cfg-rimworld") as HTMLInputElement).value,
      backup_dir: (document.getElementById("cfg-backup") as HTMLInputElement).value,
    });
    toast("配置已保存", "success");
    loadConfig();
    checkSteamCMD();
  } catch (e: any) {
    toast(`保存失败: ${e.message}`, "error");
  }
}

async function checkSteamCMD() {
  const el = document.getElementById("steamcmd-status");
  if (!el) return;
  el.innerHTML = '<span style="color:var(--gray-text)">⏳ 检测中...</span>';
  try {
    const data = await api.checkSteamcmd();
    if (data.ok) {
      el.innerHTML = '<span style="color:var(--green)">✅ ' + esc(data.msg || "检测通过") + '</span>';
    } else {
      el.innerHTML = '<span style="color:var(--red)">❌ ' + esc(data.msg || "检测失败") + '</span>';
    }
  } catch (e: any) {
    el.innerHTML = '<span style="color:var(--red)">❌ 检测失败: ' + esc(e.message) + '</span>';
  }
}
