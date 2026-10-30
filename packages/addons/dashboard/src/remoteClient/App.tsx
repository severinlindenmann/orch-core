import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError, type Api, type EnrollStatus, type Prompt, type Row, type State } from './api'

type Screen = { name: 'list' } | { name: 'add' }

const NEXT: Record<string, string> = {
  'remote.off': 'Ask the owner to turn it on.',
  'remote.device': 'Add this computer again.',
  'remote.no_code': 'Ask the owner for a new code.',
  not_allowed: 'Check that it is online and shared with you.',
  no_session: 'Connect to your hub again.',
  malformed: 'Try again.',
  handshake: 'Try again.',
  'remote_client.cancelled': 'Try again when you are ready.',
}
const nextStep = (code: string) => NEXT[code] ?? ''
const errText = (e: unknown) => (e instanceof ApiError ? [e.message, nextStep(e.code)].filter(Boolean).join(' ') : 'Something went wrong. Try again.')

function Shell({ children }: { children: React.ReactNode }) {
  return (
    <>
      <div className="top">orch remote</div>
      <main className="card">{children}</main>
    </>
  )
}

/** The page. `api` is null when the launch code did not work (the page was opened by hand or the code is used up). */
export function App({ api, pollMs = 1500 }: { api: Api | null; pollMs?: number }) {
  const [state, setState] = useState<State | null>(null)
  const [screen, setScreen] = useState<Screen>({ name: 'list' })
  const [failed, setFailed] = useState(false)

  const refresh = useCallback(async () => {
    if (!api) return
    try { setState(await api.state()); setFailed(false) } catch { setFailed(true) }
  }, [api])

  useEffect(() => {
    if (!api) return
    let stop = false
    let timer: ReturnType<typeof setTimeout>
    const tick = async () => {
      await refresh()
      if (!stop) timer = setTimeout(tick, pollMs)
    }
    void tick()
    return () => { stop = true; clearTimeout(timer) }
  }, [api, refresh, pollMs])

  if (!api) return <Shell><h1>Open this page from orch</h1><p>Run <b>orch remote open</b> to get a new link.</p></Shell>
  if (!state) return <Shell><p>{failed ? 'The client is not running. Start it again with orch remote open.' : 'Loading…'}</p></Shell>
  if (state.pin) return <Shell><PinScreen key={state.pin} api={api} id={state.pin} onDone={refresh} /></Shell>
  if (state.prompt) return <Shell><Confirm key={state.prompt.id} api={api} prompt={state.prompt} method={state.presence.method} onDone={refresh} /></Shell>
  if (!state.signed_in) return <Shell><Connect api={api} onDone={refresh} /></Shell>
  if (screen.name === 'add') return <Shell><Add api={api} onBack={() => setScreen({ name: 'list' })} pollMs={Math.min(pollMs, 700)} /></Shell>
  return <Shell><Workspaces api={api} state={state} onAdd={() => setScreen({ name: 'add' })} onChange={refresh} /></Shell>
}

function Connect({ api, onDone }: { api: Api; onDone: () => void }) {
  const [f, setF] = useState({ link: '', user: '', password: '', code: '' })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const set = (k: keyof typeof f) => (e: React.ChangeEvent<HTMLInputElement>) => setF({ ...f, [k]: e.target.value })
  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    setBusy(true); setError('')
    try { await api.connect(f); onDone() } catch (x) { setError(x instanceof ApiError ? x.message : 'Something went wrong. Try again.') } finally { setBusy(false) }
  }
  return (
    <form onSubmit={submit}>
      <h1>Connect to your hub</h1>
      <p>You need to be on the office network for this first step.</p>
      <label htmlFor="rc-link">Connect link</label>
      <input id="rc-link" value={f.link} onChange={set('link')} placeholder="Paste the link from your hub admin" autoComplete="off" required />
      <label htmlFor="rc-user">User</label>
      <input id="rc-user" value={f.user} onChange={set('user')} autoComplete="username" required />
      <label htmlFor="rc-pass">Password</label>
      <input id="rc-pass" type="password" value={f.password} onChange={set('password')} autoComplete="current-password" required />
      <label htmlFor="rc-code">6-digit code</label>
      <input id="rc-code" value={f.code} onChange={set('code')} inputMode="numeric" autoComplete="one-time-code" required />
      {error && <div className="err" role="alert">{error}</div>}
      <button className="btn full" disabled={busy}>{busy ? 'Connecting…' : 'Connect'}</button>
    </form>
  )
}

