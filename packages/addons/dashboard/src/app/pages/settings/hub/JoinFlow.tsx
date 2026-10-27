// Run-9 feedback 6: connect a hub from a link. The person pastes the link the hub admin gave them; the host fetches the
// hub card and checks it against the link, and the page shows which hub it is. One click then signs the join (HubStep,
// with the backup key: the hub always requires the owner's proof), and the host tells the hub (hub.connect, with the
// link's code and address). Both modes use this review and one-click flow.
import { useEffect, useRef, useState } from 'react'
import { api } from '@/api/client'
import { ApiError } from '@/api/types'
import type { HubLinkInfo } from '@/api/hub'
import { newIdem } from '@/api/runs'
import { Raw } from '@/components/sign/visible'
import { HubStep } from '../host/HubStep'
import { Action, Flow, NoOneClick, caveat, useHubRefresh, useOneClick } from './shared'
import { ModeChoice } from './ModeChoice'

const LINK = /^https?:\/\/\S+\/c#h1\.[0-9a-f]{32}\.[A-Za-z0-9_-]{1,64}(\.[^\s~]+)?(~[0-9a-f]{32})?$/

export function JoinFlow({ ws, onClose }: { ws: string; onClose: () => void }) {
  const oneClick = useOneClick(ws)
  const refresh = useHubRefresh(ws)
  const [failed, setFailed] = useState(false)
  const [text, setText] = useState('')
  const [info, setInfo] = useState<HubLinkInfo | null>(null)
  const [looking, setLooking] = useState(false)
  const [problem, setProblem] = useState('')
  const [signed, setSigned] = useState(false)
  const [mode, setMode] = useState<'selective' | 'full'>('selective')
  const link = text.trim()
  const valid = LINK.test(link)

  // The hub is looked up as soon as the pasted text is a link: nothing to click before the hub is shown.
  useEffect(() => {
    setInfo(null); setProblem('')
    if (!valid) return
    let alive = true
    setLooking(true)
    api.getHubLink(ws, link).then(
      got => { if (alive) setInfo(got) },
      e => { if (alive) setProblem(e instanceof ApiError && e.message ? e.code + ': ' + e.message : 'Could not reach the hub named by this link.') },
    ).finally(() => { if (alive) setLooking(false) })
    return () => { alive = false }
  }, [ws, link, valid])

  if (signed && info) return <Flow title="Connect a hub" onClose={onClose} finish="here"><ConnectStage ws={ws} info={info} mode={mode} onClose={onClose} /></Flow>

  return <Flow title="Connect a hub" onClose={onClose} finish="here">
    {!failed && <div className="space-y-1">
      <label htmlFor="hub-link-text" className="block text-sm font-medium">Connect link (from your hub admin)</label>
      <input id="hub-link-text" data-testid="hub-link-text" value={text} onChange={e => setText(e.target.value)} autoComplete="off" spellCheck={false} aria-describedby="hub-link-hint"
        placeholder="https://hub.example/c#h1...." className="min-h-11 w-full rounded-md border border-border bg-bg px-3 font-mono text-xs" />
      <p id="hub-link-hint" className="text-xs text-text-muted">Paste the link the hub admin copied from the hub console. It works once, for 30 minutes.</p>
    </div>}
    {link && !valid && <p role="alert" data-testid="hub-link-bad" className="text-danger">This does not look like a connect link. Paste all of it, from https:// to the end.</p>}
    {looking && <p role="status" data-testid="hub-link-looking">Looking up the hub…</p>}
    {problem && <p role="alert" data-testid="hub-link-problem" className="text-danger">{problem}</p>}
    {info && <>
      {!failed && <><div className="rounded-md border border-border p-3" data-testid="hub-link-hub">
        <p className="font-bold">Hub <Raw>{info.name}</Raw></p>
      </div>
      <details><summary>Technical details</summary><p>Hub id: <Raw>{info.hub_id}</Raw></p></details>
      {!signed && <ModeChoice mode={mode} onChange={setMode} />}
      <p>{mode === 'full' ? 'The hub will read everything.' : 'You choose what to share.'}</p>
      <p className="text-warning">{caveat}</p></>}
      {oneClick !== 'on'
        ? <NoOneClick state={oneClick} what="Connecting" />
        : <HubStep key={link + mode} ws={ws} action={{ action: 'hub_join', hub_card: info.hub_card, hub_id: info.hub_id, mode, ...(info.owner_user ? { owner_user: info.owner_user } : {}) }} onClose={onClose} onFailedChange={setFailed} onChanged={() => { refresh(); setSigned(true) }} />}
    </>}
    {(!info || oneClick !== 'on') && <div className="flex justify-end"><Action id="hub-join-cancel" onClick={onClose}>Cancel</Action></div>}
  </Flow>
}

/** The signed join is on the log; now the host tells the hub. "Connected" only when that worked. */
function ConnectStage({ ws, info, mode, onClose }: { ws: string; info: HubLinkInfo; mode: 'selective' | 'full'; onClose: () => void }) {
  const refresh = useHubRefresh(ws)
  const [state, setState] = useState<'idle' | 'working' | 'done' | 'failed'>('idle')
  const [why, setWhy] = useState('')
  const started = useRef(false)
  const run = () => {
    setState('working'); setWhy('')
    api.connectHub(ws, { idem: newIdem(), invite: info.code, hub_card: info.hub_card, ...(info.endpoint ? { addr: info.endpoint } : {}) }).then(
      () => { setState('done'); refresh() },
      e => { setWhy(e instanceof ApiError && e.message ? e.code + ': ' + e.message : 'The hub did not answer.'); setState('failed') },
    )
  }
  useEffect(() => { if (!started.current) { started.current = true; run() } }, []) // eslint-disable-line react-hooks/exhaustive-deps
  return <div className="space-y-3" data-testid="hub-connect-stage">
    {state === 'done' && <><p role="status" data-testid="hub-connected">Connected. {mode === 'full' ? 'The hub can read everything in this workspace.' : 'The hub sees only what you choose to share, plus notes that need your attention and full questions.'}</p><div className="flex justify-end"><Action id="hub-connect-close" onClick={onClose}>Close</Action></div></>}
    {(state === 'idle' || state === 'working') && <p role="status">Signed. Telling the hub…</p>}
    {state === 'failed' && <div role="alert" className="space-y-2" data-testid="hub-connect-failed">
      <p className="text-danger">Couldn't connect. Try again.</p>
      <details><summary>Technical details</summary><p><Raw>{why}</Raw></p></details>
      <div className="flex flex-wrap gap-2"><Action id="hub-connect-close" onClick={onClose}>Cancel</Action><Action id="hub-connect-retry" onClick={run}>Try again</Action></div>
    </div>}
  </div>
}
