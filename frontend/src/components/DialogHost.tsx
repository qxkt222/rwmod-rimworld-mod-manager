/**
 * Application dialog host — renders the `activeDialog` declared in the UI
 * slice. Panels open dialogs through `useUIStore.getState().openDialog(...)`
 * instead of mounting their own overlays, so Esc closes exactly one layer.
 */
import { useEffect, useRef } from "react";
import { createPortal } from "react-dom";

import { useUIStore } from "../store/ui";

export function DialogHost() {
  const dialog = useUIStore((state) => state.activeDialog);
  const closeDialog = useUIStore((state) => state.closeDialog);
  const primaryRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (dialog) primaryRef.current?.focus();
  }, [dialog]);

  if (!dialog) return null;

  const confirm = () => {
    dialog.onConfirm?.();
    closeDialog();
  };
  const cancel = () => {
    dialog.onCancel?.();
    closeDialog();
  };

  return createPortal(
    <div
      className="dialog-overlay"
      data-testid={`dialog-${dialog.name}`}
      role="presentation"
    >
      <div className="dialog-backdrop" onClick={cancel} />
      <div className="dialog-box" role="dialog" aria-modal="true" aria-label={dialog.title}>
        <h2 className="dialog-box__title">{dialog.title}</h2>
        {dialog.body ? <div className="dialog-box__body">{dialog.body}</div> : null}
        <div className="dialog-box__actions">
          <button type="button" className="btn btn-ghost" onClick={cancel}>
            {dialog.cancelLabel ?? "取消"}
          </button>
          <button
            ref={primaryRef}
            type="button"
            className={`btn ${dialog.danger ? "btn-danger" : "btn-primary"}`}
            onClick={confirm}
          >
            {dialog.confirmLabel ?? "确定"}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
