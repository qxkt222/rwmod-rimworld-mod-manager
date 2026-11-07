/**
 * In-app confirmation dialog (replaces `window.confirm` for new panels).
 *
 * NOTE: the legacy `profiles` panel keeps native `prompt`/`confirm` until
 * FE-TEST migrates `profiles.spec.ts` in the same wave (ui-redesign §5.4).
 */
import { useEffect, useRef, type ReactNode } from "react";
import { createPortal } from "react-dom";

export interface ConfirmDialogProps {
  open: boolean;
  title: string;
  message?: ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  danger?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
  /** testid suffix: `dialog-<name>` (default `confirm`). */
  name?: string;
}

export function ConfirmDialog({
  open,
  title,
  message,
  confirmLabel = "确定",
  cancelLabel = "取消",
  danger = false,
  onConfirm,
  onCancel,
  name = "confirm",
}: ConfirmDialogProps) {
  const confirmRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (open) confirmRef.current?.focus();
  }, [open]);

  if (!open) return null;

  return createPortal(
    <div className="dialog-overlay" data-testid={`dialog-${name}`} role="presentation">
      <div className="dialog-backdrop" onClick={onCancel} />
      <div
        className="dialog-box"
        role="dialog"
        aria-modal="true"
        aria-label={title}
        onKeyDown={(event) => {
          if (event.key === "Escape") {
            event.stopPropagation();
            onCancel();
          }
        }}
      >
        <h2 className="dialog-box__title">{title}</h2>
        {message ? <div className="dialog-box__body">{message}</div> : null}
        <div className="dialog-box__actions">
          <button type="button" className="btn btn-ghost" onClick={onCancel}>
            {cancelLabel}
          </button>
          <button
            ref={confirmRef}
            type="button"
            className={`btn ${danger ? "btn-danger" : "btn-primary"}`}
            onClick={onConfirm}
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
