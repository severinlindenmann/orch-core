// Run-9 feedback 6: connect a hub from a link. The person pastes the link the hub admin gave them; the host fetches the
// hub card and checks it against the link, and the page shows which hub it is. One click then signs the join (HubStep,
// with the backup key: the hub always requires the owner's proof), and the host tells the hub (hub.connect, with the
// link's code and address). Both modes use this review and one-click flow.
import { JoinShareOffer } from './SendShares'
import { useEffect, useRef, useState } from 'react'
import { toast } from 'sonner'
import { useQueryClient } from '@tanstack/react-query'
import { InfoDrawer } from '@/components/InfoDrawer'
import { api } from '@/api/client'
import { ApiError } from '@/api/types'
import { startJoinPolling, type HubLinkInfo, type HubMode, type HubStatus } from '@/api/hub'
import { newIdem } from '@/api/runs'
import { Raw } from '@/components/sign/visible'
import { HubStep } from '../host/HubStep'
import { Action, Flow, NoOneClick, caveat, useHubRefresh, useOneClick } from './shared'
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

/** The host a connect link dials, as shown to the person: no scheme, the port only when it is not 443. '' for a link that does not parse. */
export function hostOf(link: string): string {
  try {
    const u = new URL(link)
    const port = u.port || (u.protocol === 'https:' ? '443' : '80')
    return port === '443' ? u.hostname : `${u.hostname}:${port}`
  } catch { return '' }
}

