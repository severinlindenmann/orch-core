// The dock's session header, drawn as a compact tmux-flavoured strip under the terminal: which window, what you can
// do in it (type a prompt, type a command, read only, transcript), that it is simulated, and the terminal's actions.

import { ArrowRightFromLine, Copy, Download, Search } from 'lucide-react'
import type { ReactNode, Ref } from 'react'
import { findHarness } from '@/api/harnesses'
import type { TerminalSessionView } from '@/api/terminals'
import { Button } from '@/components/ui/button'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'

/** What you can do in this session, in words. */
export function sessionMode(s: Pick<TerminalSessionView, 'status' | 'kind' | 'harness'>, interactive: boolean): string {
  if (s.status === 'stopped') return 'Ended · Transcript'
  const h = findHarness(s.harness)
  if (interactive) return h && h.view !== 'shell' ? `Your ${h.label} · Type a prompt` : 'Your shell · Type a command'
  return s.kind === 'agent' ? 'Watching agent · Read only' : 'Read only'
}

function Icon({ label, onClick, disabled, innerRef, children }: { label: string; onClick: () => void; disabled?: boolean; innerRef?: Ref<HTMLButtonElement>; children: ReactNode }) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button ref={innerRef as never} variant="ghost" size="icon-xs" aria-label={label.replace(/ \(.*\)$/, '')} disabled={disabled} onClick={onClick}>
          {children}
        </Button>
      </TooltipTrigger>
      <TooltipContent side="top">{label}</TooltipContent>
    </Tooltip>
  )
}

export function SessionStrip({ session, interactive, name, compact = false, stripRef, leaveRef, canFind, onCopy, onFind, onDownload, onEnd, onLeave }: {
  session: TerminalSessionView
  interactive: boolean
  /** The window as the dock names it: "1 Claude · Yours". */
  name: string
  /** Narrow: only the window number, no running word, no branch, no copy/find/download (⌘F still finds). */
  compact?: boolean
  stripRef?: Ref<HTMLDivElement>
  leaveRef: Ref<HTMLButtonElement>
  canFind: boolean
  onCopy: () => void
  onFind: () => void
  onDownload: () => void
  onEnd: () => void
  onLeave: () => void
}) {
  const mode = sessionMode(session, interactive)
  return (
    <div ref={stripRef} tabIndex={-1} role="group" aria-label={`Session: ${name} · ${mode}`} data-session-strip
      className="flex h-7 shrink-0 items-center gap-2 border-t border-border bg-surface px-2 text-xs outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-brand">
      <span className="shrink-0 font-mono text-brand" title={name}>[{compact ? name.split(' ')[0] : name}]</span>
      <span className="min-w-0 truncate font-medium" title={mode} data-session-mode>{mode}</span>
      <Tooltip>
        <TooltipTrigger asChild>
          <span tabIndex={0} className="shrink-0 rounded bg-surface-2 px-1.5 text-[11px] text-text-faint outline-none focus-visible:ring-2 focus-visible:ring-brand">Simulated</span>
        </TooltipTrigger>
        <TooltipContent side="top">Scripted demo — no commands or models run</TooltipContent>
      </Tooltip>
      {session.status === 'running' && !compact && <span className="shrink-0 text-text-muted">Running (simulated)</span>}
      <span title={`${session.ctx.branch} · ${session.ctx.cwd}`} className="min-w-0 flex-1 basis-0 truncate font-mono text-text-faint">{compact ? '' : session.ctx.branch}</span>
      {!compact && (
        <>
          <Icon label="Copy selection or transcript" onClick={onCopy}><Copy /></Icon>
          <Icon label="Find (⌘F)" onClick={onFind} disabled={!canFind}><Search /></Icon>
          <Icon label="Download transcript" onClick={onDownload}><Download /></Icon>
        </>
      )}
      {interactive && <Button variant="ghost" size="xs" onClick={onEnd}>End session</Button>}
      <Icon label={interactive ? 'Leave terminal (Esc Esc)' : 'Leave terminal (Tab)'} onClick={onLeave} innerRef={leaveRef}><ArrowRightFromLine /></Icon>
    </div>
  )
}
