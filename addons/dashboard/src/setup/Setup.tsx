import { useEffect, useRef, useState } from 'react'
import { createRoot } from 'react-dom/client'
import {
  SetupAPI,
  SetupError,
  bootstrapSetup,
  type SetupState,
  type Profile,
  type Secrets,
  type Review,
  type Progress,
} from '../api/setup'
import { Inline, Raw } from '../components/sign/visible'
import './setup.css'

const voidCopy =
  'If setup fails before your workspace is created, these secret words and recovery code will no longer work. Run orch setup again to get new ones.'
const expiredCopy =
  'This session has ended. Run orch setup again to start a new session with new secret words and a new recovery code.'
/** A short, secret-free cause for the "session ended" copy: the API code, or `client: Name: message`. */
export function reasonOf(e: unknown): string {
  if (e instanceof SetupError) return e.code
  const name = e instanceof Error ? e.name : 'Error'
  const message = e instanceof Error ? e.message : String(e)
  return `client: ${name}: ${message.split('#')[0].replace(/[\u0000-\u001f\u007f]+/g, ' ').slice(0, 120)}`
}
function report(e: unknown): string {
  const reason = reasonOf(e)
  console.error('[orch setup]', reason)
  return reason
}
function clearClipboard() {
  void navigator.clipboard.writeText('').catch(() => {})
}
/** Hide the lines that carry no information for a person: blanks and values the host fills in at signing. */
const shown = (line: string) => line.trim() !== '' && !/:\s*set at signing$/.test(line)
function duration(hours: number): string {
  if (hours > 8 && hours % 24 === 0) return `${hours / 24} ${hours === 24 ? 'day' : 'days'}`
  return `${hours} hours`
}
const steps = ['Welcome', 'You', 'Agents may', 'Approval', 'Secret words', 'Review']
export function Setup({ api }: { api: SetupAPI }) {
  const [state, setState] = useState<SetupState>()
  const [profile, setProfile] = useState<Profile>({
    name: '',
    prefix: '',
    addons: [],
    method: 'terminal',
    agent_hours: 8,
    agent_grant: false,
    relay: false,
  })
  const [step, setStep] = useState(0)
  const [secrets, setSecrets] = useState<Secrets | null>(null)
  const [words, setWords] = useState<Record<string, string>>({})
  const [review, setReview] = useState<Review>()
  const [typed, setTyped] = useState('')
  const [run, setRun] = useState<string>()
  const [progress, setProgress] = useState<Progress>()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [ended, setEnded] = useState(false)
  const [saved, setSaved] = useState(false)
  const [notice, setNotice] = useState('')
  const [reason, setReason] = useState('')
  const heading = useRef<HTMLHeadingElement>(null)
  const lock = useRef(false)
  const confirmed = useRef(false)
  const secretRef = useRef<Secrets | null>(null)
  secretRef.current = secrets
  const drop = () => {
    if (secretRef.current) {
      secretRef.current.passphrase = null
      secretRef.current.recovery_words.fill('')
      secretRef.current.check_positions.length = 0
      secretRef.current = null
    }
    setSecrets(null)
    setWords({})
    setSaved(false)
  }
  async function act(fn: () => Promise<void>) {
    if (lock.current) return
    lock.current = true
    setBusy(true)
    setError('')
    try {
      await fn()
    } catch (e) {
      const code = e instanceof SetupError ? e.code : 'setup.unavailable'
      if (!(e instanceof SetupError) || code !== 'invalid.input') setReason(report(e))
      if (
        [
          'session.required',
          'session.expired',
          'secrets.shown',
          'secrets.gone',
          'setup.ended',
          'setup.token',
          'setup.code_invalid',
        ].includes(code)
      ) {
        drop()
        setEnded(true)
        api.forget()
        setError(expiredCopy)
      } else
        setError(
          code === 'invalid.input'
            ? 'Check your name and short name. Use 2–8 capital letters or digits for the short name, starting with a letter. Core will also check reserved names.'
            : code === 'review.changed'
              ? 'Your choices changed. Review them again before creating your workspace.'
              : 'We could not complete this step. Check your terminal, then try again.',
        )
      if (code === 'invalid.input') setStep(1)
      if (code === 'review.changed') {
        setReview(undefined)
        setStep(3)
      }
    } finally {
      lock.current = false
      setBusy(false)
    }
  }
  useEffect(() => {
    let active = true
    api
      .call<SetupState>('state')
      .then(
        (s) => ({ s }),
        (e: unknown) => ({ e }),
      )
      .then((r) => {
        if (!active) return
        if ('e' in r) {
          setReason(report(r.e))
          setEnded(true)
          setError(expiredCopy)
          return
        }
        const s = r.s
        try {
          const addons = s.addons.filter((a) => a.required || a.default).map((a) => a.id)
          setState(s)
          setProfile((p) => ({ ...p, ...s.suggested, addons }))
          if (s.error || s.phase !== 'profile') {
            setEnded(true)
            if (!s.error) setError('Return to the terminal to resume setup safely. Run orch setup again if this page was reloaded.')
          }
        } catch (e) {
          setReason(report(e))
          setEnded(true)
          setError(expiredCopy)
        }
      })
    return () => {
      active = false
    }
  }, [api])
  useEffect(() => {
    heading.current?.focus()
  }, [step, run, ended])
  useEffect(() => {
    if (!run) return
    let active = true
    let timer: ReturnType<typeof setTimeout>
    const poll = async () => {
      try {
        const next = await api.call<Progress>(`progress?run_id=${encodeURIComponent(run)}`)
        if (!active) return
        setProgress(next)
        setError('')
        if (!next.done && !next.error) timer = setTimeout(() => void poll(), 1000)
      } catch {
        if (active) {
          setError('Connection interrupted. Check your terminal. We will keep checking your progress.')
          timer = setTimeout(() => void poll(), 2500)
        }
      }
    }
    void poll()
    return () => {
      active = false
      clearTimeout(timer)
    }
  }, [api, run])
  useEffect(() => {
    const hide = () => {
      drop()
      api.forget()
    }
    window.addEventListener('pagehide', hide)
    return () => {
      window.removeEventListener('pagehide', hide)
      drop()
    }
  }, [api])
  const update = (value: Partial<Profile>) => {
    setProfile((p) => ({ ...p, ...value }))
    setReview(undefined)
    setTyped('')
  }
  async function getReview() {
    setReview(await api.call<Review>('review', {}))
    setStep(5)
  }
  async function approveProfile() {
    await api.call('profile', profile)
    if (state?.mode === 'finish' || confirmed.current) await getReview()
    else {
      const revealed = await api.call<Secrets>('secrets/reveal', {})
      if (profile.method === 'click') revealed.passphrase = null
      setSecrets(revealed)
      setStep(4)
    }
  }
  async function confirm() {
    const result = await api.call<{ ok: boolean; attempts_left?: number }>('secrets/confirm', { words })
    Object.keys(words).forEach((key) => {
      words[key] = ''
    })
    setWords({})
    if (result.ok) {
      confirmed.current = true
      drop()
      await getReview()
    } else if (result.attempts_left === 0) {
      drop()
      setEnded(true)
      api.forget()
      setError(expiredCopy)
    } else setError(`Those words did not match. ${result.attempts_left} attempts left.`)
  }
  async function copyRecovery() {
    try {
      await navigator.clipboard.writeText(secrets!.recovery_words.join(' '))
      setNotice('Recovery code copied. We will try to clear your clipboard in 60 seconds.')
      // Do not retain the secret in a timer closure, or read unrelated clipboard contents.
      setTimeout(clearClipboard, 60_000)
    } catch {
      setNotice('Copy is unavailable. Write down the recovery code or print it.')
    }
  }
  async function finish() {
    const result = await api.call<{ url: string }>('finish', {})
    const url = new URL(result.url, location.origin)
    if (url.origin !== location.origin || !url.pathname.startsWith('/')) throw new Error('destination')
    drop()
    api.forget()
    location.assign(url.pathname + url.search + url.hash)
  }
  const terminalReviewMissing =
    !!review &&
    (((state?.mode === 'finish' || !!review.runner_grant) && !review.tty_confirm) ||
      (review.tty_confirm && review.fingerprint?.length !== 3))
  const addonTitles = (review?.items ?? []).filter((i) => i.id.startsWith('addon:')).map((i) => i.title)
  const next = (n: number) => (
    <button className="primary" onClick={() => setStep(n)}>
      Continue →
    </button>
  )
  const title = ended
    ? 'Setup paused'
    : run
      ? progress?.done
        ? 'Your workspace is ready'
        : progress?.error
          ? 'Setup needs your attention'
          : review?.tty_confirm && !progress?.steps.some((s) => s.status === 'doing' || s.status === 'done')
            ? 'Confirm in your terminal'
            : 'Setting up your workspace'
      : step === 0
        ? `${state?.mode === 'finish' ? 'Finish setup' : 'Set up orch'}${state ? ` in ${state.dir_name}` : ''}`
        : [
            '',
            'You',
            'What may agents do here?',
            'How do you want to approve things?',
            'Your secret words',
            'Review your setup',
          ][step]
  return (
    <main className="setup">
      <header>
        <strong>
          orch<span>.</span>
        </strong>
        <nav aria-label="Setup progress">
          {steps.map((s, i) => (
            <span
              key={s}
              className={i <= step ? 'active' : ''}
              aria-current={i === step ? 'step' : undefined}
              aria-label={`${i + 1}. ${s}`}
            />
          ))}
        </nav>
      </header>
      <h1 ref={heading} tabIndex={-1}>
        {title}
      </h1>
      {error && (
        <p role="alert" className="warning">
          {error}
        </p>
      )}
      {error && reason && <small className="reason">Reason: {reason}</small>}
      {state?.error && (
        <p role="alert" className="warning">
          <Raw>{state.error.message}</Raw> Check your terminal, then run <code>orch setup</code> in this folder to resume.
        </p>
      )}
      {!state && !error && <p role="status">Connecting to setup…</p>}
      {state && !ended && !run && (
        <fieldset disabled={busy}>
          {step === 0 && (
            <>
              <p>This keeps tickets for you and your AI agents.</p>
              <div className="inset">
                <small>Project folder</small>
                <p>
                  <Raw>{state.dir}</Raw>
                </p>
              </div>
              <p>Everything stays on this computer unless you choose to connect your phone.</p>
              {state.mode === 'finish' && (
                <>
                  <p>We will finish only the steps that are still missing.</p>
                  <ul>
                    {state.missing.map((id) => (
                      <li key={id}>
                        <Raw>{id}</Raw>
                      </li>
                    ))}
                  </ul>
                </>
              )}
              <footer>{next(1)}</footer>
            </>
          )}
          {step === 1 && (
            <>
              <label>
                Your name
                <input autoComplete="name" value={profile.name} onChange={(e) => update({ name: e.target.value })} />
              </label>
              <label>
                Workspace short name
                <input value={profile.prefix} onChange={(e) => update({ prefix: e.target.value })} />
              </label>
              <p>Used in ticket names, like TST-0001.</p>
              <footer>
                <button onClick={() => setStep(0)}>Back</button>
                <button
                  className="primary"
                  disabled={!profile.name.trim() || !profile.prefix.trim()}
                  onClick={() => setStep(2)}
                >
                  Continue →
                </button>
              </footer>
            </>
          )}
          {step === 2 && (
            <>
              <p>A starting point. You can change these choices in Settings.</p>
              <label className="toggle">
                <span>
                  <strong>Dashboard</strong>
                  <small>Your workspace in the browser · required</small>
                </span>
                <input type="checkbox" role="switch" checked disabled />
              </label>
              {state.addons
                .filter((a) => a.id !== 'dashboard')
                .map((a) => (
                  <label className="toggle" key={a.id}>
                    <span>
                      <strong>{a.title}</strong>
                      <small>{a.desc}</small>
                      {a.capabilities.map((c) => (
                        <small key={c}>
                          <Raw>{c}</Raw>
                        </small>
                      ))}
                    </span>
                    <input
                      type="checkbox"
                      role="switch"
                      checked={profile.addons.includes(a.id)}
                      disabled={a.required}
                      onChange={(e) =>
                        update({
                          addons: e.target.checked
                            ? [...profile.addons, a.id]
                            : profile.addons.filter((id) => id !== a.id),
                        })
                      }
                    />
                  </label>
                ))}
              <footer>
                <button onClick={() => setStep(1)}>Back</button>
                {next(3)}
              </footer>
            </>
          )}
          {step === 3 && (
            <>
              <p>Choose what works for you on this device.</p>
              {state.methods.map((method) => (
                <label className="toggle" key={method}>
                  <input
                    type="radio"
                    name="method"
                    checked={profile.method === method}
                    disabled={method === 'click' && !state.click_available}
                    onChange={() => update({ method, ...(method === 'click' ? { relay: false } : {}) })}
                  />
                  <span>
                    <strong>{method === 'click' ? 'One click on this computer' : 'Terminal'}</strong>
                    <small>
                      {method === 'terminal'
                        ? 'Confirm in your terminal'
                        : state.click_available
                          ? 'Approve here with a click'
                          : state.click_reason}
                    </small>
                  </span>
                </label>
              ))}
              <h2>Let agents work for you for:</h2>
              <div className="segments">
                {state.agent_hours.map((h) => (
                  <button key={h} aria-pressed={profile.agent_hours === h} onClick={() => update({ agent_hours: h })}>
                    {h === 8 ? '8 hours' : `${h / 24} ${h === 24 ? 'day' : 'days'}`}
                  </button>
                ))}
              </div>
              <label className="toggle">
                <span>
                  Give this computer’s runner agent access
                  <small>The access key stays with the runner, never in this page.</small>
                </span>
                <input
                  type="checkbox"
                  role="switch"
                  checked={profile.agent_grant}
                  onChange={(e) => update({ agent_grant: e.target.checked })}
                />
              </label>
              {state.relay.available && state.relay.enabled && (
                <>
                  <label className="toggle">
                    <span>
                      Connect relay and phone<small>Optional · your workspace starts local</small>
                    </span>
                    <input
                      type="checkbox"
                      role="switch"
                      checked={profile.relay}
                      disabled={profile.method === 'click' || !state.relay.available || !state.relay.enabled}
                      onChange={(e) => update({ relay: e.target.checked })}
                    />
                  </label>
                  {profile.method === 'click' && (
                    <p>
                      Relay is unavailable with One click:{' '}
                      {state.click_reason ||
                        'One click and phone approvals cannot be used together yet (click_relay_unsupported).'}
                    </p>
                  )}
                </>
              )}
              <footer>
                <button onClick={() => setStep(2)}>Back</button>
                <button className="primary" onClick={() => void act(approveProfile)}>
                  Continue →
                </button>
              </footer>
            </>
          )}
          {step === 4 && (
            <>
              <p>Keep these somewhere safe. Only you should have them.</p>
              <p className="warning">{voidCopy}</p>
              {secrets ? (
                <>
                  {secrets.passphrase && (
                    <section className="passphrase">
                      <h2>Your passphrase</h2>
                      <p className="inset">{secrets.passphrase}</p>
                      <p>Never type your passphrase into this page.</p>
                    </section>
                  )}
                  <section className="recovery-print">
                    <h2>Recovery code</h2>
                    <ol className="recovery">
                      {secrets.recovery_words.map((word, i) => (
                        <li key={i}>
                          <span>{i + 1}</span> {word}
                        </li>
                      ))}
                    </ol>
                  </section>
                  <div className="segments">
                    <button onClick={() => void copyRecovery()}>Copy recovery code</button>
                    <button onClick={() => window.print()}>Print recovery code</button>
                  </div>
                  <p role="status">{notice}</p>
                  <label>
                    <input type="checkbox" checked={saved} onChange={(e) => setSaved(e.target.checked)} /> I saved my
                    secret words and recovery code
                  </label>
                  <h2>Check three recovery words</h2>
                  <div className="word-check">
                    {secrets.check_positions.map((n) => (
                      <label key={n}>
                        Word {n}
                        <input
                          autoComplete="off"
                          autoCorrect="off"
                          spellCheck={false}
                          value={words[String(n)] ?? ''}
                          onChange={(e) => setWords({ ...words, [String(n)]: e.target.value })}
                        />
                      </label>
                    ))}
                  </div>
                  <p>Only these three recovery words are checked. Five incorrect attempts end this session.</p>
                  <footer>
                    <button
                      className="primary"
                      disabled={!saved || secrets.check_positions.some((n) => !words[String(n)]?.trim())}
                      onClick={() => void act(confirm)}
                    >
                      Confirm saved words →
                    </button>
                  </footer>
                </>
              ) : (
                <button onClick={() => void act(getReview)}>Continue to review →</button>
              )}
            </>
          )}
          {step === 5 && review && (
            <>
              <h2>You are about to approve</h2>
              <ul className="review-summary">
                <li>Agents may work for you for up to {duration(review.runner_grant?.hours ?? profile.agent_hours)}</li>
                <li>
                  {profile.method === 'click'
                    ? 'You approve in the terminal or with one click on this Mac'
                    : 'You approve in the terminal'}
                </li>
                {addonTitles.length > 0 && (
                  <li>
                    Add-ons: <Inline>{addonTitles.join(', ')}</Inline>
                  </li>
                )}
              </ul>
              {state.mode === 'new' && <p className="warning">{voidCopy}</p>}
              {(review.risk || review.flags.some((f) => f.id === 'runner' || f.id === 'terminals')) && (
                <div className="warning">
                  <p>
                    {profile.method === 'click'
                      ? 'One-click approval and dashboard agents act as you on this Mac.'
                      : 'Dashboard agents act as you on this Mac.'}{' '}
                    Only continue on a computer you trust.
                  </p>
                  <details>
                    <summary>Why?</summary>
                    {review.risk && (
                      <p>
                        <Raw>{review.risk.text}</Raw>
                      </p>
                    )}
                    {review.flags.map((f) => (
                      <p key={f.id}>
                        <Raw>{f.text}</Raw>
                      </p>
                    ))}
                  </details>
                </div>
              )}
              <details className="review-technical">
                <summary>Show technical details</summary>
                <p>Times and IDs are filled in when you sign.</p>
                {review.items.map((item) => (
                  <section className="review-item" key={item.id}>
                    <h3>
                      <Raw>{item.title}</Raw>
                    </h3>
                    {item.lines.filter(shown).map((line, i) => (
                      <p key={i}>
                        <Raw>{line}</Raw>
                      </p>
                    ))}
                    {item.note && (
                      <p>
                        <Raw>{item.note}</Raw>
                      </p>
                    )}
                    {item.details.filter(shown).map((line, i) => (
                      <p key={`d${i}`}>
                        <Raw>{line}</Raw>
                      </p>
                    ))}
                  </section>
                ))}
                {review.runner_grant && (
                  <section className="review-item">
                    <h3>Runner access</h3>
                    <p>
                      {review.runner_grant.hours} hours · <Raw>{review.runner_grant.scope}</Raw>
                    </p>
                    <p>{review.runner_grant.holder}</p>
                  </section>
                )}
              </details>
              {review.tty_confirm && (
                <div className="inset">
                  <h2>Then confirm in your terminal</h2>
                  <ol>
                    {review.typed_word && (
                      <li>
                        Type <Raw>{review.typed_word}</Raw> below and press {state.mode === 'finish' ? 'Finish setup' : 'Create workspace'}.
                      </li>
                    )}
                    <li>
                      Your terminal then shows three words. If they match <Raw>{review.fingerprint?.join(' ') ?? ''}</Raw>,
                      type yes there.
                    </li>
                  </ol>
                </div>
              )}
              {review.typed_word && (
                <label>
                  Type <Raw>{review.typed_word}</Raw> here to confirm
                  <input autoComplete="off" value={typed} onChange={(e) => setTyped(e.target.value)} />
                </label>
              )}
              {(profile.agent_hours > 8 || profile.agent_grant || review.runner_grant) && !review.typed_word && (
                <p role="alert">A typed confirmation is required. Return to your terminal to restart setup.</p>
              )}
              {terminalReviewMissing && (
                <p role="alert">Terminal confirmation is missing. Return to your terminal to restart setup.</p>
              )}
              <footer>
                <button
                  className="primary"
                  disabled={
                    terminalReviewMissing ||
                    (!!review.typed_word && typed !== review.typed_word) ||
                    !!((profile.agent_hours > 8 || profile.agent_grant || review.runner_grant) && !review.typed_word)
                  }
                  onClick={() =>
                    void act(async () => {
                      const result = await api.call<{ run_id: string }>('create', {
                        review_id: review.review_id,
                        digest: review.digest,
                        ...(review.typed_word ? { typed_word: typed } : {}),
                      })
                      setTyped('')
                      setRun(result.run_id)
                    })
                  }
                >
                  {state.mode === 'finish' ? 'Finish setup' : 'Create workspace'}
                </button>
              </footer>
            </>
          )}
        </fieldset>
      )}
      {run && (
        <section aria-live="polite">
          {review?.tty_confirm &&
            !progress?.done &&
            !progress?.steps.some((s) => s.status === 'doing' || s.status === 'done') && (
              <p>
                Your terminal now asks you to confirm. If it shows <Raw>{review.fingerprint?.join(' ') ?? ''}</Raw>, type
                yes there.
              </p>
            )}
          {progress?.waiting && (
            <p role="status">
              {progress.waiting === 'waiting for terminal'
                ? 'Confirm in your terminal'
                : progress.waiting === 'runner' || progress.waiting === 'waiting for runner'
                  ? 'Waiting for the agent runner to connect…'
                  : <Raw>{progress.waiting}</Raw>}
            </p>
          )}
          <ol className="progress">
            {progress?.steps.map((s) => (
              <li key={s.id}>
                <span>{s.label}</span>
                <strong>
                  {{ todo: 'Waiting', doing: 'Working…', done: 'Done', failed: 'Failed', skipped: 'Skipped' }[s.status]}
                </strong>
              </li>
            ))}
          </ol>
          {progress?.error && (
            <>
              <p role="alert" className="warning">
                {progress.error.finishable
                  ? 'Your workspace was created, but some setup steps need attention. Run orch setup in this folder to finish them later.'
                  : voidCopy}
              </p>
              <p>
                <Raw>{progress.error.message}</Raw>
              </p>
              {progress.retry.length > 0 && (
                <p>
                  Still to finish:{' '}
                  {progress.retry.map((id) => (
                    <Raw key={id}>{id + ' '}</Raw>
                  ))}
                </p>
              )}
              <button
                onClick={() =>
                  void act(async () => {
                    await api.call('cancel', {})
                    api.forget()
                    setEnded(true)
                    setRun(undefined)
                  })
                }
              >
                Finish setup later
              </button>
            </>
          )}
          {progress?.done && !progress.error && (
            <button className="primary" disabled={busy} onClick={() => void act(finish)}>
              Open your workspace →
            </button>
          )}
        </section>
      )}
      {!ended && !run && state && (
        <button
          className="cancel"
          disabled={busy}
          onClick={() =>
            void act(async () => {
              drop()
              await api.call('cancel', {})
              api.forget()
              setEnded(true)
            })
          }
        >
          Finish setup later
        </button>
      )}
      {ended && (
        <p>
          You can close this page. Run <code>orch setup</code> in your project folder when you are ready.
        </p>
      )}
    </main>
  )
}
export async function mount(session: Promise<SetupAPI> = bootstrapSetup()) {
  const root = createRoot(document.getElementById('root')!)
  try {
    const api = await session
    root.render(<Setup api={api} />)
  } catch (e) {
    const reason = report(e)
    root.render(
      <main className="setup">
        <h1>Open setup from your terminal</h1>
        <p>{expiredCopy}</p>
        <small className="reason">Reason: {reason}</small>
      </main>,
    )
  }
}
