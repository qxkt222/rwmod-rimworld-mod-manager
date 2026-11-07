/**
 * Single navigation source: five groups / fifteen panels
 * (docs/ui-redesign.md §1.2). Entries are real anchors so middle-click,
 * right-click and keyboard activation keep working; the hash router reacts to
 * `hashchange` like any other deep link.
 */
import { useRoute } from "../router/hashRouter";
import { ROUTES, SIDEBAR_GROUPS, type PanelName } from "../router/routes";
import { useModsStore } from "../store/mods";
import { selectPendingCount, useQueueStore } from "../store/queue";
import { useUIStore } from "../store/ui";

export function Sidebar() {
  const { panel: active } = useRoute();
  const collapsed = useUIStore((state) => state.sidebarCollapsed);
  const modsCount = useModsStore((state) => state.items.length);
  const pendingCount = useQueueStore(selectPendingCount);
  const wsState = useQueueStore((state) => state.wsState);

  const wsStale = wsState !== "connected";
  const staleTitle = "实时连接已断开，数字可能不是最新";

  function badgeFor(panel: PanelName): { value: number; stale: boolean } | null {
    if (panel === "mods") return { value: modsCount, stale: false };
    if (panel === "queue") return { value: pendingCount, stale: wsStale };
    return null;
  }

  return (
    <aside
      id="sidebar"
      className={`sidebar${collapsed ? " sidebar--collapsed" : ""}`}
      data-testid="nav-sidebar"
      data-collapsed={collapsed ? "true" : "false"}
    >
      <nav aria-label="主导航">
        {SIDEBAR_GROUPS.map(({ group, panels }) => (
          <div className="sidebar__group" key={group}>
            <div className="side-section" aria-hidden="true">
              {group}
            </div>
            {panels.map((panel) => {
              const route = ROUTES[panel];
              const isActive = panel === active;
              const badge = badgeFor(panel);
              return (
                <a
                  key={panel}
                  href={`#${panel}`}
                  className={`side-item${isActive ? " active" : ""}`}
                  data-panel={panel}
                  data-testid={`nav-${panel}`}
                  data-active={isActive ? "true" : "false"}
                  aria-current={isActive ? "page" : undefined}
                  title={route.label}
                >
                  <span className="ico" aria-hidden="true">
                    {route.icon}
                  </span>
                  <span className="side-label">{route.label}</span>
                  {badge ? (
                    <span
                      className="side-badge"
                      id={panel === "mods" ? "mod-count" : "queue-count"}
                      title={badge.stale ? staleTitle : undefined}
                      style={badge.stale ? { opacity: 0.5 } : undefined}
                      aria-label={`${route.label}: ${badge.value}${badge.stale ? "（可能不是最新）" : ""}`}
                    >
                      {badge.value}
                    </span>
                  ) : null}
                </a>
              );
            })}
          </div>
        ))}
      </nav>
    </aside>
  );
}
