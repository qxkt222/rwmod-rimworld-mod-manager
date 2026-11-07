/** Reusable loading state. */
export interface LoadingStateProps {
  message?: string;
  testid?: string;
}

export function LoadingState({ message = "加载中…", testid = "status-loading" }: LoadingStateProps) {
  return (
    <div className="loading-state" data-testid={testid} role="status" aria-live="polite">
      <span className="loading-state__spinner" aria-hidden="true" />
      <span>{message}</span>
    </div>
  );
}
