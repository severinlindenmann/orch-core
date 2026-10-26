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
  'This session has ended. In your terminal, press Enter to open a fresh setup link (or run orch setup again). It starts a new session with new secret words and a new recovery code.'
/** A short, secret-free cause for the "session ended" copy: the API code, or `client: Name: message`. */
export function reasonOf(e: unknown): string {
  if (e instanceof SetupError) return e.code
  const name = e instanceof Error ? e.name : 'Error'
  const message = e instanceof Error ? e.message : String(e)
  return `client: ${name}: ${message.split('#')[0].replace(/[\u0000-\u001f\u007f]+/g, ' ').slice(0, 120)}`
}
/** The cause in plain words; the code itself stays under "Technical details". */
export function plainReason(code: string): string {
  if (code === 'session.required' || code === 'session.expired') return 'This page no longer has its link to your terminal. This happens after a reload or when the session ran out.'
  if (code === 'setup.ended') return 'This setup was already finished or stopped.'
  if (code === 'setup.token' || code === 'setup.unavailable') return 'The link to your terminal could not be used.'
  if (code.startsWith('client:') || code.startsWith('http.')) return 'The page could not reach orch on this computer.'
  return 'Setup could not go on.'
}
/** Why One click is greyed out, in plain words. Core sends a code (today only `dev_host`); an unknown one never shows raw. */
export function clickOffText(code: string): string {
  if (code === 'dev_host') return 'One click is off on this test computer; you approve in your terminal.'
  return 'One click is not available on this computer; you approve in your terminal.'
}
function Reason({ reason }: { reason: string }) {
  return (
    <>
      <small>{plainReason(reason)}</small>
      <details className="reason">
        <summary>Technical details</summary>
        <small>Reason: {reason}</small>
      </details>
    </>
  )
}
function report(e: unknown): string {
  const reason = reasonOf(e)
  // A link that ended or a reload is an ordinary outcome the page explains in words: no console error for it.
  if (!/^(session\.|setup\.(ended|token|unavailable)|secrets\.)/.test(reason)) console.error('[orch setup]', reason)
  return reason
}
function clearClipboard() {
  void navigator.clipboard.writeText('').catch(() => {})
}
/** Hide the lines that carry no information for a person: blanks and values the host fills in at signing. */
const shown = (line: string) => line.trim() !== ''
/** Lines of a review row without blanks and without the values the host fills in at signing: core names those by
 * position (`placeholders`); a line is never hidden for how it ends. */
const rowLines = (lines: string[], hidden: number[] | undefined) =>
  lines.filter((line, i) => shown(line) && !(hidden ?? []).includes(i))
/** The plain summary of what a review signs, from the review core returned and nothing else: the events' own fields,
 * the runner grant, and core's flags. A kind this page does not know is shown by its title (and opens the technical
 * details), never left out. */
