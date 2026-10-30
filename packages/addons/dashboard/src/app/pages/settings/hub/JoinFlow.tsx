// Run-9 feedback 6: connect a hub from a link. The person pastes the link the hub admin gave them; the host fetches the
// hub card and checks it against the link, and the page shows which hub it is. One click then signs the join (HubStep,
// with the backup key: the hub always requires the owner's proof), and the host tells the hub (hub.connect, with the
// link's code and address). Both modes use this review and one-click flow.
import { useEffect, useRef, useState } from 'react'
import { toast } from 'sonner'
import { useQueryClient } from '@tanstack/react-query'
import { InfoDrawer } from '@/components/InfoDrawer'
import { api } from '@/api/client'
import { ApiError } from '@/api/types'
import type { HubLinkInfo } from '@/api/hub'
import { newIdem } from '@/api/runs'
import { Raw } from '@/components/sign/visible'
import { HubStep } from '../host/HubStep'
import { Action, Flow, NoOneClick, caveat, useHubRefresh, useOneClick, violet } from './shared'
import { ModeChoice } from './ModeChoice'

export const LINK = /^https?:\/\/\S+\/c#h1\.[0-9a-f]{32}\.[A-Za-z0-9_-]{1,64}(\.[^\s~]+)?(~[0-9a-f]{32})?$/

/** The connect link the hub's /c page put in the address (`#connect=<link>`), or null. Only a full, well-formed connect link
 *  is returned; anything else in the address is ignored. It prefills the dialog, nothing more: the owner still signs. */
export function connectFromHash(hash: string): string | null {
  const m = /^#?connect=([^&]+)$/.exec(hash)
  if (!m) return null
  try {
    const link = decodeURIComponent(m[1]).trim()
    return LINK.test(link) ? link : null
  } catch { return null }
}

