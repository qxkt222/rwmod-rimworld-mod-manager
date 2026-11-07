/**
 * Cross-panel refresh bus (docs/ui-redesign.md §4.1).
 *
 * Mutation actions call `bump(topic)`; panels subscribe with
 * `useRefreshOn(topic, fetch)`. A mounted panel refreshes immediately; an
 * unmounted panel notices the revision change the next time it mounts, so it
 * does not need its own stale-data bookkeeping.
 */
import { useEffect, useRef } from "react";
import { create } from "zustand";

export const TOPICS = [
  "mods",
  "queue",
  "history",
  "backups",
  "profiles",
  "tags",
  "config",
  "settings",
  "updates",
  "saves",
  "rimsort",
  "dashboard",
] as const;

export type Topic = (typeof TOPICS)[number];

type Revision = Record<Topic, number>;

interface BusState {
  revision: Revision;
  bump: (topic: Topic) => void;
}

function zeroed(): Revision {
  return Object.fromEntries(TOPICS.map((topic) => [topic, 0])) as Revision;
}

export const useBus = create<BusState>()((set) => ({
  revision: zeroed(),
  bump: (topic) =>
    set((state) => ({
      revision: { ...state.revision, [topic]: state.revision[topic] + 1 },
    })),
}));

/** Bump one topic. Safe to call from non-React code (stores, WS bridge). */
export function bump(topic: Topic): void {
  useBus.getState().bump(topic);
}

/** Current revision of a topic (for tests and imperative code). */
export function getRevision(topic: Topic): number {
  return useBus.getState().revision[topic];
}

/**
 * Run `refresh` whenever `topic` is bumped.
 *
 * The revision is captured during render and compared inside the effect, so a
 * bump that lands between render and effect still triggers exactly one call.
 */
export function useRefreshOn(topic: Topic, refresh: () => void): void {
  const refreshRef = useRef(refresh);
  refreshRef.current = refresh;
  const seenRef = useRef(getRevision(topic));

  useEffect(() => {
    const check = () => {
      const next = getRevision(topic);
      if (next === seenRef.current) return;
      seenRef.current = next;
      refreshRef.current();
    };
    check();
    return useBus.subscribe(check);
  }, [topic]);
}

/** Test helper — reset every topic to 0. */
export function resetBus(): void {
  useBus.setState({ revision: zeroed() });
}
