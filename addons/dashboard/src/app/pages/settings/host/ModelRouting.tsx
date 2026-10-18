// Settings > Add-ons > Model routing (real host): which model each Start agent mode starts on, per agent. Changing it is a
// signed settings change (the typed word); you see every row again when you sign. Start agent shows the model before each
// start, and you can still pick another one there. "Agent default" passes no model at all.
import { useQuery } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { cleanTable, routingQueries, type ModelTable } from '@/api/modelRouting'
import { queries } from '@/api/queries'
import type { Workspace } from '@/api/types'
import { Button } from '@/components/ui/button'
import { Section } from '../../ticket/shared'
import { OwnerNote } from '../OwnerNote'
import { useSettingsHandoff } from './useSettingsHandoff'

const DEFAULT = ''
const same = (a: ModelTable, b: ModelTable) => JSON.stringify(cleanTable(a)) === JSON.stringify(cleanTable(b))

export function ModelRouting({ workspace, owner }: { workspace: Workspace; owner: boolean }) {
  const ws = workspace.id
  const choices = useQuery(routingQueries.choices(ws))
  const settings = useQuery({ ...queries.hostSettings(ws), retry: false })
  const signed: ModelTable = settings.data?.models ?? {}
  const [draft, setDraft] = useState<ModelTable>(signed)
  useEffect(() => setDraft(settings.data?.models ?? {}), [settings.data?.models])
  const { ask, dialog } = useSettingsHandoff(ws, () => void settings.refetch())
  if (!choices.data) return null
  const changed = !same(draft, signed)
  const set = (harness: string, mode: string, model: string) =>
    setDraft((d) => ({ ...d, [harness]: { ...(d as Record<string, Record<string, string>>)[harness], [mode]: model } }))
  return (
    <Section title="Model routing: which model each kind of work starts on">
      <div className="space-y-3 text-[13px]" data-testid="model-routing">
        <p className="text-text-muted">
          Start agent begins each kind of work on the model you choose here. You see the model before every start and can pick another one then. Only an owner changes this, and you sign the change with the typed word.
        </p>
        {settings.data && settings.data.available === false && <p className="text-text-muted">This host does not report its settings, so nothing is shown here.</p>}
        {choices.data.harnesses.map((h) => (
          <div key={h.id} className="space-y-1" data-testid={`routing-${h.id}`}>
            <p className="font-medium">{h.label}</p>
            <dl className="grid grid-cols-1 gap-x-3 gap-y-1.5 sm:grid-cols-[minmax(0,15rem)_minmax(0,1fr)] sm:items-center">
              {choices.data.modes.map((m) => {
                const id = `route-${h.id}-${m.id}`
                const value = (draft as Record<string, Record<string, string> | undefined>)[h.id]?.[m.id] ?? DEFAULT
                return (
                  <div key={m.id} className="contents">
                    <dt>
                      <label htmlFor={id} className="text-text-muted">{m.label}</label>
                    </dt>
                    <dd>
                      <select id={id} className="h-8 w-full max-w-xs rounded border border-border bg-bg px-2" value={value} disabled={!owner} onChange={(e) => set(h.id, m.id, e.target.value)}>
                        <option value={DEFAULT}>Agent default</option>
                        {h.models.map((x) => (
                          <option key={x.id} value={x.id}>{x.label}</option>
                        ))}
                      </select>
                    </dd>
                  </div>
                )
              })}
            </dl>
          </div>
        ))}
        {owner ? (
          <div className="flex flex-wrap gap-2">
            <Button size="sm" disabled={!changed} onClick={() => ask({ title: 'Choose the models for Start agent', action: { action: 'models_set', models: cleanTable(draft) }, autoStart: true })}>
              Continue to sign
            </Button>
            {changed && <Button size="sm" variant="ghost" onClick={() => setDraft(signed)}>Undo my changes</Button>}
          </div>
        ) : (
          <OwnerNote>Only owners can change this.</OwnerNote>
        )}
        <p className="text-[12px] text-text-muted">If a check on a ticket keeps failing, Start agent suggests a stronger model. It only suggests: nothing changes until you start.</p>
      </div>
      {dialog}
    </Section>
  )
}
