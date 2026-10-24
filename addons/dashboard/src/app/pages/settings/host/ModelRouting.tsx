// Settings > Add-ons > Model routing (real host): which model each Start agent mode starts on, per agent. Changing it is a
// signed settings change (the typed word); you see every row again when you sign. Start agent shows the model before each
// start, and you can still pick another one there. "Agent default" passes no model at all.
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { Inline, plain } from '@/components/sign/visible'
import { LoadFailed } from '@/components/LoadFailed'
import { Skeleton } from '@/components/ui/skeleton'
import { cleanTable, routingQueries, type ModelTable, type RoutingChoices } from '@/api/modelRouting'
import { queries } from '@/api/queries'
import type { Workspace } from '@/api/types'
import { Button } from '@/components/ui/button'
import { Section } from '../../ticket/shared'
import { OwnerNote } from '../OwnerNote'
import { useSettingsHandoff } from './useSettingsHandoff'
import { AgentName } from '@/components/agents/AgentIcon'

const DEFAULT = ''
const wide = () => typeof window === 'undefined' || !window.matchMedia || window.matchMedia('(min-width: 640px)').matches
const same = (a: ModelTable, b: ModelTable) => JSON.stringify(cleanTable(a)) === JSON.stringify(cleanTable(b))

export function ModelRouting({ workspace, owner, embedded = false }: { workspace: Workspace; owner: boolean; embedded?: boolean }) {
  const ws = workspace.id
  const qc = useQueryClient()
  const choices = useQuery(routingQueries.choices(ws))
  const settings = useQuery({ ...queries.hostSettings(ws), retry: false })
  const signed: ModelTable = settings.data?.models ?? {}
  const [draft, setDraft] = useState<ModelTable>(signed)
  useEffect(() => setDraft(settings.data?.models ?? {}), [settings.data?.models])
  const { ask, dialog } = useSettingsHandoff(ws, () => {
    void settings.refetch()
    void qc.invalidateQueries({ queryKey: queries.addOns(ws).queryKey })
    void qc.invalidateQueries({ queryKey: ['workspaces'] })
  })
  const [custom, setCustom] = useState(false)
  if (choices.isError) return <Wrap embedded={embedded}><LoadFailed what="the model choices" onRetry={() => void choices.refetch()} /></Wrap>
  if (!choices.data) return <Wrap embedded={embedded}><div data-testid="model-routing-loading" role="status" aria-label="Loading the model choices"><Skeleton className="h-24" /></div></Wrap>
  // Editing waits until the signed table has been read: a draft over an unread table could silently undo a setting.
  const ready = settings.isSuccess
  const canEdit = owner && ready
  const changed = !same(draft, signed)
  const turningOff = Object.keys(cleanTable(draft)).length === 0 && Object.keys(cleanTable(signed)).length > 0
  const chosen = Object.values(cleanTable(signed)).reduce((n, m) => n + Object.keys(m as object).length, 0)
  return (
    <Wrap embedded={embedded}>
      <div className="space-y-3 text-[13px]" data-testid="model-routing">
        <p className="text-text-muted">
          Start agent begins each kind of work on the model you choose here. You see the model before every start and can pick another one then. Only an owner can change this, and you review and sign the change.
        </p>
        <p className="text-[12px] text-text-muted" data-testid="agent-default-help">“Agent default” means the agent app uses its own default model; orch does not choose one.</p>
        {settings.isPending && <p role="status" className="text-text-muted">Loading your current choices…</p>}
        {settings.isError && (
          <p role="alert" className="text-danger">Your current choices could not be loaded, so they cannot be changed right now. <Button size="sm" variant="outline" onClick={() => void settings.refetch()}>Retry</Button></p>
        )}
        {settings.data && settings.data.available === false && <p className="text-text-muted">This host does not report its settings, so nothing is shown here.</p>}
        {!custom && (
          <div className="space-y-1.5" data-testid="routing-summary">
            <p>
              {chosen === 0
                ? `Every agent uses its own default model. Available: ${choices.data.harnesses.map((h) => h.label).join(', ') || 'none found'}.`
                : `${chosen} model choice${chosen === 1 ? '' : 's'} saved; the rest use the agent's own default.`}
            </p>
            {choices.data.harnesses.length === 0 && <p className="text-warning">No supported agent was found on this computer. Install one, then come back.</p>}
            <Button size="sm" variant="secondary" aria-expanded={false} onClick={() => setCustom(true)}>Customize models</Button>
          </div>
        )}
        {custom && <Button size="sm" variant="ghost" aria-expanded onClick={() => setCustom(false)} disabled={changed}>Hide model choices</Button>}
        {custom && <ModelRoutingForm choices={choices.data} draft={draft} setDraft={setDraft} canEdit={canEdit} />}
        {custom && turningOff && <p>Model routing needs at least one model chosen. To use the agent defaults for everything, turn it off.</p>}
        {!custom ? null : owner ? (
          <div className="flex flex-wrap gap-2">
            <Button size="sm" disabled={!canEdit || !changed} onClick={() => ask({ title: turningOff ? 'Turn off Model routing' : 'Choose the models for Start agent', action: turningOff ? { action: 'addon_disable', name: 'model-routing' } : { action: 'models_set', models: cleanTable(draft) }, autoStart: true })}>
              {turningOff ? 'Turn off Model routing' : 'Review and sign'}
            </Button>
            {!changed && <span className="self-center text-[12px] text-text-muted" data-testid="routing-nochange">No changes to save.</span>}
            {changed && <Button size="sm" variant="ghost" onClick={() => setDraft(signed)}>Undo my changes</Button>}
          </div>
        ) : (
          <OwnerNote>Only an owner can change this.</OwnerNote>
        )}
        <p className="text-[12px] text-text-muted">If a check on a ticket is failing, Start agent suggests a stronger model. It only suggests: nothing changes until you start.</p>
      </div>
      {dialog}
    </Wrap>
  )
}

