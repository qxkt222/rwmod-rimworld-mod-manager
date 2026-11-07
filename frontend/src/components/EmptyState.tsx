/** Reusable empty state — panels use it instead of hand-rolled grey text. */
import type { ReactNode } from "react";

export interface EmptyStateProps {
  title?: string;
  message?: string;
  icon?: string;
  action?: ReactNode;
  testid?: string;
}

export function EmptyState({
  title = "暂无数据",
  message,
  icon = "🗂",
  action,
  testid = "status-empty",
}: EmptyStateProps) {
  return (
    <div className="empty-state" data-testid={testid} role="status">
      <div className="empty-state__icon" aria-hidden="true">
        {icon}
      </div>
      <div className="empty-state__title">{title}</div>
      {message ? <div className="empty-state__message">{message}</div> : null}
      {action ? <div className="empty-state__action">{action}</div> : null}
    </div>
  );
}
