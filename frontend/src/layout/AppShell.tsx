/**
 * Application shell — Topbar + Sidebar + PanelHost + global overlays.
 * Wired in one place so the overlays and shortcuts mount exactly once.
 */
import { useEffect } from "react";

import { CommandPalette } from "../components/CommandPalette";
import { DialogHost } from "../components/DialogHost";
import { ToastViewport } from "../components/ToastViewport";
import { useUIStore } from "../store/ui";
import { useGlobalShortcuts } from "../app/shortcuts";
import { PanelHost } from "./PanelHost";
import { Sidebar } from "./Sidebar";
import { Topbar } from "./Topbar";

export interface AppShellProps {
  version: string;
  status: string;
}

export function AppShell({ version, status }: AppShellProps) {
  useGlobalShortcuts();
  const theme = useUIStore((state) => state.theme);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
  }, [theme]);

  return (
    <>
      <Topbar version={version} status={status} />
      <div className="main">
        <Sidebar />
        <PanelHost />
      </div>
      <ToastViewport />
      <CommandPalette />
      <DialogHost />
    </>
  );
}

export default AppShell;