/** True when a hub "name" is just the address (compared lower-cased, scheme and a :443 port dropped). */
function sameHost(name: string, host: string): boolean {
  const n = (x: string) => x.toLowerCase().replace(/^https?:\/\//, '').replace(/\/.*$/, '').replace(/:443$/, '')
  return n(name) === n(host)
}

export function JoinFlow({ ws, onClose, initialLink }: { ws: string; onClose: () => void; initialLink?: string }) {
  const refreshHub = useHubRefresh(ws)
  // Closing after the signature (waiting, failed or confirmed) reads the hub status again: allowed while paused.
  const leave = () => { refreshHub(); onClose() }
  const oneClick = useOneClick(ws)
  const [failed, setFailed] = useState(false)
  const [text, setText] = useState(initialLink ?? '')
  // The hub card that came back, with the exact link it was looked up for: what is shown (name, address) and what is signed
  // always belong to that link; a changed field shows nothing until the new link is looked up.
  const [found, setFound] = useState<{ link: string; info: HubLinkInfo } | null>(null)
  const [looking, setLooking] = useState(false)
  // A link that arrived in the address is looked up only after a click (opening a page must not make the workspace call out).
  const [armed, setArmed] = useState(!initialLink)
  const [problem, setProblem] = useState('')
  const [signed, setSigned] = useState(false)
  const [mode, setMode] = useState<'selective' | 'full'>('selective')
  const [noCopied, setNoCopied] = useState(false)
  const link = text.trim()
  const info = found && found.link === link ? found.info : null
  const textRef = useRef(text)
  textRef.current = text
  const [detailsOpen, setDetailsOpen] = useState(false)
  const valid = LINK.test(link)
  // P12: the clipboard is read only on this click (a user gesture), checked with LINK, and only fills the field and starts
  // the lookup. Nothing is signed: the person still reads the hub and presses Connect.
  const canRead = typeof navigator !== 'undefined' && typeof navigator.clipboard?.readText === 'function'
  // `auto`: no click was made (permission was already granted): the link only fills the field and names its host; the lookup
  // (an outbound call) waits for "Look up this hub", exactly like a link that came in the address.
  const useCopied = async (auto = false) => {
    setNoCopied(false)
    try {
      const got = ((await navigator.clipboard.readText()) ?? '').trim()
      if (auto && textRef.current.trim()) return // the person typed or pasted meanwhile: never replace it
      if (LINK.test(got)) { setText(got); setArmed(!auto) } else if (!auto) setNoCopied(true)
    } catch { if (!auto) setNoCopied(true) }
  }

  // Run 15 P04: when the browser already lets this page read the clipboard, a copied connect link is picked up at once (same
  // LINK check, same lookup as the button; nothing is signed). Otherwise the button is the one big step. A link that came in
  // the address is never replaced.
  useEffect(() => {
    if (initialLink || !canRead) return
    let alive = true
    const perms = typeof navigator !== 'undefined' ? navigator.permissions : undefined
    if (!perms?.query) return
    perms.query({ name: 'clipboard-read' as PermissionName }).then(
      st => { if (alive && st.state === 'granted') void useCopied(true) },
      () => {},
    )
    return () => { alive = false }
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  // The hub is looked up as soon as the pasted text is a link: nothing to click before the hub is shown.
  useEffect(() => {
    setFound(null); setProblem('')
    if (!valid || !armed) return
    let alive = true
    setLooking(true)
    api.getHubLink(ws, link).then(
      got => { if (alive) setFound({ link, info: got }) },
      e => { if (alive) setProblem(e instanceof ApiError && e.message ? e.message : 'Could not reach the hub named by this link.') },
    ).finally(() => { if (alive) setLooking(false) })
    return () => { alive = false }
  }, [ws, link, valid, armed])

  // P08: the hub is named as its card names itself; the address that was really looked up and is dialled sits under Details.
  // The consent line names the verified host (from the link that was looked up, no scheme, port only when it is not 443):
  // the hub's own name is self-declared, so it never stands alone in the question the owner signs under.
  const hubHost = found ? hostOf(found.link) : ''
  const hubAt = found ? found.link.replace(/^(https?:\/\/[^/]+).*$/, '$1') : ''
  // The host's card has no display name: `name` is the link's address part. A name is shown only when it says more than the host.
  const rawName = info?.name?.trim() || ''
  const hubName = rawName && !sameHost(rawName, hubHost) ? rawName : ''
  if (signed && info) return <Flow title="Connect a hub" onClose={leave} finish="here"><ConnectStage ws={ws} info={info} mode={mode} onClose={leave} /></Flow>

  return <Flow title="Connect a hub" onClose={onClose} finish="here">
    {!failed && <div className="space-y-2">
      <div data-testid="hub-link-paste" className="space-y-1">
        <label htmlFor="hub-link-text" className="block text-sm text-text-muted">Paste the connect link from your hub admin</label>
        <div className="flex gap-2">
          <input id="hub-link-text" data-testid="hub-link-text" autoFocus value={text} onChange={e => { setText(e.target.value); setArmed(true) }} autoComplete="off" spellCheck={false}
            className="min-h-11 min-w-0 flex-1 rounded-md border border-border bg-bg px-3 font-mono text-xs" />
          {canRead && <Action id="hub-link-use-copied" onClick={() => { void useCopied() }}>Paste from clipboard</Action>}
        </div>
        {noCopied && <p role="status" data-testid="hub-link-nocopied" className="text-sm text-text-muted">Copy the link in the hub first.</p>}
      </div>
      {info && valid && <p role="status" data-testid="hub-link-found" className="font-medium text-success">Link found: <Raw>{hubName || hubHost}</Raw></p>}
    </div>}
    {link && !valid && <p role="alert" data-testid="hub-link-bad" className="text-danger">This does not look like a connect link. Paste all of it, from https:// to the end.</p>}
    {valid && !armed && <div className="space-y-1"><p data-testid="hub-link-from">Link found. This link points to <Raw>{link.replace(/^(https?:\/\/[^/]+).*$/, '$1')}</Raw></p><Action id="hub-link-lookup" onClick={() => setArmed(true)}>Look up this hub</Action></div>}
    {looking && <p role="status" data-testid="hub-link-looking">Looking up the hub…</p>}
    {problem && <p role="alert" data-testid="hub-link-problem" className="text-danger">{problem}</p>}
    {info && <>
      {!failed && <><div className="space-y-2 rounded-md border border-border p-3" data-testid="hub-link-hub">
        <p className="font-bold" data-testid="hub-link-question">{hubName ? <>Connect to <Raw>{hubName}</Raw> at <Raw>{hubHost}</Raw>?</> : <>Connect to <Raw>{hubHost}</Raw>?</>}</p>
        <ModeChoice mode={mode} onChange={setMode} />
        {mode === 'selective' && <p className="text-[13px] text-text-muted" data-testid="hub-link-later">Leaving later does not take back what the hub already read.</p>}
        <details className="text-sm text-text-muted" data-testid="hub-link-details" onToggle={e => setDetailsOpen(e.currentTarget.open)}>
          <summary className="cursor-pointer">Details</summary>
          {detailsOpen && <>
            <p>Address: <Raw>{hubAt}</Raw></p>
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
  const [offer, setOffer] = useState(false)
  const [why, setWhy] = useState('')
  const started = useRef(false)
  const done = (confirmedMode?: HubMode | null) => {
    // The hub confirmed: write what is known now (a status read that started earlier may still land with the old answer),
    // then let the real answer replace it. Only ever called when confirmed === true.
    const m = confirmedMode === 'selective' || confirmedMode === 'full' ? confirmedMode : mode
    const key = ['hub', ws, 'status']
    void qc.cancelQueries({ queryKey: key })
    qc.setQueryData<HubStatus>(key, old => old ? { ...old, joined: true, mode: m } : old)
    startJoinPolling(ws)
    refresh()
    void qc.invalidateQueries() // the hub confirmed: the whole page behind the dialog is read once, now
    toast.success(mode === 'full' ? 'Connected. The hub can read everything in this workspace.' : 'Connected. The hub sees only what you choose to share, plus this workspace\'s name.')
    void api.getHubShare(ws).then(share => {
      if ((share.not_sent_count ?? 0) > 0) setOffer(true)
      else onClose()
    }, onClose)
  }
  const failure = (e: unknown) => { setWhy(e instanceof ApiError && e.message ? e.message : 'The hub did not answer.'); setState('failed') }
  const run = () => {
    setState('working'); setWhy('')
    api.connectHub(ws, { idem: newIdem(), invite: info.code, hub_card: info.hub_card, ...(info.endpoint ? { addr: info.endpoint } : {}) }).then(
      got => { if (got.confirmed === true) done(got.mode); else setState('waiting') },
      failure,
    )
  }
  const check = () => {
    setState('working'); setWhy('')
    api.refreshHub(ws).then(got => { if (got.confirmed === true) done(); else setState('waiting') }, failure)
  }
  useEffect(() => { if (!started.current) { started.current = true; run() } }, []) // eslint-disable-line react-hooks/exhaustive-deps
  if (offer) return <JoinShareOffer ws={ws} onClose={onClose} />
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
