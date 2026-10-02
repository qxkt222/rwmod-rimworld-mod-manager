/**
 * Ctrl+K Command Palette — keyboard-driven navigation.
 * Uses a lazy-registry pattern to avoid circular imports from main.ts.
 *
 * Shortcut labels are looked up from PANEL_SHORTCUTS (panel-registry.ts), the
 * same map main.ts's keydown handler dispatches on — so every `Ctrl+N` shown
 * here is a `Ctrl+N` that actually works, and a renamed/removed panel can
 * never leave a stale label behind.
 */
import { panelShortcut, type PanelName } from "./panel-registry";

interface CmdItem {
  label: string;
  ico: string;
  action: () => void;
  shortcut?: string;
}

/** Navigate via the hash router so the URL stays in sync. */
function _makePanelAction(panel: PanelName): () => void {
  return () => {
    import("./router").then(({ navigate }) => navigate(panel));
  };
}

const COMMANDS: CmdItem[] = [
  { label: "下载 Mod", ico: "📥", action: _makePanelAction("download"), shortcut: panelShortcut("download") },
  { label: "下载合集", ico: "📦", action: _makePanelAction("collection"), shortcut: panelShortcut("collection") },
  { label: "导入列表", ico: "📋", action: _makePanelAction("import"), shortcut: panelShortcut("import") },
  { label: "已安装 Mods", ico: "📊", action: _makePanelAction("mods"), shortcut: panelShortcut("mods") },
  { label: "搜索 Workshop", ico: "🔍", action: _makePanelAction("search"), shortcut: panelShortcut("search") },
  { label: "下载队列", ico: "📋", action: _makePanelAction("queue"), shortcut: panelShortcut("queue") },
  { label: "RimSort", ico: "📐", action: _makePanelAction("rimsort"), shortcut: panelShortcut("rimsort") },
  // Ctrl+8 is the last panel in the derived shortcut range (profiles); the
  // remaining panels stay reachable here without a key badge.
  { label: "配置档案", ico: "💾", action: _makePanelAction("profiles"), shortcut: panelShortcut("profiles") },
  { label: "配置", ico: "⚙", action: _makePanelAction("config") },
  { label: "导出 Mod 列表", ico: "📤", action: () => document.getElementById("btn-export")?.click() },
  { label: "切换深色模式", ico: "🌙", action: () => document.getElementById("btn-dark")?.click() },
  { label: "刷新 Mod 列表", ico: "🔄", action: () => document.getElementById("btn-refresh")?.click() },
  { label: "清空日志", ico: "🗑", action: () => document.getElementById("btn-clear-log")?.click() },
];

let selectedIdx = 0;

export function openCmdPalette() {
  const palette = document.getElementById("cmd-palette")!;
  const input = document.getElementById("cmd-input") as HTMLInputElement;
  palette.classList.add("open");
  input.value = "";
  selectedIdx = 0;
  renderResults(COMMANDS);
  input.focus();
}

function renderResults(items: CmdItem[]) {
  const container = document.getElementById("cmd-results")!;
  container.innerHTML = items
    .map(
      (item, i) => /* html */ `
    <div class="cmd-result${i === selectedIdx ? " selected" : ""}" data-idx="${i}">
      <span>${item.ico}</span>
      <span>${item.label}</span>
      ${item.shortcut ? `<span class="cmd-kbd">${item.shortcut}</span>` : ""}
    </div>`,
    )
    .join("");

  container.querySelectorAll(".cmd-result").forEach((el) => {
    el.addEventListener("click", () => {
      const i = Number((el as HTMLElement).dataset.idx);
      if (items[i]) {
        closePalette();
        items[i].action();
      }
    });
  });
}

function closePalette() {
  document.getElementById("cmd-palette")!.classList.remove("open");
}

// Keyboard navigation
document.addEventListener("keydown", (e) => {
  const palette = document.getElementById("cmd-palette")!;
  if (!palette.classList.contains("open")) return;

  if (e.key === "Escape") {
    closePalette();
    return;
  }

  if (e.key === "ArrowDown") {
    e.preventDefault();
    selectedIdx = Math.min(selectedIdx + 1, COMMANDS.length - 1);
    renderResults(COMMANDS);
  }
  if (e.key === "ArrowUp") {
    e.preventDefault();
    selectedIdx = Math.max(selectedIdx - 1, 0);
    renderResults(COMMANDS);
  }
  if (e.key === "Enter") {
    e.preventDefault();
    COMMANDS[selectedIdx]?.action();
    closePalette();
  }
});

// Filter on input
document.addEventListener("input", (e) => {
  const target = e.target as HTMLElement;
  if (target.id !== "cmd-input") return;
  const q = (target as HTMLInputElement).value.toLowerCase();
  selectedIdx = 0;
  const filtered = q
    ? COMMANDS.filter((c) => c.label.toLowerCase().includes(q))
    : COMMANDS;
  renderResults(filtered);
});
