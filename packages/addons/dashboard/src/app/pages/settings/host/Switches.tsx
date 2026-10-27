// Two plain one-click switches of the workspace (no review, no signature): Claude Code's folder question, and the
// history of the person's work. The host decides what is allowed; its plain refusal is shown as it is.
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Info } from 'lucide-react'
import { api } from '@/api/client'
import { queries } from '@/api/queries'
import type { Workspace } from '@/api/types'
import { toastApiError } from '@/app/toast'
import { Button } from '@/components/ui/button'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { Switch } from '@/components/ui/switch'
import { Section } from '../../ticket/shared'
import { useViewerRole } from './useViewerRole'

const TRUST_LABEL = "Let Claude Code open this workspace's folders without asking"
const TRUST_MORE =
  "Claude Code asks you to trust every folder it works in. This lets orch's agents start in this workspace's ticket folders without that question. Only for folders orch makes; it never trusts a folder that has someone else's settings."
const HISTORY_LABEL = 'Keep a history of your work'
const HISTORY_MORE = 'Orch saves your tickets and their history on this computer every 15 minutes, in the background. It does not send them anywhere.'

function SwitchRow({ label, line, more, checked, disabled, onChange, children }: {
  label: string
  line: string
  more: string
  checked: boolean
  disabled?: boolean
  onChange: (next: boolean) => void
  children?: React.ReactNode
}) {
  return (
    <div className="space-y-1 py-2 text-[13px]">
      <div className="flex items-center gap-3">
        <Switch checked={checked} disabled={disabled} onCheckedChange={onChange} aria-label={label} />
        <span className="font-medium">{label}</span>
        <Popover>
          <PopoverTrigger asChild>
            <button type="button" aria-label={`More about ${label}`} className="rounded p-0.5 text-text-faint hover:text-text-muted">
              <Info className="size-3.5" aria-hidden />
            </button>
          </PopoverTrigger>
          <PopoverContent align="start" className="w-80 max-w-[calc(100vw-2rem)] text-[13px]">{more}</PopoverContent>
        </Popover>
      </div>
      <p className="text-text-muted">{line}</p>
      {children}
    </div>
  )
}

/** Settings > General: the switches that need no review. A row the host cannot offer is not drawn. */
export function WorkspaceSwitches({ workspace }: { workspace: Workspace }) {
  const ws = workspace.id
  const qc = useQueryClient()
  const role = useViewerRole(workspace)
  const mayRecord = role === 'owner' // the host checks the owner role on both routes
  const trust = useQuery({ ...queries.claudeTrust(ws), enabled: mayRecord })
  const records = useQuery({ ...queries.records(ws), enabled: mayRecord })

  const grant = useMutation({
    mutationFn: (allowParent: boolean) => api.grantClaudeTrust(ws, allowParent),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['claude-trust', ws] }),
    onError: (e) => toastApiError(e),
  })
  const history = useMutation({
    mutationFn: (on: boolean) => (on ? api.recordsAutoOn(ws) : api.recordsAutoOff(ws)),
    onSettled: () => qc.invalidateQueries({ queryKey: ['records', ws] }),
    onError: (e) => toastApiError(e),
  })

  const t = trust.data
  const showTrust = !!t && t.available
  const r = records.data
  const showHistory = mayRecord && !!r && r.available && r.configured !== false && !!r.repo
  if (!showTrust && !showHistory) return null

  const parentIssue = !!t && !t.offered && t.parent_config !== '' && t.why_not !== ''
  const trustLine = !t ? '' : t.on ? 'On. Orch can only turn this on here.' : t.offered ? 'Off. Claude Code asks about each folder.' : t.why_not
  const historyOn = r?.auto?.on === true
  return (
    <Section title="Switches">
      <div className="divide-y divide-border">
        {showTrust && t && (
          <SwitchRow
            label={TRUST_LABEL}
            line={trustLine}
            more={TRUST_MORE}
            checked={t.on}
            disabled={t.on || grant.isPending || (!t.offered && !parentIssue)}
            onChange={(next) => { if (next && t.offered) grant.mutate(false) }}
          >
            {parentIssue && !t.on && (
              <Button size="sm" variant="secondary" disabled={grant.isPending} onClick={() => grant.mutate(true)}>Trust anyway</Button>
            )}
          </SwitchRow>
        )}
        {showHistory && (
          <SwitchRow
            label={HISTORY_LABEL}
            line={historyOn ? 'On. Your work is saved in the background.' : 'Off. Your work is not being saved for you.'}
            more={HISTORY_MORE}
            checked={historyOn}
            disabled={history.isPending}
            onChange={(next) => history.mutate(next)}
          />
        )}
      </div>
    </Section>
  )
}
