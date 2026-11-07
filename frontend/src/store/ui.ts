/**
 * UI slice — theme, sidebar, command palette, dialog host and toasts
 * (docs/ui-redesign.md §2.2 / §5). Layout components are the only writers of
 * the open/closed flags; any module may push a toast through `notify`.
 */
import type { ReactNode } from "react";
import { create } from "zustand";

export type Theme = "light" | "dark";
export type ToastType = "success" | "error" | "warn" | "info";

export interface Toast {
  id: number;
  type: ToastType;
  message: string;
  duration: number;
}

/** A modal managed by DialogHost. `name` becomes the `dialog-<name>` testid. */
export interface DialogSpec {
  name: string;
  title: string;
  body?: ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  danger?: boolean;
  onConfirm?: () => void;
  onCancel?: () => void;
}

interface UIState {
  theme: Theme;
  sidebarCollapsed: boolean;
  commandPaletteOpen: boolean;
  activeDialog: DialogSpec | null;
  toasts: Toast[];

  setTheme: (theme: Theme) => void;
  toggleTheme: () => void;
  setSidebarCollapsed: (collapsed: boolean) => void;
  toggleSidebar: () => void;
  setCommandPalette: (open: boolean) => void;
  toggleCommandPalette: () => void;
  openDialog: (dialog: DialogSpec) => void;
  closeDialog: () => void;
  pushToast: (type: ToastType, message: string, duration?: number) => void;
  dismissToast: (id: number) => void;
  clearToasts: () => void;
}

const THEME_KEY = "rwmod-theme";
const MAX_TOASTS = 5;
const DEDUPE_WINDOW_MS = 1_000;

function readTheme(): Theme {
  try {
    const stored = typeof localStorage !== "undefined" ? localStorage.getItem(THEME_KEY) : null;
    if (stored === "dark" || stored === "light") return stored;
  } catch {
    /* private mode / storage disabled */
  }
  return "light";
}

let toastSeq = 0;
let lastMessage = "";
let lastAt = 0;

export const useUIStore = create<UIState>()((set, get) => ({
  theme: readTheme(),
  sidebarCollapsed: false,
  commandPaletteOpen: false,
  activeDialog: null,
  toasts: [],

  setTheme: (theme) => {
    try {
      localStorage.setItem(THEME_KEY, theme);
    } catch {
      /* ignore quota / disabled storage */
    }
    set({ theme });
  },
  toggleTheme: () => get().setTheme(get().theme === "dark" ? "light" : "dark"),

  setSidebarCollapsed: (sidebarCollapsed) => set({ sidebarCollapsed }),
  toggleSidebar: () => set((state) => ({ sidebarCollapsed: !state.sidebarCollapsed })),

  setCommandPalette: (commandPaletteOpen) => set({ commandPaletteOpen }),
  toggleCommandPalette: () => set((state) => ({ commandPaletteOpen: !state.commandPaletteOpen })),

  openDialog: (activeDialog) => set({ activeDialog }),
  closeDialog: () => set({ activeDialog: null }),

  pushToast: (type, message, duration = 3_500) => {
    const now = Date.now();
    // Same text within 1s is a duplicate (double click / retry storm).
    if (message === lastMessage && now - lastAt < DEDUPE_WINDOW_MS) return;
    lastMessage = message;
    lastAt = now;

    const id = ++toastSeq;
    set((state) => ({
      toasts: [...state.toasts, { id, type, message, duration }].slice(-MAX_TOASTS),
    }));
    if (duration > 0) {
      setTimeout(() => get().dismissToast(id), duration);
    }
  },
  dismissToast: (id) =>
    set((state) => ({ toasts: state.toasts.filter((toast) => toast.id !== id) })),
  clearToasts: () => set({ toasts: [] }),
}));

/** Fire-and-forget toast helpers used by stores and components. */
export const notify = {
  success: (message: string, options?: { duration?: number }) =>
    useUIStore.getState().pushToast("success", message, options?.duration),
  error: (message: string, options?: { duration?: number }) =>
    useUIStore.getState().pushToast("error", message, options?.duration),
  warn: (message: string, options?: { duration?: number }) =>
    useUIStore.getState().pushToast("warn", message, options?.duration),
  info: (message: string, options?: { duration?: number }) =>
    useUIStore.getState().pushToast("info", message, options?.duration),
};

/** Test helper — restore the initial UI state. */
export function resetUI(): void {
  lastMessage = "";
  lastAt = 0;
  useUIStore.setState({
    theme: "light",
    sidebarCollapsed: false,
    commandPaletteOpen: false,
    activeDialog: null,
    toasts: [],
  });
}
