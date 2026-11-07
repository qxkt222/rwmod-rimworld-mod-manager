/**
 * Global keyboard shortcuts (docs/ui-redesign.md §1.4 / §5.2), registered once
 * by AppShell:
 *   Ctrl+K            toggle command palette
 *   Ctrl+1..8         navigation (see router/routes.ts SHORTCUTS)
 *   Escape            close the topmost overlay (palette → dialog)
 *
 * Typing targets are never intercepted; `preventDefault` is required for
 * Ctrl+digit because browsers otherwise bind it to "switch to tab N".
 */
import { useEffect } from "react";

import { navigate } from "../router/hashRouter";
import { SHORTCUTS } from "../router/routes";
import { useUIStore } from "../store/ui";

/** Inputs / textareas / selects / contentEditable suppress panel shortcuts. */
export function isTypingTarget(target: EventTarget | null): boolean {
  const element = target as HTMLElement | null;
  if (!element || typeof element.tagName !== "string") return false;
  if (element.tagName === "INPUT" || element.tagName === "TEXTAREA" || element.tagName === "SELECT") {
    return true;
  }
  return element.isContentEditable === true;
}

export function useGlobalShortcuts(): void {
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const ui = useUIStore.getState();
      const modifier = event.ctrlKey || event.metaKey;

      if (modifier && !event.altKey && !event.shiftKey && event.key.toLowerCase() === "k") {
        event.preventDefault();
        ui.toggleCommandPalette();
        return;
      }

      if (event.key === "Escape") {
        if (ui.commandPaletteOpen) {
          event.preventDefault();
          ui.setCommandPalette(false);
          return;
        }
        if (ui.activeDialog) {
          event.preventDefault();
          ui.closeDialog();
        }
        return;
      }

      if (!modifier || event.altKey || event.shiftKey) return;
      const panel = SHORTCUTS[`Ctrl+${event.key}`];
      if (!panel || isTypingTarget(event.target)) return;
      event.preventDefault();
      navigate(panel);
    };

    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, []);
}