export function ModelRoutingForm({ choices, draft, setDraft, canEdit }: { choices: RoutingChoices; draft: ModelTable; setDraft: React.Dispatch<React.SetStateAction<ModelTable>>; canEdit: boolean }) {
  const [openAgents, setOpenAgents] = useState<Record<string, boolean>>({})
  const set = (harness: string, mode: string, model: string) =>
    setDraft((d) => ({ ...d, [harness]: { ...(d as Record<string, Record<string, string>>)[harness], [mode]: model } }))
  return (
    <>
      {choices.harnesses.map((h, i) => (
        <details key={h.id} className="space-y-1 rounded-md border border-border px-3 py-2" data-testid={`routing-${h.id}`} open={openAgents[h.id] ?? (i === 0 || wide())} onToggle={(e) => { const o = (e.currentTarget as HTMLDetailsElement).open; setOpenAgents((p) => (p[h.id] === o ? p : { ...p, [h.id]: o })) }}>
          <summary className="cursor-pointer font-medium">{h.id === 'claude-code' || h.id === 'codex' ? <AgentName harness={h.id} /> : plain(h.label)}</summary>
          <dl className="mt-2 grid grid-cols-1 gap-x-3 gap-y-1.5 sm:grid-cols-[minmax(0,15rem)_minmax(0,1fr)] sm:items-center">
            {choices.modes.map((m) => {
              const id = `route-${h.id}-${m.id}`
              const value = (draft as Record<string, Record<string, string> | undefined>)[h.id]?.[m.id] ?? DEFAULT
              return (
                <div key={m.id} className="contents">
                  <dt>
                    <label htmlFor={id} className="text-text-muted"><Inline>{m.label}</Inline></label>
                  </dt>
                  <dd>
                    <select id={id} className="h-8 w-full max-w-xs rounded border border-border bg-bg px-2" value={value} disabled={!canEdit} onChange={(e) => set(h.id, m.id, e.target.value)}>
                      <option value={DEFAULT}>Agent default</option>
                      {h.models.map((x) => (
                        <option key={x.id} value={x.id}>{plain(x.label)}</option>
                      ))}
                    </select>
                    {value === DEFAULT && <span className="mt-0.5 block text-[12px] text-text-faint">Chosen by the agent app</span>}
                  </dd>
                </div>
              )
            })}
          </dl>
        </details>
      ))}
    </>
  )
}

function Wrap({ embedded, children }: { embedded: boolean; children: React.ReactNode }) {
  return embedded ? <div className="mt-2 rounded-md border border-border bg-bg/40 p-3">{children}</div> : <Section title="Default models">{children}</Section>
}
