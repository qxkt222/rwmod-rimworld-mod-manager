/** Reusable inline error with an optional retry action. */
export interface ErrorStateProps {
  title?: string;
  message: string;
  onRetry?: () => void;
  retryLabel?: string;
  testid?: string;
}

export function ErrorState({
  title = "加载失败",
  message,
  onRetry,
  retryLabel = "重试",
  testid = "status-error",
}: ErrorStateProps) {
  return (
    <div className="error-state" data-testid={testid} role="alert">
      <div className="error-state__title">{title}</div>
      <div className="error-state__message">{message}</div>
      {onRetry ? (
        <button type="button" className="btn btn-ghost btn-sm" onClick={onRetry}>
          {retryLabel}
        </button>
      ) : null}
    </div>
  );
}