export function JoinFlow({ ws, onClose, initialLink }: { ws: string; onClose: () => void; initialLink?: string }) {
  const refreshHub = useHubRefresh(ws)
  // Closing after the signature (waiting, failed or confirmed) reads the hub status again: allowed while paused.
  const leave = () => { refreshHub(); onClose() }
  const oneClick = useOneClick(ws)
  const [failed, setFailed] = useState(false)
  const [text, setText] = useState(initialLink ?? '')
  const [info, setInfo] = useState<HubLinkInfo | null>(null)
  const [looking, setLooking] = useState(false)
  // A link that arrived in the address is looked up only after a click (opening a page must not make the workspace call out).
  const [armed, setArmed] = useState(!initialLink)
  const [problem, setProblem] = useState('')
  const [signed, setSigned] = useState(false)
  const [mode, setMode] = useState<'selective' | 'full'>('selective')
  const [choosing, setChoosing] = useState(false)
  const [noCopied, setNoCopied] = useState(false)
  const link = text.trim()
  const [detailsOpen, setDetailsOpen] = useState(false)
  const valid = LINK.test(link)
  // P12: the clipboard is read only on this click (a user gesture), checked with LINK, and only fills the field and starts
  // the lookup. Nothing is signed: the person still reads the hub and presses Connect.
  const canRead = typeof navigator !== 'undefined' && typeof navigator.clipboard?.readText === 'function'
  const useCopied = async () => {
    setNoCopied(false)
    try {
      const got = ((await navigator.clipboard.readText()) ?? '').trim()
      if (LINK.test(got)) { setText(got); setArmed(true) } else setNoCopied(true)
    } catch { setNoCopied(true) }
  }

  // The hub is looked up as soon as the pasted text is a link: nothing to click before the hub is shown.
  useEffect(() => {
    setInfo(null); setProblem('')
    if (!valid || !armed) return
    let alive = true
    setLooking(true)
    api.getHubLink(ws, link).then(
      got => { if (alive) setInfo(got) },
      e => { if (alive) setProblem(e instanceof ApiError && e.message ? e.message : 'Could not reach the hub named by this link.') },
    ).finally(() => { if (alive) setLooking(false) })
    return () => { alive = false }
  }, [ws, link, valid, armed])

  // the hub is named by the address the link points to (what is really dialled); its own name and id sit under Details
  const hubAt = (valid && link.replace(/^https?:\/\/([^/]+).*$/, '$1')) || info?.name || ''
  if (signed && info) return <Flow title="Connect a hub" onClose={leave} finish="here"><ConnectStage ws={ws} info={info} mode={mode} onClose={leave} /></Flow>

  return <Flow title="Connect a hub" onClose={onClose} finish="here">
    {!failed && <div className="space-y-1">
      <label htmlFor="hub-link-text" className="sr-only">Connect link</label>
      <p id="hub-link-hint" className="text-sm text-text-muted">Paste the connect link from your hub admin.</p>
      {canRead && !valid && <Action id="hub-link-use-copied" onClick={() => { void useCopied() }}>Use copied link</Action>}
      {noCopied && <p role="status" data-testid="hub-link-nocopied" className="text-sm text-text-muted">There is no connect link copied yet.</p>}
      <input id="hub-link-text" data-testid="hub-link-text" autoFocus value={text} onChange={e => { setText(e.target.value); setArmed(true) }} autoComplete="off" spellCheck={false} aria-describedby="hub-link-hint"
        className="min-h-11 w-full rounded-md border border-border bg-bg px-3 font-mono text-xs" />
    </div>}
    {link && !valid && <p role="alert" data-testid="hub-link-bad" className="text-danger">This does not look like a connect link. Paste all of it, from https:// to the end.</p>}
    {valid && !armed && <div className="space-y-1"><p data-testid="hub-link-from">This link points to <Raw>{link.replace(/^(https?:\/\/[^/]+).*$/, '$1')}</Raw></p><Action id="hub-link-lookup" onClick={() => setArmed(true)}>Look up this hub</Action></div>}
    {looking && <p role="status" data-testid="hub-link-looking">Looking up the hub…</p>}
    {problem && <p role="alert" data-testid="hub-link-problem" className="text-danger">{problem}</p>}
    {info && <>
      {!failed && <><div className="space-y-2 rounded-md border border-border p-3" data-testid="hub-link-hub">
        <p className="font-bold">Connect to the hub at <Raw>{hubAt}</Raw>?</p>
        {choosing && !signed
          ? <ModeChoice mode={mode} onChange={setMode} />
          : <div className={'rounded-md border p-2 ' + violet} data-testid="hub-mode-box">
            <p className="text-sm">{mode === 'full' ? 'The hub can read every ticket and file.' : 'The hub sees the tickets you pick, notes that need you, and this workspace\'s name.'}{!signed && <> <button type="button" data-testid="hub-mode-other" className="underline" onClick={() => setChoosing(true)}>Change</button></>}</p>
          </div>}
        <details className="text-sm text-text-muted" data-testid="hub-link-details" onToggle={e => setDetailsOpen(e.currentTarget.open)}>
          <summary className="cursor-pointer">Details</summary>
          {detailsOpen && <>
            <p>Hub <Raw>{info.name}</Raw></p>
            <p>Hub id: <Raw>{info.hub_id}</Raw></p>
            {initialLink && <p data-testid="hub-link-origin">at <Raw>{link.replace(/^(https?:\/\/[^/]+).*$/, '$1')}</Raw></p>}
          </>}
        </details>
      </div>
      {mode === 'full' && !info.owner_user && <p role="alert" data-testid="hub-link-old" className="text-warning">This link does not name you on the hub, so your agents could not work in this mode. Make a new connect link in the hub and paste that one.</p>}
      {mode === 'full' && <p className="text-warning">{caveat}</p>}</>}
      {oneClick !== 'on'
        ? <NoOneClick state={oneClick} what="Connecting" />
        : <HubStep key={link + mode} ws={ws} action={{ action: 'hub_join', hub_card: info.hub_card, hub_id: info.hub_id, mode, ...(info.owner_user ? { owner_user: info.owner_user } : {}) }} onClose={onClose} onFailedChange={setFailed} onChanged={() => setSigned(true)} />}
    </>}
    {(!info || oneClick !== 'on') && <div className="flex justify-end"><Action id="hub-join-cancel" onClick={onClose}>Cancel</Action></div>}
  </Flow>
}

/** The signed join is on the log; now the host tells the hub. The dialog says "Connected" and closes only once the hub
 *  has confirmed its first settings (until then the host keeps every add-on paused, and the page behind would fail), so
 *  nothing on the page is read before that. A hub that is slow gets plain words and one "Check again". */
function ConnectStage({ ws, info, mode, onClose }: { ws: string; info: HubLinkInfo; mode: 'selective' | 'full'; onClose: () => void }) {
  const refresh = useHubRefresh(ws)
  const qc = useQueryClient()
  const [state, setState] = useState<'idle' | 'working' | 'waiting' | 'failed'>('idle')
  const [why, setWhy] = useState('')
  const started = useRef(false)
  const done = () => {
    refresh()
    void qc.invalidateQueries() // the hub confirmed: the whole page behind the dialog is read once, now
    toast.success(mode === 'full' ? 'Connected. The hub can read everything in this workspace.' : 'Connected. The hub sees only what you choose to share, plus this workspace\'s name.')
    onClose()
  }
  const failure = (e: unknown) => { setWhy(e instanceof ApiError && e.message ? e.message : 'The hub did not answer.'); setState('failed') }
  const run = () => {
    setState('working'); setWhy('')
    api.connectHub(ws, { idem: newIdem(), invite: info.code, hub_card: info.hub_card, ...(info.endpoint ? { addr: info.endpoint } : {}) }).then(
      got => { if (got.confirmed === true) done(); else setState('waiting') },
      failure,
    )
  }
  const check = () => {
    setState('working'); setWhy('')
    api.refreshHub(ws).then(got => { if (got.confirmed === true) done(); else setState('waiting') }, failure)
  }
  useEffect(() => { if (!started.current) { started.current = true; run() } }, []) // eslint-disable-line react-hooks/exhaustive-deps
  return <div className="space-y-3" data-testid="hub-connect-stage">
    {(state === 'idle' || state === 'working') && <p role="status">Signed. Telling the hub… and waiting for it to confirm.</p>}
    {state === 'waiting' && <div role="status" className="space-y-2" data-testid="hub-connect-waiting">
      <p>Connected, but the hub has not confirmed yet. Your workspace is paused for a moment and nothing is lost.</p>
      <div className="flex flex-wrap gap-2"><Action id="hub-connect-close" onClick={onClose}>Close</Action><Action id="hub-connect-check" onClick={check}>Check again</Action></div>
    </div>}
    {state === 'failed' && <div role="alert" className="space-y-2" data-testid="hub-connect-failed">
      <p className="text-danger">Couldn't connect. Try again.</p>
      <InfoDrawer><p><Raw>{why}</Raw></p></InfoDrawer>
      <div className="flex flex-wrap gap-2"><Action id="hub-connect-close" onClick={onClose}>Cancel</Action><Action id="hub-connect-retry" onClick={run}>Try again</Action></div>
    </div>}
  </div>
}
