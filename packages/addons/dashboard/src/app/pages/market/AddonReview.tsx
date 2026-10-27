import { useEffect, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import type { AddOnRow } from '@/api/addOnSwitches'
import type { SignContent } from '@/api/signing'
import type { Workspace } from '@/api/types'
import { clickReady } from '@/api/click'
import { hubOnly } from '@/api/hubsign'
import { useClickSign } from '@/components/sign/useClickSign'
import { useHandoff } from '@/components/sign/useHandoff'
import { useSignMethods } from '@/components/sign/SignMethods'
import { ClickSignPanel } from '@/components/sign/ClickSignPanel'
import { HandoffPanel } from '@/components/sign/HandoffPanel'
import { SignPopover } from '@/components/sign/SignPopover'
import { Inline } from '@/components/sign/visible'
import { Button } from '@/components/ui/button'

/** Presentation only: the host's lines, notes and risk are shown as sent (the trust-line rule lives in the host's signcontent.py). */
export function capabilityWords(value: string): string {
  const names: Record<string, string> = { files: 'files', filesystem: 'files', read_files: 'read files', write_files: 'change files', pty: 'terminals', network: 'internet', spawn_agent: 'agents', serve_http: 'web pages', git_push: 'push code', choose_model: 'choose agent models', credential: 'saved sign-in keys', secrets: 'service tokens' }
  return names[value] ?? value
}

export function addonContent(content: SignContent, _marketplace?: boolean): SignContent {
  const lines = content.summary?.lines.filter(l => !l.startsWith('Version ')).map(l => l.startsWith('It says it will ') ? l.replace(/\b[a-z][a-z_]+\b/g, capabilityWords) : l)
  return content.summary && lines ? { ...content, summary: { ...content.summary, lines } } : content
}

export function AddonReview({ workspace, row, marketplace, onBusy, onDone, onClose }: {
  workspace: Workspace; row: AddOnRow; marketplace: boolean; onBusy: (busy: boolean) => void; onDone: () => void; onClose: () => void
}) {
  const turningOn = row.state !== 'on'
  const label = turningOn ? 'Turn on' : 'Turn off'
  const action = { action: turningOn ? 'addon_grant' as const : 'addon_disable' as const, name: row.id }
  const methods = useSignMethods(workspace.id)
  const click = useClickSign(workspace.id, () => onDone())
  const terminal = useHandoff(workspace.id, phase => { if (phase === 'done') onDone() })
  const viaClick = clickReady(workspace) && (!methods.allowed || methods.allowed.includes('click'))
  // The workspace card can be older than the host's mode (the hub was switched to Full after this page loaded): the host's
  // `sign.hub_only` is the truth, and the review then becomes the hub approval (FB15 / run 12a).
  const qc = useQueryClient()
  const [forcedHub, setForcedHub] = useState(false)
  const viaHub = hubOnly(workspace) || forcedHub
  const refused = [click.error, terminal.error].some(e => (e as { code?: string } | null)?.code === 'sign.hub_only')
  useEffect(() => { if (refused) { setForcedHub(true); void qc.invalidateQueries({ queryKey: ['workspaces'] }) } }, [refused, qc])
  const started = useRef(false)
  useEffect(() => {
    if (methods.loading || viaHub || started.current) return
    started.current = true
    if (viaClick) void click.start(action)
    else void terminal.prepare(action)
  })
  // One click on: the press on the switch is the approval; the review shows under the row while it signs (owner). Off: the person confirms.
  const autoSigned = useRef(false)
  useEffect(() => {
    if (!viaClick || click.phase !== 'ready' || autoSigned.current) return
    autoSigned.current = true
    void click.sign()
  }, [viaClick, click.phase, click.sign])
  const signing = click.phase === 'signing' || click.phase === 'settling'
  useEffect(() => { onBusy(signing) }, [signing, onBusy])
  const [waitUntil, setWaitUntil] = useState(0)
  useEffect(() => { if (click.problem?.waitMs) setWaitUntil(Date.now() + click.problem.waitMs) }, [click.problem])
  const panel = useRef<HTMLDivElement>(null)
  useEffect(() => { if (click.view || terminal.request) panel.current?.scrollIntoView?.({ block: 'nearest' }) }, [click.view, terminal.request])
  if (viaHub) return <SignPopover ws={workspace.id} title={`${label} ${row.title}`} action={action} inline autoStart forceHub onClose={onClose} onSettled={phase => { if (phase === 'done') onDone() }} />
  const close = async () => { if (viaClick) { if (click.problem?.next !== 'check') await click.cancel() } else await terminal.cancel(); onClose() }
  return <div ref={panel} className="mt-2 space-y-2 rounded-lg border border-border bg-surface p-3 [&_[data-testid=sign-exact]]:max-h-[30vh] [&_[data-testid=sign-exact]]:overflow-auto" aria-label={`${label} ${row.title}`}>
    <p className="font-medium">{label} <Inline>{row.title}</Inline></p>
    {row.version && <details className="text-xs text-text-muted"><summary className="cursor-pointer">Technical details</summary><p>Version <Inline>{row.version}</Inline></p></details>}
    {viaClick && click.view ? <ClickSignPanel answer={{ ...click.view, view: { ...click.view.view, content: click.view.view.content && addonContent(click.view.view.content, marketplace) } }} ws={workspace.id} problem={click.problem} signing={signing} ready={click.phase === 'ready'} waitUntil={waitUntil} extra={click.phase === 'failed' && click.problem?.next !== 'check' ? <Button onClick={() => void (click.problem?.next === 'review' ? click.review() : click.again(action))}>Review again</Button> : undefined} label={`Approve and ${label.toLowerCase()}`} onSign={() => void click.sign()} onCancel={() => void close()} /> : terminal.request && terminal.phase === 'waiting' ? <>
      <HandoffPanel request={{ ...terminal.request, content: terminal.request.content && addonContent(terminal.request.content, marketplace) }} />
      <Button variant="ghost" onClick={() => void close()}>Cancel</Button>
    </> : <>
      <p className="text-text-muted">{click.problem?.body ?? terminal.error?.message ?? 'Reading what this add-on can access…'}</p>
      <Button variant="ghost" onClick={() => void close()}>Cancel</Button>
      {click.phase === 'failed' && click.problem?.next !== 'check' && <Button onClick={() => void click.again(action)}>Try again</Button>}
      {terminal.error && <Button onClick={() => void terminal.prepare(action, terminal.retry + 1)}>Try again</Button>}
    </>}
  </div>
}
