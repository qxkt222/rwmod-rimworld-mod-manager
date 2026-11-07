/**
 * Application bootstrap — runs once from `main.tsx` (outside React so
 * StrictMode's double-effect can never open two sockets).
 *
 * Loads the config/mods/queue/updates snapshots in parallel, then starts the
 * WS bridge and the 30s Steam-status poll. Failures are contained: each store
 * owns its error state, and a dead backend must still render the shell.
 */
import { startWsBridge, stopWsBridge } from "../api/ws";
import { useConfigStore } from "../store/config";
import { useModsStore } from "../store/mods";
import { useQueueStore } from "../store/queue";
import { useUpdatesStore } from "../store/updates";

const ONLINE_POLL_MS = 30_000;

let started = false;
let onlineTimer: ReturnType<typeof setInterval> | null = null;

export async function bootstrap(): Promise<void> {
  if (started) return;
  started = true;

  await Promise.allSettled([
    useConfigStore.getState().load(),
    useModsStore.getState().load(),
    useQueueStore.getState().refresh(),
    useUpdatesStore.getState().refreshStatus(),
  ]);

  startWsBridge();
  startOnlinePolling();
}

export function startOnlinePolling(intervalMs = ONLINE_POLL_MS): void {
  if (onlineTimer) return;
  const tick = () => {
    void useConfigStore.getState().refreshStatus();
  };
  tick();
  onlineTimer = setInterval(tick, intervalMs);
}

/** Tear down sockets/timers (tests / hot reload). */
export function stopBootstrap(): void {
  started = false;
  if (onlineTimer) {
    clearInterval(onlineTimer);
    onlineTimer = null;
  }
  stopWsBridge();
}
