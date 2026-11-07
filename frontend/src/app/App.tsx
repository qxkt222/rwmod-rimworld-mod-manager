/**
 * React application root — wave 2 real shell.
 *
 * `version` / `status` stay in the public props so the wave-0b smoke test
 * continues to describe a real contract (version badge + accessible status
 * line), while the actual layout lives in `layout/AppShell`.
 */
import { AppShell } from "../layout/AppShell";

export interface AppProps {
  /** Backend/application version shown next to the title. */
  version: string;
  /** One-line runtime status (connection state, bootstrap progress, …). */
  status?: string;
}

const DEFAULT_STATUS = "React 预览入口 · 共享层已就绪";

export function App({ version, status = DEFAULT_STATUS }: AppProps) {
  return (
    <div className="app-shell" data-testid="app-shell">
      <AppShell version={version} status={status} />
    </div>
  );
}

export default App;
