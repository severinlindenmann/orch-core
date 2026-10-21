import { useQuery } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { cleanTable, routingQueries, type ModelTable } from '@/api/modelRouting'
import { queries } from '@/api/queries'
import type { HandoffAction } from '@/api/handoff'
import { Inline } from '@/components/sign/visible'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { LoadFailed } from '@/components/LoadFailed'
import { ModelRoutingForm } from './ModelRouting'
import { PublishForm, publishValidation } from './PublishServer'

export function AddonSetup({ ws, name, enabling, close, submit }: { ws: string; name: 'publish' | 'model-routing'; enabling: boolean; close: () => void; submit: (title: string, action: HandoffAction) => void }) {
  const title = `${enabling ? 'Turn on' : 'Set up'} ${name === 'publish' ? 'Publish' : 'Model routing'}`
  const choices = useQuery({ ...routingQueries.choices(ws), enabled: name === 'model-routing' })
  const settings = useQuery({ ...queries.hostSettings(ws), enabled: name === 'model-routing', retry: false })
  const [draft, setDraft] = useState<ModelTable>({})
  useEffect(() => setDraft(settings.data?.models ?? {}), [settings.data?.models])
  const [server, setServer] = useState('')
  const [share, setShare] = useState('')
  const models = cleanTable(draft)
  const ready = name === 'publish' ? publishValidation(server, share).ready : settings.isSuccess && !!choices.data && Object.keys(models).length > 0
  const action: HandoffAction = enabling
    ? name === 'publish' ? { action: 'addon_enable', name, publish: { server, share } } : { action: 'addon_enable', name, models }
    : name === 'publish' ? { action: 'publish_setup', server, share } : { action: 'models_set', models }
  return (
    <Dialog open onOpenChange={(open) => !open && close()}>
      <DialogContent className="max-h-[85vh] overflow-y-auto border-border bg-surface">
        <DialogHeader>
          <DialogTitle><Inline>{title}</Inline></DialogTitle>
          <DialogDescription>{enabling ? 'Choose the settings, then review and sign to turn it on.' : 'Choose the settings, then review and sign.'}</DialogDescription>
        </DialogHeader>
        {name === 'publish' ? <>
          <p className="text-[13px] text-text-muted">Publish is signed in your terminal.</p>
          <PublishForm server={server} share={share} setServer={setServer} setShare={setShare} />
        </> : choices.isError || settings.isError ? (
          <LoadFailed what="the model settings" onRetry={() => { void choices.refetch(); void settings.refetch() }} />
        ) : choices.data ? (
          <ModelRoutingForm choices={choices.data} draft={draft} setDraft={setDraft} canEdit={settings.isSuccess} />
        ) : <p role="status">Loading the model choices…</p>}
        <DialogFooter>
          <Button variant="ghost" onClick={close}>Cancel</Button>
          <Button disabled={!ready} onClick={() => submit(title, action)}>{enabling ? 'Turn on' : 'Review and sign'}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
