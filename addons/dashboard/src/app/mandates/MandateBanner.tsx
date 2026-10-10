// The shell banner while the preview mandate is in force (concept-mandates.md §3), PREVIEW ONLY (the wide mandate, owner decision 10 Oct evening). Calm (never orange,
// never a warning colour), one line that truncates, so it fits the 13" page beside the dock. Dismissible for the
// browser session (per mandate and per phase: a Stop shows it again). Nothing here signs.
import { Link } from '@tanstack/react-router'
import { ShieldCheck, X } from 'lucide-react'
import { useState } from 'react'
import { plain } from '@/components/sign/visible'
import { Button } from '@/components/ui/button'
import { useWorkspace } from '../workspace'
import { Pill } from '../pages/ticket/shared'
import { shownMandate, StopDialog, useCanMandate, useMandatesPreview, weekday } from './shared'

const DISMISS = 'orch.preview.mandates.banner.'

function readDismissed(key: string): boolean {
  try {
    return sessionStorage.getItem(key) === '1'
  } catch {
    return false
  }
}

export function MandateBanner() {
  const { workspace } = useWorkspace()
  const ws = workspace?.id
  const q = useMandatesPreview(ws)
  const m = shownMandate(q.data)
  const owner = useCanMandate()
  const [stopping, setStopping] = useState(false)
  const key = m && ws ? `${DISMISS}${ws}.${m.id}.${m.state === 'active' ? 'active' : 'stop'}` : null
  const [dismissed, setDismissed] = useState<Record<string, boolean>>({})
  if (!m || !ws || !key) return null
  if (dismissed[key] ?? readDismissed(key)) return null

  const live = m.decisions.filter((d) => !d.voided).length
  const phase = m.state === 'stopping' ? 'Stopping…' : m.state === 'stopped' ? `Stopped at #${m.stop?.boundary_seq}` : `until ${weekday(m.expires)}`
  const text = `Mandate ${plain(m.id)} · for ${plain(m.issuer)} · whole workspace · ${live} decision${live === 1 ? '' : 's'} · ${phase}`
  return (
    <div role="region" aria-label="Mandate in force (preview)" data-testid="mandate-banner" className="flex h-9 shrink-0 items-center gap-2 border-b border-border bg-surface px-4 text-[13px]">
      <ShieldCheck className="size-4 shrink-0 text-text-muted" aria-hidden />
      <Pill className="shrink-0">Preview</Pill>
      <span className="min-w-0 flex-1 truncate text-text" title={`${text}. Preview of a proposed feature: nothing here signs anything.`} aria-live="polite">
        {text}
      </span>
      {owner && m.state === 'active' && (
        <Button size="xs" variant="outline" className="shrink-0" onClick={() => setStopping(true)}>
          Stop…
        </Button>
      )}
      <Link to="/agents" search={{ tab: 'mandates' }} className="shrink-0 rounded px-1 text-[12px] text-text-muted underline-offset-2 outline-none hover:text-text hover:underline focus-visible:ring-2 focus-visible:ring-ring">
        Details
      </Link>
      <button
        type="button"
        aria-label="Hide the mandate banner for this session"
        className="shrink-0 rounded p-0.5 text-text-muted outline-none hover:bg-surface-2 hover:text-text focus-visible:ring-2 focus-visible:ring-ring"
        onClick={() => {
          try {
            sessionStorage.setItem(key, '1')
          } catch {
            /* storage blocked: hidden until a reload */
          }
          setDismissed((d) => ({ ...d, [key]: true }))
        }}
      >
        <X className="size-3.5" />
      </button>
      {stopping && <StopDialog ws={ws} m={m} onClose={() => setStopping(false)} />}
    </div>
  )
}
