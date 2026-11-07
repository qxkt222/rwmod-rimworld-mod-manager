/**
 * Panel-level error boundary. A crashing panel must not blank the shell, so
 * PanelHost wraps every lazy panel in one of these keyed by panel name.
 */
import { Component, type ErrorInfo, type ReactNode } from "react";

import { ErrorState } from "./ErrorState";

export interface ErrorBoundaryProps {
  children: ReactNode;
  /** Shown in the fallback heading, e.g. the panel label. */
  label?: string;
  onReset?: () => void;
}

interface ErrorBoundaryState {
  error: Error | null;
}

export class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  state: ErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error("[rwmod] panel render failed", error, info.componentStack);
  }

  private reset = (): void => {
    this.setState({ error: null });
    this.props.onReset?.();
  };

  render(): ReactNode {
    if (this.state.error) {
      return (
        <ErrorState
          title={`${this.props.label ?? "面板"}加载失败`}
          message={this.state.error.message || "渲染时发生未知错误"}
          onRetry={this.reset}
          retryLabel="重新加载"
          testid="status-panel-error"
        />
      );
    }
    return this.props.children;
  }
}

export default ErrorBoundary;
