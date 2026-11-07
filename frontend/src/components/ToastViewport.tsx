/**
 * Toast viewport — bottom-right stack driven by `store/ui.ts`.
 *
 * Keeps the legacy `#toast-container` id so `expectToast()` in the E2E helpers
 * keeps working during the two-entry transition.
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
    <div id="toast-container" className="toast-viewport" aria-live="polite" aria-atomic="false">
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
