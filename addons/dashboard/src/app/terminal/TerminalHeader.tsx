import { ArrowRightFromLine, Copy, Download, Search } from 'lucide-react'
import type { ReactNode, Ref } from 'react'
import type { TerminalSessionView } from '@/api/terminals'
import { Button } from '@/components/ui/button'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'

function IconButton({ label, onClick, disabled, innerRef, children }: { label: string; onClick: () => void; disabled?: boolean; innerRef?: Ref<HTMLButtonElement>; children: ReactNode }) {
  return <Tooltip>
    <TooltipTrigger asChild>
      <Button ref={innerRef as never} variant="ghost" size="icon-xs" aria-label={label.replace(/ \(.*\)$/, '')} disabled={disabled} onClick={onClick}>{children}</Button>
    </TooltipTrigger>
    <TooltipContent side="bottom">{label}</TooltipContent>
  </Tooltip>
}

export function TerminalHeader({ session, interactive, rail, picker, leaveRef, canFind, onCopy, onFind, onDownload, onEnd, onLeave, onOpen }: {
  session: TerminalSessionView; interactive: boolean; rail: boolean; picker?: ReactNode; leaveRef: Ref<HTMLButtonElement>; canFind: boolean
  onCopy: () => void; onFind: () => void; onDownload: () => void; onEnd: () => void; onLeave: () => void; onOpen: () => void
}) {
  return <header className="flex h-8 min-w-0 items-center gap-1.5 border-b border-border bg-surface px-2 text-xs">
    {picker}
    <span title={session.label} className="max-w-[45%] min-w-0 shrink-0 truncate font-medium">{session.label}</span>
    <span className="shrink-0 rounded bg-surface-2 px-1.5 text-text-muted">{session.status === 'running' ? 'Live' : 'Ended'}</span>
    <span className="shrink-0 rounded bg-surface-2 px-1.5">{session.kind === 'agent' ? 'Agent output · view only' : interactive ? 'Your shell · interactive' : 'Your shell · view only'}</span>
    {rail ? <span className="flex-1" /> : <span title={`${session.ctx.branch} · ${session.ctx.cwd}`} className="flex min-w-0 flex-1 gap-1 overflow-hidden font-mono text-text-muted"><span className="min-w-0 truncate">{session.ctx.branch}</span><span className="min-w-0 shrink-[999] truncate">· {session.ctx.cwd}</span></span>}
    {!rail && <>
      <IconButton label="Copy selection or transcript" onClick={onCopy}><Copy /></IconButton>
      <IconButton label="Find (⌘F)" onClick={onFind} disabled={!canFind}><Search /></IconButton>
      <IconButton label="Download transcript" onClick={onDownload}><Download /></IconButton>
      {interactive && <Button variant="ghost" size="xs" onClick={onEnd}>End session</Button>}
    </>}
    <IconButton label={interactive ? 'Leave terminal (Esc Esc)' : 'Leave terminal (Tab)'} onClick={onLeave} innerRef={leaveRef}><ArrowRightFromLine /></IconButton>
    {rail && <Button variant="ghost" size="xs" onClick={onOpen}>Open in Terminals</Button>}
  </header>
}
