import type { ReactNode, Ref } from 'react'
import type { TerminalSessionView } from '@/api/terminals'

export function TerminalHeader({ session, interactive, rail, picker, leaveRef, onCopy, onFind, onDownload, onEnd, onOpen }: {
  session: TerminalSessionView; interactive: boolean; rail: boolean; picker?: ReactNode; leaveRef: Ref<HTMLButtonElement>
  onCopy: () => void; onFind: () => void; onDownload: () => void; onEnd: () => void; onOpen: () => void
}) {
  const control = 'shrink-0 rounded px-1.5 text-xs hover:bg-surface-3 focus-visible:outline-2 focus-visible:outline-brand'
  return <header className="flex h-8 min-w-0 items-center gap-1 border-b border-border bg-surface px-1 text-xs">
    {picker}
    <span title={session.label} className="min-w-0 truncate font-medium">{session.label}</span>
    <span className="shrink-0 rounded bg-surface-2 px-1 text-text-muted">{session.status === 'running' ? 'Live' : 'Ended'}</span>
    <span className="shrink-0 rounded bg-surface-2 px-1">{session.kind === 'agent' ? 'Agent output · view only' : interactive ? 'Your shell · interactive' : 'Your shell · view only'}</span>
    {!rail && <>
      <span title={`${session.ctx.branch} · ${session.ctx.cwd}`} className="min-w-0 flex-1 truncate font-mono text-text-muted">{session.ctx.branch} · {session.ctx.cwd}</span>
      <button className={control} onClick={onCopy}>Copy</button>
      <button className={control} onClick={onFind} title="Find (⌘F)">Find</button>
      <button className={control} onClick={onDownload} aria-label="Download transcript" title="Download transcript">↓</button>
      {interactive && <button className={control} onClick={onEnd}>End session</button>}
    </>}
    <button ref={leaveRef} className={control} aria-label="Leave terminal" title="Leave terminal">{interactive ? 'Esc Esc to leave' : 'Tab to leave'}</button>
    {rail && <button className={control} onClick={onOpen}>Open in Terminals</button>}
  </header>
}