function Workspaces({ api, state, onAdd, onChange }: { api: Api; state: State; onAdd: () => void; onChange: () => void }) {
  const [rows, setRows] = useState<Row[] | null>(null)
  const [errors, setErrors] = useState<Record<string, string>>({})
  const [busy, setBusy] = useState<string | null>(null)
  const [listError, setListError] = useState('')
  const load = useCallback(async () => {
    try { setRows(await api.workspaces()); setListError('') } catch (e) { setListError(errText(e)); onChange() }
  }, [api, onChange])
  useEffect(() => { void load() }, [load, state.open.length])
  const open = async (ws: string) => {
    setBusy(ws); setErrors((x) => ({ ...x, [ws]: '' }))
    try { await api.open(ws); await load() } catch (e) { setErrors((x) => ({ ...x, [ws]: errText(e) })) } finally { setBusy(null) }
  }
  const end = async (ws: string) => { await api.end(ws).catch(() => null); await load(); onChange() }
  return (
    <>
      <h1>Workspaces</h1>
      <p>Shared with you by your company.</p>
      {listError && <div className="err" role="alert">{listError}</div>}
      {rows && rows.length === 0 && <div className="list"><div className="row"><p>Nothing here yet. Ask a workspace owner to share one with you.</p></div></div>}
      {rows && rows.length > 0 && (
        <div className="list">
          {rows.map((r) => (
            <div className="row" key={r.workspace_id}>
              <div className="line">
                <span className={`dot${r.online ? ' on' : ''}`} aria-hidden />
                <span className="grow">{r.name}{r.name_from_hub && <span className="hint">name from the hub</span>}</span>
                {r.added
                  ? <>
                      {r.open && <button className="link" onClick={() => void end(r.workspace_id)}>End</button>}
                      <button className="btn sm" disabled={!r.online || busy === r.workspace_id} title={r.online ? '' : 'Offline'} onClick={() => void open(r.workspace_id)}>
                        {busy === r.workspace_id ? 'Opening…' : 'Open'}
                      </button>
                    </>
                  : <button className="btn sm sec" onClick={onAdd}>Add this computer</button>}
              </div>
              {(errors[r.workspace_id] || r.ended) && <div className="err" role="alert">{errors[r.workspace_id] || r.ended}</div>}
            </div>
          ))}
        </div>
      )}
      <details>
        <summary>Technical details</summary>
        <dl>
          <dt>This computer</dt><dd>{state.computer}</dd>
          <dt>Hub</dt><dd>{state.hub?.address ?? ''}</dd>
          <dt>Hub id</dt><dd>{state.hub?.id ?? ''}</dd>
          <dt>{state.presence.method === 'key' ? 'Security key' : 'Tied to this Mac'}</dt><dd>{state.presence.method === 'key' ? 'In use' : state.presence.tied ? 'Yes' : 'No'}</dd>
        </dl>
      </details>
    </>
  )
}

