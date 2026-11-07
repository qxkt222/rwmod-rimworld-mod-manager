/// <reference types="vite/client" />
/**
 * Panel host — renders exactly one panel for `useRoute().panel`.
 *
 * Panels self-register by filename: `src/panels/<Pascal>Panel.tsx` maps to the
 * canonical `<kebab>` panel name (`DashboardPanel` → `dashboard`,
 * `RimsortPanel` → `rimsort`, `SettingsPanel` → `settings`). FE-2/FE-3 only add
 * files; they never edit this mapping. Names that are not canonical (e.g. a
 * typo, or a shared helper named `*Panel.tsx`) are ignored and the panel keeps
 * its placeholder.
 *
 * The wrapper keeps the legacy anchor contract (`.panel.active`,
 * `#panel-<name>`) *and* the canonical testid (`panel-<name>`, `data-active`).
 */
import { lazy, Suspense, type ComponentType, type LazyExoticComponent } from "react";

import { EmptyState } from "../components/EmptyState";
import { ErrorBoundary } from "../components/ErrorBoundary";
import { LoadingState } from "../components/LoadingState";
import { useRoute } from "../router/hashRouter";
import { ROUTES, type PanelName, type RouteDef } from "../router/routes";

type PanelModule = { default: ComponentType };

/** Vite lazy glob: one chunk per `*Panel.tsx`, nothing eager. */
const PANEL_MODULES = import.meta.glob<PanelModule>("../panels/*Panel.tsx");

/**
 * `DashboardPanel.tsx` → `dashboard`; `RimsortPanel.tsx` → `rimsort`.
 * Lower-casing absorbs spelling variants such as `RimSortPanel`.
 */
export function panelNameFromPath(path: string): PanelName | null {
  const file = path.split("/").pop() ?? "";
  const match = /^([A-Za-z0-9]+)Panel\.tsx$/.exec(file);
  if (!match) return null;
  const candidate = match[1].toLowerCase();
  return candidate in ROUTES ? (candidate as PanelName) : null;
}

const PANELS: Partial<Record<PanelName, LazyExoticComponent<ComponentType>>> = {};
for (const [path, loader] of Object.entries(PANEL_MODULES)) {
  const name = panelNameFromPath(path);
  if (name) PANELS[name] = lazy(loader);
}

function PlaceholderPanel({ route }: { route: RouteDef }) {
  return (
    <div className="card">
      <EmptyState
        icon={route.icon}
        title={`${route.label}尚未迁移`}
        message="该面板仍在旧前端（index.html）中提供，将在后续波次迁移到 React。"
        testid={`status-placeholder-${route.name}`}
      />
    </div>
  );
}

export function PanelHost() {
  const { panel, route } = useRoute();
  const Panel = PANELS[panel];

  return (
    <main id="workspace" className="panel-host" data-testid="panel-host">
      <section
        className="panel active"
        id={`panel-${panel}`}
        data-testid={`panel-${panel}`}
        data-active="true"
        role="tabpanel"
        aria-label={route.label}
      >
        <ErrorBoundary key={panel} label={route.label}>
          <Suspense
            fallback={
              <LoadingState message={`正在加载 ${route.label}…`} testid="status-panel-loading" />
            }
          >
            {Panel ? <Panel /> : <PlaceholderPanel route={route} />}
          </Suspense>
        </ErrorBoundary>
      </section>
    </main>
  );
}
