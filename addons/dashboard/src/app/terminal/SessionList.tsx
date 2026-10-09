import type { TerminalSessionView } from '@/api/terminals'

export function SessionList({ sessions, selected, onSelect }: { sessions: TerminalSessionView[]; selected: string; onSelect: (id: string) => void }) {
  const ended = sessions.filter((s) => s.status === 'stopped')
  const row = (s: TerminalSessionView) => <li key={s.id}>
    <button type="button" aria-current={selected === s.id ? 'true' : undefined} onClick={() => onSelect(s.id)} className={`w-full rounded px-3 py-2 text-left hover:bg-surface-2 ${selected === s.id ? 'bg-surface-2' : ''}`}>
      <span className="flex items-center gap-2 text-[13px]">
        {s.status === 'running' && <span aria-hidden="true" className="size-1.5 shrink-0 rounded-full bg-success" />}
        <span className="truncate">{s.label}</span>
      </span>
      <span className="block text-xs text-text-muted">{s.interactive ? 'Interactive' : 'View only'} · started {s.started.slice(11, 16)}</span>
    </button>
  </li>
  return <nav aria-label="Terminal sessions" className="min-w-0 space-y-2">
    <ul>{sessions.filter((s) => s.status === 'running').map(row)}</ul>
    {ended.length > 0 && <details><summary className="cursor-pointer px-3 py-2 text-xs text-text-muted">Ended ({ended.length})</summary><ul>{ended.map(row)}</ul></details>}
  </nav>
}
