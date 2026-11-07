/**
 * Toast viewport — bottom-right stack driven by `store/ui.ts`.
 *
 * The container is `status-toasts`; each toast is `status-toast` (the E2E
 * helper asserts on the container so several stacked toasts never trip
 * Playwright's strict mode).
 */
import { useUIStore } from "../store/ui";

const ICONS: Record<string, string> = {
  success: "✅",
  error: "❌",
  warn: "⚠️",
  info: "ℹ️",
};

export function ToastViewport() {
  const toasts = useUIStore((state) => state.toasts);
  const dismiss = useUIStore((state) => state.dismissToast);

  return (
    <div
      className="toast-viewport"
      data-testid="status-toasts"
      aria-live="polite"
      aria-atomic="false"
    >
      {toasts.map((toast) => (
        <div
          key={toast.id}
          className={`toast ${toast.type}`}
          data-testid="status-toast"
          data-type={toast.type}
          role="status"
          onClick={() => dismiss(toast.id)}
        >
          <span aria-hidden="true">{ICONS[toast.type] ?? "ℹ️"}</span> {toast.message}
        </div>
      ))}
    </div>
  );
}