export function summarize(review: Review): { lines: string[]; unknown: boolean } {
  const lines: string[] = []
  let unknown = false
  const addons: string[] = []
  const odd = (title: string) => {
    unknown = true
    lines.push(title)
  }
  for (const item of review.items) {
    const f = item.fields
    const only = (...keys: string[]) => !!f && Object.keys(f).every((k) => k === 'type' || keys.includes(k))
    if (item.type === 'addon.granted') addons.push(item.title)
    else if (item.id === 'runner_grant' && item.type === 'grant.issued' && f && typeof f.hours === 'number' && typeof f.scope === 'string' && typeof f.verbs === 'string')
      // the ordinary grant in words (scope and verbs are in the technical details); any other scope or verbs keeps the raw values
      lines.push(f.scope === 'workable' && f.verbs === 'agent'
        ? `This computer’s agent runner may work on any ticket you can see, as you, for ${duration(f.hours)}`
        : `This computer’s agent runner gets agent access for ${duration(f.hours)} (scope ${f.scope}, verbs ${f.verbs})`)
    else if (item.type === 'settings.changed' && only('set') && f?.set && typeof f.set === 'object' && !Array.isArray(f.set)) {
      const set = f.set as Record<string, unknown>
      const okHours = !('grant_hours' in set) || typeof set.grant_hours === 'number'
      const okMethods = !('sign_methods' in set) || (Array.isArray(set.sign_methods) && set.sign_methods.every((m) => typeof m === 'string'))
      if (!Object.keys(set).every((k) => k === 'grant_hours' || k === 'sign_methods') || !okHours || !okMethods) odd(item.title)
      else {
        if (typeof set.grant_hours === 'number') lines.push(`Agents may work for you for up to ${duration(set.grant_hours)}`)
        if (Array.isArray(set.sign_methods))
          lines.push(`Ways to approve allowed here: ${set.sign_methods.map((m) => (m === 'click' ? 'one click' : String(m))).join(', ')}`)
      }
    } else odd(item.title)
  }
  for (const flag of review.flags) {
    if (flag.id === 'custody' || flag.id === 'terminal' || flag.id === 'records') lines.push(flag.text)
    else if (flag.id !== 'runner' && flag.id !== 'terminals') odd(flag.text) // a flag this page does not know is shown
  }
  if (addons.length) lines.push(`Add-ons: ${addons.join(', ')}`)
  return { lines, unknown }
}
const prefixOk = (p: string) => /^[A-Z][A-Z0-9]{1,7}$/.test(p)
/** A ticket prefix from a workspace name: first letters, capitals, letters and digits only ("Marketing Department" gives
 * MARK, "Personal Workspace" gives PW); empty or unusable names fall back to PW. */
export function suggestPrefix(name: string): string {
  const words = name.toUpperCase().match(/[A-Z0-9]+/g) ?? []
  const p = (words.length >= 2 ? words.map((w) => w[0]).join('') : (words[0] ?? '').slice(0, 4)).slice(0, 8)
  return prefixOk(p) ? p : 'PW'
}
const statusWord = { todo: 'Waiting', doing: 'Running', done: 'Done', failed: 'Failed', skipped: 'Skipped' }
const statusIcon = { todo: '•', doing: '◔', done: '✓', failed: '✕', skipped: '–' }
function duration(hours: number): string {
  if (hours > 8 && hours % 24 === 0) return `${hours / 24} ${hours === 24 ? 'day' : 'days'}`
  return `${hours} hours`
}
/** The three fingerprint words: each stays whole, lines break only between words. */
function Words({ words }: { words: string[] | null }) {
  return (
    <span className="words">
      {(words ?? []).map((w, i) => (
        <span key={i}>
          {i > 0 && ' '}
          <Raw>{w}</Raw>
        </span>
      ))}
    </span>
  )
}
const pausedCopy = 'The setup in your terminal has ended. Run orch setup again to start a new session with new secret words.'
const steps = ['Welcome', 'You', 'Agents may', 'Approval', 'Secret words', 'Review']
/** Finish setup has two screens: what exists and what to add, then the review. */
const finishSteps = ['Your workspace', 'Review']
/** Hours a person can choose, in words. */
const hoursText = (h: number) => (h === 8 ? '8 hours' : `${h / 24} ${h === 24 ? 'day' : 'days'}`)
/** The largest choice that is not longer than what the workspace has (never a downgrade, never invalid). */
const nearestHours = (current: number, choices: number[]) =>
  [...choices].sort((a, b) => b - a).find((h) => h <= current) ?? choices[0] ?? 8
const yesNo = (on: boolean) => (on ? 'On' : 'Off')
type Current = NonNullable<SetupState['current']>
/** Finish setup (B11 finding 5): what the workspace has is shown as it is; only what is missing can be chosen, and
 * nothing already on can be switched off or shortened here. */
