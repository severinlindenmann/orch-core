export function TasksChip(_: { ticket?: string }) {
  return <span data-testid="focus-tasks-chip" className="rounded-full border border-border px-2 py-1 text-xs text-text-muted">Tasks coming in F4.</span>
}

export function TasksList(_: { ticket?: string }) {
  return <section data-testid="focus-tasks-list" aria-label="Tasks" className="border-t border-border p-4 text-sm text-text-muted">Tasks coming in F4.</section>
}
