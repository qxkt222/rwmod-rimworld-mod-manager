/**
 * Hash router — alias normalisation, unknown/empty fallback and event dedupe
 * (docs/ui-redesign.md §3).
 */
import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
  getCurrentPanel,
  navigate,
  resolveHash,
  startRouter,
  stopRouter,
  subscribe,
  syncFromLocation,
  useRoute,
} from "../../src/router/hashRouter";

beforeEach(() => {
  window.history.replaceState({}, "", "/");
  startRouter();
  syncFromLocation();
});

afterEach(() => {
  stopRouter();
});

describe("resolveHash", () => {
  it("maps canonical names and the legacy alias", () => {
    expect(resolveHash("#queue")).toEqual({ panel: "queue", canonical: true });
    expect(resolveHash("#updates")).toEqual({ panel: "queue", canonical: false });
    expect(resolveHash("#tags")).toEqual({ panel: "tags", canonical: true });
  });

  it("falls back to the dashboard for empty and unknown hashes", () => {
    expect(resolveHash("").panel).toBe("dashboard");
    expect(resolveHash("#does-not-exist").panel).toBe("dashboard");
    expect(resolveHash("#does-not-exist").canonical).toBe(false);
  });
});

describe("location synchronisation", () => {
  it("rewrites #updates to #queue with replaceState", () => {
    window.history.replaceState({}, "", "#updates");
    expect(syncFromLocation()).toBe("queue");
    expect(window.location.hash).toBe("#queue");
  });

  it("rewrites an unknown hash to #dashboard", () => {
    window.history.replaceState({}, "", "#nope");
    expect(syncFromLocation()).toBe("dashboard");
    expect(window.location.hash).toBe("#dashboard");
  });

  it("navigate() updates the URL and useRoute()", () => {
    const { result } = renderHook(() => useRoute());

    act(() => navigate("mods"));
    expect(window.location.hash).toBe("#mods");
    expect(result.current.panel).toBe("mods");

    act(() => navigate("config"));
    expect(result.current.panel).toBe("config");
    expect(result.current.route.label).toBe("配置");
  });

  it("deduplicates hashchange + popstate for one resolved panel", () => {
    const seen: string[] = [];
    const unsubscribe = subscribe(() => seen.push(getCurrentPanel()));

    window.history.replaceState({}, "", "#tags");
    window.dispatchEvent(new Event("hashchange"));
    window.dispatchEvent(new PopStateEvent("popstate"));

    expect(seen).toEqual(["tags"]);
    unsubscribe();
  });
});