function FinishChoices({
  state,
  current,
  profile,
  update,
  onContinue,
  onOpen,
}: {
  state: SetupState
  current: Current
  profile: Profile
  update: (value: Partial<Profile>) => void
  onContinue: () => void
  onOpen: () => void
}) {
  const on = state.addons.filter((a) => a.id === 'dashboard' || current.addons_on.includes(a.id))
  const canAdd = state.addons.filter((a) => a.kind === 'addon' && !a.required && !current.addons_on.includes(a.id))
  // a runner grant is never longer than the workspace's agent time: below the shortest choice (8 h) none is offered
  const offersGrant = state.missing.includes('runner_grant') && current.agent_hours >= 8
  const offersClick = current.method === 'terminal' && state.click_available
  const hoursFixed = current.agent_hours !== 8
  const chosen =
    canAdd.some((a) => profile.addons.includes(a.id)) || profile.agent_grant || (offersClick && profile.method === 'click')
  const label = (id: string) => state.labels?.[id] ?? 'A setup step'
  return (
    <>
      {!state.complete && <p>This workspace already exists. Nothing that is on will be switched off.</p>}
      <h2>Already set up</h2>
      <dl className="current">
        <dt>Name</dt>
        <dd>
          <Raw>{current.name}</Raw>
        </dd>
        <dt>Short name</dt>
        <dd>
          <Raw>{current.prefix}</Raw>
        </dd>
        <dt>Add-ons on</dt>
        <dd>{on.map((a) => a.title).join(', ')}</dd>
        <dt>Agents may work for</dt>
        <dd>{hoursText(current.agent_hours)}</dd>
        <dt>Agent execution on this computer</dt>
        <dd>{yesNo(current.runner.enabled)}</dd>
        <dt>Agent access for this computer’s runner</dt>
        <dd>{current.runner_grant ? 'Given before' : 'Not given'}</dd>
        <dt>You approve with</dt>
        <dd>{current.method === 'click' ? 'One click on this computer' : 'Terminal'}</dd>
      </dl>
      {!state.complete && (canAdd.length > 0 || offersGrant || offersClick) ? (
        <>
          <h2>You can still add</h2>
          {canAdd.map((a) => (
            <label className="toggle" key={a.id}>
              <span>
                <strong>{a.title}</strong>
                <small>{a.desc}</small>
              </span>
              <input
                type="checkbox"
                role="switch"
                checked={profile.addons.includes(a.id)}
                onChange={(e) =>
                  update({
                    addons: e.target.checked ? [...profile.addons, a.id] : profile.addons.filter((id) => id !== a.id),
                  })
                }
              />
            </label>
          ))}
          {offersGrant && (
            <>
              <label className="toggle">
                <span>
                  <strong>{label('runner_grant')}</strong>
                  <small>The runner is the program on this computer that starts your agents. Its access key never goes into this page.</small>
                </span>
                <input
                  type="checkbox"
                  role="switch"
                  checked={profile.agent_grant}
                  onChange={(e) => update({ agent_grant: e.target.checked })}
                />
              </label>
              {profile.agent_grant && !hoursFixed && (
                <>
                  <h2>Let agents work for:</h2>
                  <div className="segments">
                    {state.agent_hours.map((h) => (
                      <button key={h} aria-pressed={profile.agent_hours === h} onClick={() => update({ agent_hours: h })}>
                        {hoursText(h)}
                      </button>
                    ))}
                  </div>
                </>
              )}
            </>
          )}
          {offersClick && (
            <label className="toggle">
              <span>
                <strong>Approve with one click on this computer</strong>
                <small>Instead of the terminal. You keep your passphrase.</small>
              </span>
              <input
                type="checkbox"
                role="switch"
                checked={profile.method === 'click'}
                onChange={(e) => update({ method: e.target.checked ? 'click' : 'terminal', relay: false })}
              />
            </label>
          )}
        </>
      ) : (
        <p>Nothing is missing.</p>
      )}
      <footer>
        {state.complete ? (
          <button className="primary" onClick={onOpen}>
            Open dashboard →
          </button>
        ) : (
          <button className="primary" disabled={!chosen} onClick={onContinue}>
            Continue →
          </button>
        )}
      </footer>
    </>
  )
}
/** U2: the one plain message wherever setup waits for the terminal. */
export const terminalMessage = 'Now look at your terminal: orch asks you to confirm there.'
function TerminalNotice({ children }: { children?: React.ReactNode }) {
  return (
    <div className="terminal-notice" role="status">
      <svg aria-hidden="true" viewBox="0 0 24 24" width="28" height="28" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <rect x="2.5" y="4" width="19" height="16" rx="2.5" />
        <path d="M7 10l3 2.5L7 15" />
        <path d="M12.5 15h4.5" />
      </svg>
      <div>
        <strong>{terminalMessage}</strong>
        {children}
        <small>Waiting for your confirmation…</small>
      </div>
    </div>
  )
}
/** The final check: three large tiles to compare with the setup window. The browser never imitates that window. */
export function ConfirmWords({ words }: { words: string[] | null }) {
  return (
    <section className="confirm-words" role="status" aria-label="Final check">
      <p>
        <strong>Continue in the setup window on this computer.</strong> Compare these words with it. If all three match in this order, type yes in that window
        and press Enter.
      </p>
      <ol className="words word-tiles" aria-label="Your three words">
        {(words ?? []).map((w, i) => (
          <li key={i}>
            <Raw>{w}</Raw>
          </li>
        ))}
      </ol>
      <small className="words-wait">Waiting for you in the setup window…</small>
      <details>
        <summary>The words don’t match</summary>
        <p>Do not type yes. Close the setup window, then run orch setup again to start over with new words.</p>
      </details>
    </section>
  )
}
export function Setup({ api }: { api: SetupAPI }) {
  const [state, setState] = useState<SetupState>()
  const [profile, setProfile] = useState<Profile>({
    name: '',
    workspace_name: 'Personal Workspace',
    prefix: 'PW',
    addons: [],
    method: 'terminal',
    agent_hours: 8,
    agent_grant: false,
    relay: false,
    records: true,
  })
  const [step, setStep] = useState(0)
  const [prefixEdited, setPrefixEdited] = useState(false)
  const [secrets, setSecrets] = useState<Secrets | null>(null)
  const [words, setWords] = useState<Record<string, string>>({})
  const [review, setReview] = useState<Review>()
  const [run, setRun] = useState<string>()
  const [progress, setProgress] = useState<Progress>()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [ended, setEnded] = useState(false)
  const [saved, setSaved] = useState(false)
  const [notice, setNotice] = useState('')
  const [reason, setReason] = useState('')
  // "No agent access" is a choice of its own (B11 finding 21); it keeps the workspace default time and no runner grant.
  const [access, setAccess] = useState<'none' | number>('none')
  const chooseAccess = (a: 'none' | number) => {
    setAccess(a)
    setProfile((p) => ({ ...p, agent_hours: a === 'none' ? 8 : a, ...(a === 'none' ? { agent_grant: false } : {}) }))
    setReview(undefined)
  }
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
            ? 'Check your name, workspace name and ticket prefix. Use 2–8 capital letters or digits for the prefix, starting with a letter.'
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
          setState(s)
          if (s.mode === 'finish' && s.current) {
            // Finish setup starts from what the workspace has: nothing is switched off, no time is shortened.
            const c = s.current
            setProfile((p) => ({
              ...p,
              name: c.name,
              workspace_name: s.suggested.workspace_name,
              prefix: c.prefix,
              addons: s.addons.filter((a) => a.required || (a.kind === 'addon' && c.addons_on.includes(a.id))).map((a) => a.id),
              method: c.method,
              agent_hours: nearestHours(c.agent_hours, s.agent_hours),
            }))
          } else {
            const addons = s.addons.filter((a) => a.required || a.default).map((a) => a.id)
            // agent_hours/agent_grant are seeded only together with the access picker, so the two never disagree
            const { agent_hours: sugHours, agent_grant: sugGrant, ...rest } = s.suggested
            setProfile((p) => ({ ...p, ...rest, addons }))
            // U1: "let agents work for you" starts ticked (3 days); the person can still pick "No agent access"
            if (sugGrant && typeof sugHours === 'number' && s.agent_hours.includes(sugHours)) {
              chooseAccess(sugHours)
              setProfile((p) => ({ ...p, agent_grant: true }))
            }
          }
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
  // The setup in the terminal can end while this page is open (Ctrl-C, closed terminal, "Finish setup later" elsewhere).
  // Notice within about two seconds, wipe the secret words from the page and say so (B11 finding 4).
  const progressDone = !!progress?.done
  useEffect(() => {
    if (!state || ended || progressDone) return
    let active = true
    let failures = 0
    const timer = setInterval(() => {
      api.call<SetupState>('state').then(
        (s) => {
          failures = 0
          if (active && (s.phase === 'cancelled' || s.error)) pause()
        },
        (e: unknown) => {
          if (!active) return
          const code = e instanceof SetupError ? e.code : ''
          // The setup process is gone (its socket is closed): paused at once. A failed request without an answer
          // (network) has to repeat once, so a single hiccup does not end a session that is fine.
          if (['setup.unavailable', 'setup.ended', 'setup.token', 'session.required', 'session.expired'].includes(code)) pause()
          else if (++failures >= 2) pause()
        },
      )
    }, 1000)
    function pause() {
      active = false
      clearInterval(timer)
      drop()
      api.forget()
      setRun(undefined)
      setEnded(true)
      setReason('')
      setError(pausedCopy)
    }
    return () => {
      active = false
      clearInterval(timer)
    }
  }, [api, state, ended, progressDone])
  // B12 N8: leaving the page after "Your workspace is ready" tells the setup it is finished, so the terminal ends with
  // one closing line instead of waiting for its idle timeout.
  const readyRef = useRef(false)
  readyRef.current = !!progress?.done && !progress.error
  useEffect(() => {
    const hide = () => {
      if (readyRef.current) api.leave()
      else api.left()
      drop()
      api.forget()
    }
    window.addEventListener('pagehide', hide)
    return () => {
      window.removeEventListener('pagehide', hide)
      drop()
    }
  }, [api])
  // Worktrees need a git repository: a new workspace in a folder without git offers to make one (nothing is committed)
  const offersGit = state?.mode === 'new' && state.git?.repo === false && profile.addons.includes('worktrees')
  const update = (value: Partial<Profile>) => {
    setProfile((p) => ({ ...p, ...value }))
    setReview(undefined)
  }
  async function getReview() {
    setReview(await api.call<Review>('review', {}))
    setStep(5)
  }
  async function approveProfile() {
    await api.call('profile', { ...profile, git_init: offersGit && profile.git_init === true, records: state?.mode !== 'finish' && profile.records !== false })
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
    // A wrong attempt keeps the entries that were right and clears only the wrong ones (the words are on this page
    // anyway). A right one, or the end of the session, clears everything.
    const typedWords = { ...words }
    const keep: Record<string, string> = {}
    if (!result.ok && result.attempts_left !== 0 && secretRef.current)
      for (const [pos, w] of Object.entries(typedWords))
        if (w.trim().toLowerCase() === secretRef.current.recovery_words[Number(pos) - 1]) keep[pos] = w
    Object.keys(words).forEach((key) => {
      words[key] = ''
    })
    setWords(keep)
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
    const result = await api.call<{ url: string; workspace_id?: string }>('finish', {})
    const url = new URL(result.url, location.origin)
    if (url.origin !== location.origin || !url.pathname.startsWith('/')) throw new Error('destination')
    // The dashboard opens the workspace it remembers: make that the new one (same key as app/workspace.tsx).
    if (typeof result.workspace_id === 'string' && result.workspace_id) {
      try {
        localStorage.setItem('orch.workspace', result.workspace_id)
      } catch {
        /* storage unavailable: the dashboard picks its own */
      }
    }
    drop()
    api.forget()
    location.assign(url.pathname + url.search + url.hash)
  }
  const terminalReviewMissing =
    !!review &&
    (((state?.mode === 'finish' || !!review.runner_grant) && !review.tty_confirm) ||
      (review.tty_confirm && review.fingerprint?.length !== 3))
  const summary = review ? summarize(review) : { lines: [], unknown: false }
  const next = (n: number) => (
    <button className="primary" onClick={() => setStep(n)}>
      Continue →
    </button>
  )
  // The dots carry their names (compact on a phone: only the current one). Finish setup has its own two steps.
  const finishing = state?.mode === 'finish'
  const names = finishing ? finishSteps : steps
  const at = finishing ? (step === 5 ? 1 : 0) : step
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
      : step === 0 && state?.complete
        ? 'Setup is complete'
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
          <ol>
            {names.map((s, i) => (
              <li
                key={s}
                className={`${i <= at ? 'active' : ''}${i === at ? ' current' : ''}`}
                aria-current={i === at ? 'step' : undefined}
              >
                <span className="dot" aria-hidden />
                <span className="name">{s}</span>
              </li>
            ))}
          </ol>
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
      {error && reason && <Reason reason={reason} />}
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
              {state.mode === 'finish' && state.current ? (
                <FinishChoices
                  state={state}
                  current={state.current}
                  profile={profile}
                  update={update}
                  onContinue={() => void act(approveProfile)}
                  onOpen={() => void act(finish)}
                />
              ) : (
                <footer>{next(1)}</footer>
              )}
            </>
          )}
          {step === 1 && (
            <>
              <label>
                Your name
                <input autoComplete="name" value={profile.name} onChange={(e) => update({ name: e.target.value })} />
              </label>
              <label>
                Workspace name
                <input
                  value={profile.workspace_name}
                  onChange={(e) => {
                    const workspace_name = e.target.value
                    update(prefixEdited ? { workspace_name } : { workspace_name, prefix: suggestPrefix(workspace_name) })
                  }}
                />
                <small>What people see in menus, e.g. Marketing Department.</small>
              </label>
              <label>
                Ticket prefix
                <input
                  value={profile.prefix}
                  aria-invalid={!prefixOk(profile.prefix) || undefined}
                  onChange={(e) => {
                    setPrefixEdited(true)
                    update({ prefix: e.target.value.toUpperCase() })
                  }}
                />
                <small>{prefixOk(profile.prefix) ? 'Tickets are numbered with it.' : 'Use 2–8 capital letters or digits, starting with a letter.'}</small>
              </label>
              <div className="ticket-preview" aria-label="Ticket number preview">
                {[1, 2, 3].map((n) => (
                  <span className="chip" key={n}>
                    {profile.prefix.trim() || 'PW'}-{String(n).padStart(4, '0')}
                  </span>
                ))}
              </div>
              <footer>
                <button onClick={() => setStep(0)}>Back</button>
                <button
                  className="primary"
                  disabled={!profile.name.trim() || !profile.workspace_name.trim() || !prefixOk(profile.prefix)}
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
                .map((a) =>
                  a.kind === 'flag' ? (
                    // Nothing to switch: the text says so and there is no switch (B11 finding 20).
                    <div className="toggle" key={a.id}>
                      <span>
                        <strong>{a.title}</strong>
                        <small>{a.desc}</small>
                      </span>
                      <span className="always">Always on</span>
                    </div>
                  ) : (
                  <label className="toggle" key={a.id}>
                    <span>
                      <strong>{a.title}</strong>
                      <small>{a.desc}</small>
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
                          // ticking Worktrees again asks the git question again
                          ...(a.id === 'worktrees' ? { git_init: undefined } : {}),
                        })
                      }
                    />
                  </label>
                  ),
                )}
              {offersGit && (
                <div className="note" role="group" aria-label="Git repository">
                  <p>
                    <strong>This folder is not a git repository.</strong> Worktrees need one.
                  </p>
                  <label className="toggle">
                    <span>
                      <strong>Make it a git repository</strong>
                      <small>
                        Runs git init and keeps orch’s own folders out of git. None of your files is committed.
                      </small>
                    </span>
                    <input
                      type="radio"
                      name="git"
                      checked={profile.git_init === true}
                      onChange={() => update({ git_init: true })}
                    />
                  </label>
                  <label className="toggle">
                    <span>
                      <strong>Continue without worktrees</strong>
                      <small>Each ticket gets its own work folder instead.</small>
                    </span>
                    <input
                      type="radio"
                      name="git"
                      checked={profile.git_init === false}
                      onChange={() =>
                        update({ git_init: false, addons: profile.addons.filter((id) => id !== 'worktrees') })
                      }
                    />
                  </label>
                </div>
              )}
              <footer>
                <button onClick={() => setStep(1)}>Back</button>
                {offersGit && profile.git_init === undefined ? (
                  <button className="primary" disabled>
                    Choose above to continue →
                  </button>
                ) : (
                  next(3)
                )}
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
                          : clickOffText(state.click_reason)}
                    </small>
                  </span>
                </label>
              ))}
              {profile.method === 'click' && state.click_available && (
                <p className="notice" data-testid="click-risk">
                  <Inline>{state.click_risk}</Inline>
                </p>
              )}
              <h2>Let agents work for you for:</h2>
              <div className="segments">
                <button aria-pressed={access === 'none'} onClick={() => chooseAccess('none')}>
                  No agent access
                </button>
                {state.agent_hours.map((h) => (
                  <button key={h} aria-pressed={access === h} onClick={() => chooseAccess(h)}>
                    {hoursText(h)}
                  </button>
                ))}
              </div>
              {access === 'none' ? (
                <p>No agent can work in this workspace until you give access. You can do that later.</p>
              ) : (
                <label className="toggle">
                  <span>
                    Let this computer’s agent runner start agents now
                    <small>
                      The runner is the program on this computer that starts your agents. Its access key stays with it and
                      never goes into this page.
                    </small>
                  </span>
                  <input
                    type="checkbox"
                    role="switch"
                    checked={profile.agent_grant}
                    onChange={(e) => update({ agent_grant: e.target.checked })}
                  />
                </label>
              )}
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
                    <p>Phone approvals are not available with One click yet. Choose Terminal to use them.</p>
                  )}
                </>
              )}
              {state.mode !== 'finish' && (
                <label className="toggle">
                  <span>
                    Keep a history of this workspace
                    <small>Saved on this computer automatically. Nothing is uploaded.</small>
                  </span>
                  <input
                    type="checkbox"
                    role="switch"
                    checked={profile.records !== false}
                    onChange={(e) => update({ records: e.target.checked })}
                  />
                </label>
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
                  <p className="warning">{voidCopy}</p>
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
              <h2>Check and create</h2>
              {review.workspace && (
                <dl className="current review-facts">
                  <dt>Workspace</dt>
                  <dd>
                    <Raw>{review.workspace.workspace_name ?? review.workspace.name}</Raw>{' '}
                    <small>
                      (tickets <Raw>{review.workspace.prefix}</Raw>-0001…)
                    </small>
                  </dd>
                  <dt>Owner</dt>
                  <dd>
                    <Raw>{review.workspace.name}</Raw>
                  </dd>
                  <dt>Folder</dt>
                  <dd>
                    <Raw>{review.workspace.dir}</Raw>
                  </dd>
                </dl>
              )}
              <ul className="review-summary">
                {summary.lines.map((line, i) => (
                  <li key={i}>
                    <Inline>{line}</Inline>
                  </li>
                ))}
              </ul>
              {(review.risk || review.flags.some((f) => f.id === 'runner' || f.id === 'terminals')) && (
                <div className="warning">
                  <p>
                    {review.flags.some((f) => f.id === 'custody')
                      ? 'One-click approval and dashboard agents act as you on this computer.'
                      : 'Dashboard agents act as you on this computer.'}{' '}
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
                <summary>Everything you approve ({review.items.length + (review.workspace ? 1 : 0)} items)</summary>
                <p>Times and IDs are filled in when you sign.</p>
                {review.workspace && (
                  <section className="review-item">
                    <h3>New workspace</h3>
                    {review.workspace.lines.map((line, i) => (
                      <p key={i}>
                        <Raw>{line}</Raw>
                      </p>
                    ))}
                  </section>
                )}
                {review.items.map((item) => (
                  <section className="review-item" key={item.id}>
                    <h3>
                      <Raw>{item.title}</Raw>
                    </h3>
                    {rowLines(item.lines, item.placeholders).map((line, i) => (
                      <p key={i}>
                        <Raw>{line}</Raw>
                      </p>
                    ))}
                    {item.note && (
                      <p>
                        <Raw>{item.note}</Raw>
                      </p>
                    )}
                    {rowLines(item.details, item.placeholder_details).map((line, i) => (
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
                  <h2>One more confirmation after you click</h2>
                  <ol>
                    <li>Click {state.mode === 'finish' ? 'Finish setup' : 'Create workspace'} below.</li>
                    <li>Return to the terminal window where you started orch setup. Leave this page open to compare the three words.</li>
                    <li>
                      Check that the terminal shows the same words in the same order: <Words words={review.fingerprint} />. If they match, type yes in the terminal
                      and press Enter.
                    </li>
                  </ol>
                  <p>If any word is different, do not type yes. Close the terminal window and run orch setup again.</p>
                </div>
              )}
              {terminalReviewMissing && (
                <p role="alert">Terminal confirmation is missing. Return to your terminal to restart setup.</p>
              )}
              <p>
                Clicking {state.mode === 'finish' ? 'Finish setup' : 'Create workspace'} approves everything listed above
                {review.tty_confirm ? ' and starts the final check.' : ' and starts creating your workspace.'}
              </p>
              <footer>
                <button
                  className="primary"
                  disabled={terminalReviewMissing}
                  onClick={() =>
                    void act(async () => {
                      const result = await api.call<{ run_id: string }>('create', {
                        review_id: review.review_id,
                        digest: review.digest,
                      })
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
              <ConfirmWords words={review.fingerprint} />
            )}
          {review?.tty_confirm && !progress?.done && progress?.steps.some((s) => s.status === 'doing' || s.status === 'done') && (
            <p role="status" className="words-confirmed">
              The three words matched. Your choices are confirmed.
            </p>
          )}
          {progress?.waiting && title !== 'Confirm in your terminal' && (
            progress.waiting === 'waiting for terminal' ? (
              <TerminalNotice />
            ) : (
            <p role="status">
              {progress.waiting === 'runner' || progress.waiting === 'waiting for runner'
                  ? 'Waiting for the agent runner to connect…'
                  : <Raw>{progress.waiting}</Raw>}
            </p>
            )
          )}
          <ol className="progress">
            {progress?.steps.map((s) => (
              <li key={s.id}>
                <span>{s.label}</span>
                <strong className={`status ${s.status}`} aria-label={`${s.label}: ${statusWord[s.status]}`}>
                  <span aria-hidden="true">{statusIcon[s.status]}</span> {statusWord[s.status]}
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
                <Inline>{progress.error.message}</Inline>
              </p>
              {progress.retry.length > 0 && (
                <p>
                  Still to finish:{' '}
                  {progress.retry.map((id) => state?.labels?.[id] ?? 'a setup step').join(', ')}
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
            <button className="primary open-workspace" disabled={busy} onClick={() => void act(finish)}>
              Open your workspace →
            </button>
          )}
        </section>
      )}
      {!ended && !run && state && !state.complete && (
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
        <Reason reason={reason} />
      </main>,
    )
  }
}
