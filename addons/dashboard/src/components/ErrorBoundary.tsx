import { Component, type ReactNode } from 'react'
import { TriangleAlert } from 'lucide-react'

interface Props {
  children: ReactNode
  /** Changing this value clears a caught error (for example the route path). */
  resetKey?: unknown
  /** What to show instead; receives a function that retries the children. */
  fallback: (retry: () => void) => ReactNode
}

/** Keeps one failing subtree from blanking its parent. Logs the error once for developers. */
export class ErrorBoundary extends Component<Props, { failed: boolean }> {
  state = { failed: false }
  static getDerivedStateFromError() {
    return { failed: true }
  }
  componentDidCatch(error: unknown) {
    console.error('Caught by ErrorBoundary:', error)
  }
  componentDidUpdate(prev: Props) {
    if (this.state.failed && prev.resetKey !== this.props.resetKey) this.setState({ failed: false })
  }
  render() {
    return this.state.failed ? this.props.fallback(() => this.setState({ failed: false })) : this.props.children
  }
}

/** The calm panel for a whole page: the shell around it stays usable. */
export function PageProblem({ retry }: { retry: () => void }) {
  return (
    <div role="alert" className="mx-auto mt-10 max-w-md rounded-lg border border-border bg-surface p-5 text-center">
      <TriangleAlert className="mx-auto mb-2 size-5 text-warning" aria-hidden />
      <h2 className="text-[15px] font-semibold text-text">This page hit a problem</h2>
      <p className="mt-1 text-[13px] text-text-muted">Nothing was lost. Use the sidebar to go elsewhere, or reload this page.</p>
      <button type="button" onClick={retry} className="mt-3 rounded-md border border-border-strong px-3 py-1.5 text-[13px] text-text hover:bg-surface-2">
        Reload
      </button>
    </div>
  )
}

/** A small inline note for one failed block or panel. */
export function BlockProblem({ what }: { what: string }) {
  return (
    <div role="note" className="my-2 flex items-center gap-2 rounded-md border border-dashed border-border px-3 py-2 text-[12px] text-text-muted">
      <TriangleAlert className="size-3.5 text-warning" aria-hidden />
      {what} could not be drawn.
    </div>
  )
}