function Add({ api, onBack, pollMs }: { api: Api; onBack: () => void; pollMs: number }) {
  const [job, setJob] = useState<{ id: string; workspace_id: string } | null>(null)
  const [status, setStatus] = useState<EnrollStatus | null>(null)
  const [error, setError] = useState('')
  const [typed, setTyped] = useState('')
  const [names, setNames] = useState<Record<string, string>>({})
  const alive = useRef(true)
  useEffect(() => { alive.current = true; return () => { alive.current = false } }, [])
  useEffect(() => { api.workspaces().then((r) => setNames(Object.fromEntries(r.map((x) => [x.workspace_id, x.name])))).catch(() => null) }, [api])
  useEffect(() => {
    if (!job || (status && ['added', 'error', 'stopped'].includes(status.status))) return
    const t = setTimeout(async () => {
      try { const s = await api.enrollStatus(job.id); if (alive.current) setStatus(s) } catch (e) { if (alive.current) { setError(errText(e)); setJob(null) } }
    }, status ? pollMs : 0)
    return () => clearTimeout(t)
  }, [api, job, status, pollMs])

  const start = async (code: string) => {
    setError(''); setStatus(null)
    try { setJob(await api.enrollStart(code)) } catch (e) { setError(errText(e)) }
  }
  const paste = async () => {
    try { await start((await navigator.clipboard.readText()).trim()) } catch { setError('Could not read the clipboard. Paste the code into the box instead.') }
  }
  const stop = async () => { if (job) await api.enrollStop(job.id).catch(() => null); setJob(null); setStatus(null) }
  const name = job ? names[job.workspace_id] ?? 'the workspace' : ''

  if (job && status?.status === 'added') {
    return (
      <div className="center">
        <div className="big" aria-hidden>✓</div>
        <h1>Added</h1>
        <p>This computer can now open {name}.</p>
        <button className="btn" onClick={onBack}>Open workspace</button>
      </div>
    )
  }
  if (job && !error && status?.status !== 'error' && status?.status !== 'stopped') {
    const words = status?.words ?? []
    return (
      <>
        <h1>Read these six words to the owner of {name}</h1>
        <p>They must see the same words at the workspace.</p>
        {words.length === 6
          ? <ol className="words" aria-label="Six words">{words.map((w, i) => <li key={i}><small>{i + 1}</small>{w}</li>)}</ol>
          : <p><span className="spin" />Getting your words…</p>}
        {words.length === 6 && <p><span className="spin" />Waiting for the owner to confirm…</p>}
        <button className="btn sec" onClick={() => void stop()}>Stop</button>
      </>
    )
  }
  const shown = error || (status?.status === 'error' ? status.message : '')
  return (
    <>
      <button className="link back" onClick={onBack}>← Workspaces</button>
      <h1>Add this computer</h1>
      <p>Copy the code you got from the workspace owner, then press Paste code.</p>
      {shown && <div className="err" role="alert">{shown}</div>}
      <button className="btn full" onClick={() => void paste()}>Paste code</button>
      <label htmlFor="rc-code-box">Or paste it here</label>
      <input id="rc-code-box" value={typed} onChange={(e) => setTyped(e.target.value)} placeholder="Paste the code here" autoComplete="off" />
      <button className="btn sec full" disabled={!typed.trim()} onClick={() => void start(typed.trim())}>Add</button>
    </>
  )
}

function Confirm({ api, prompt, method, onDone }: { api: Api; prompt: Prompt; method: 'touch_id' | 'key'; onDone: () => void }) {
  const [sent, setSent] = useState(false)
  const answer = async (approve: boolean) => {
    setSent(true)
    try { await api.answer(prompt.id, approve) } finally { onDone() }
  }
  return (
    <>
      <h2>{prompt.workspace} asks you to confirm</h2>
      <ul className="acts">{prompt.lines.map((l, i) => <li key={i}>{l}</li>)}</ul>
      <p className="muted">This list comes straight from {prompt.workspace}, not from the hub.</p>
      {sent
        ? <p><span className="spin" />{method === 'key' ? 'Touch your key…' : 'Waiting for Touch ID…'}</p>
        : <div className="gap">
            <button className="btn sec" onClick={() => void answer(false)}>Cancel</button>
            <button className="btn" onClick={() => void answer(true)}>{method === 'key' ? 'Confirm with your key' : 'Confirm with Touch ID'}</button>
          </div>}
    </>
  )
}

/** Shown only when the security key asks for its PIN (a key with a PIN, not Touch ID). It is the key's own secret, not a
 * confirm word: it is sent to this client only, kept in memory until the key has used it, never stored. */
function PinScreen({ api, id, onDone }: { api: Api; id: string; onDone: () => void }) {
  const [pin, setPin] = useState('')
  const [sent, setSent] = useState(false)
  const send = async (value: string | null) => {
    setSent(true)
    try { await api.pin(id, value) } finally { onDone() }
  }
  return (
    <form onSubmit={(e) => { e.preventDefault(); if (pin) void send(pin) }}>
      <h2>Enter your security key's PIN</h2>
      <p className="muted">Your key asks for it. It is used once and not saved.</p>
      <label htmlFor="rc-pin">PIN</label>
      <input id="rc-pin" type="password" value={pin} onChange={(e) => setPin(e.target.value)} autoComplete="off" autoFocus disabled={sent} />
      {sent
        ? <p><span className="spin" />Touch your key…</p>
        : <div className="gap">
            <button type="button" className="btn sec" onClick={() => void send(null)}>Cancel</button>
            <button className="btn" disabled={!pin}>Continue</button>
          </div>}
    </form>
  )
}
