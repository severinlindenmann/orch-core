// Agents → Mandates, PREVIEW ONLY (docs/concept-mandates.md §2.11–§2.16, §3; owner decision 10 Oct evening: the wide
// mandate). How it would look: the preflight, issuing a mandate that steers the whole workspace in your name (what it
// may do, what stays yours, a length up to 30 days), the mandate in force (limits, decision log, refused items,
// revisions, Renew, Stop, Revoke and void). Nothing signs; every request goes to the preview endpoint only.
import { Check, CircleSlash } from 'lucide-react'
import { useState } from 'react'
import { DECISION_KIND_LABEL, MANDATE_DAYS, REFUSAL_LABEL, type MandatesPreviewState, type PreviewMandate } from '@/api/mandatesPreview'
import { plain, Raw } from '@/components/sign/visible'
import { Button } from '@/components/ui/button'
import { Label } from '@/components/ui/label'
import { Switch } from '@/components/ui/switch'
import { fmtDateTime, fmtExact, fmtWhen } from '@/lib/time'
import { cn } from '@/lib/utils'
import { DecisionLine, DecisionSubject, PreviewNote, PreviewPrompt, RevokeDialog, StopDialog, useCanMandate, useMandatesOp, weekday } from '../../mandates/shared'
import { Pill, Section } from '../ticket/shared'

const DAY = 86_400_000

export function MandatesTab({ ws, state, now }: { ws: string; state: MandatesPreviewState; now: string }) {
  const owner = useCanMandate()
  const op = useMandatesOp(ws)
  const [issuing, setIssuing] = useState(false)
  const m = state.on ? state.mandate : null
  const missing = state.preflight.filter((c) => c.state !== 'passed').length
  const inForce = m && (m.state === 'active' || m.state === 'stopping')

  const preflight = (
    <Section title="Preflight" aside={<Pill tone={missing ? 'warning' : 'success'}>{missing ? `${missing} of ${state.preflight.length} missing` : 'All pass'}</Pill>}>
      <ul className="space-y-2" aria-label="Prerequisites">
        {state.preflight.map((c) => (
          <li key={c.id} className="flex gap-2 text-[13px]" data-testid={`preflight-${c.id}`}>
            {c.state === 'passed' ? <Check className="mt-0.5 size-4 shrink-0 text-success" aria-label="passed" /> : <CircleSlash className="mt-0.5 size-4 shrink-0 text-text-muted" aria-label="missing" />}
            <div className="min-w-0">
              <p className="font-medium text-text">
                {c.title}: <span className="font-normal text-text-muted">{c.note.replace(/\.$/, '').toLowerCase()}</span>
              </p>
              <p className="text-[12px] text-text-muted">{c.detail}</p>
            </div>
          </li>
        ))}
      </ul>
      {missing > 0 && (
        <p className="mt-3 text-[13px] text-text" data-testid="preflight-blocked">
          Issuing is blocked. A real host refuses every mandate until all four pass (mandate.custody_unsupported); it is never a warning you can click past.
          {state.on && <span className="text-text-muted"> Preview: unblocked here only to show the flow; a real host would refuse.</span>}
        </p>
      )}
      {owner ? (
        <div className="mt-3 flex items-center gap-2 border-t border-border pt-3">
          <Switch id="mandates-anyway" checked={state.on} onCheckedChange={(v) => void op(v ? { op: 'enable' } : { op: 'disable' })} />
          <Label htmlFor="mandates-anyway" className="text-[13px] font-normal">
            Show the mandate anyway (preview)
          </Label>
          <span className="text-[12px] text-text-muted">· only to see how it would look</span>
        </div>
      ) : (
        <p className="mt-3 text-[12px] text-text-muted">Only owners can try the preview.</p>
      )}
    </Section>
  )

  return (
    <div className="space-y-4" data-testid="mandates-tab">
      <PreviewNote />

      {/* The preflight comes first; with a mandate shown it follows the mandate (it still says why a real host refuses). */}
      {!m && preflight}
      {m && <MandateView ws={ws} m={m} now={now} owner={owner} />}

      {!inForce && (
        <Section title="Issue a mandate">
          <p className="text-[13px] text-text-muted">
            One orchestrator steers the whole workspace in your name: it approves waiting agents and gates, unblocks tickets, enables factories and starts their runs, and issues grants. Settings, addons, members and roles, devices, relay pairing, secrets and connections and protected paths stay yours. You pick the length, up to 30 days, and renew it with one new signature on this Mac with Touch ID.
          </p>
          {m && <p className="mt-2 text-[13px] text-text-muted">Mandate {m.id} {m.state === 'revoked' ? 'was revoked' : `was stopped at #${m.stop?.boundary_seq}`}. One mandate at a time in the preview.</p>}
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <Button onClick={() => setIssuing(true)} disabled={!owner || !state.on}>
              Issue a mandate…
            </Button>
            {!state.on && <span className="text-[12px] text-text-muted" data-testid="issue-blocked">Blocked by the preflight.</span>}
          </div>
        </Section>
      )}

      {m && preflight}

      {issuing && <IssueDialog ws={ws} state={state} now={now} onClose={() => setIssuing(false)} />}
    </div>
  )
}

function stateLine(m: PreviewMandate): { text: string; tone: 'success' | 'neutral' | 'danger' | 'info' } {
  switch (m.state) {
    case 'active':
      return { text: 'In force', tone: 'success' }
    case 'stopping':
      return { text: 'Stopping…', tone: 'info' }
    case 'stopped':
      return { text: `Stopped at #${m.stop?.boundary_seq}`, tone: 'neutral' }
    default:
      return { text: 'Revoked', tone: 'danger' }
  }
}

function Meter({ label, value, max, text, reached = value >= max }: { label: string; value: number; max: number; text: string; reached?: boolean }) {
  const v = Math.max(0, Math.min(value, max))
  return (
    <div>
      <div className="mb-1 flex justify-between gap-2 text-[12px] text-text-muted">
        <span>{label}</span>
        <span className="font-mono tabular-nums">
          {text}
          {reached ? ' · limit reached' : ''}
        </span>
      </div>
      <div role="progressbar" aria-label={label} aria-valuemin={0} aria-valuemax={max} aria-valuenow={v} className="h-1.5 overflow-hidden rounded-full bg-surface-2">
        {/* A reached limit is marked (muted, with "limit reached"), not drawn like any other fill. */}
        <div className={cn('h-full rounded-full', reached ? 'bg-text-muted' : 'bg-brand')} style={{ width: `${(v / max) * 100}%` }} />
      </div>
    </div>
  )
}

function MandateView({ ws, m, now, owner }: { ws: string; m: PreviewMandate; now: string; owner: boolean }) {
  const [dialog, setDialog] = useState<'stop' | 'revoke' | 'renew' | null>(null)
  const s = stateLine(m)
  const total = Math.round((Date.parse(m.expires) - Date.parse(m.issued_at)) / DAY)
  const left = Math.max(0, Math.round((Date.parse(m.expires) - Date.parse(now)) / DAY))
  const log = [...m.decisions].reverse()
  return (
    <>
      <Section
        title={
          <span className="flex flex-wrap items-center gap-2">
            Mandate {m.id}
            <Pill tone={s.tone}>{s.text}</Pill>
            <Pill>Preview</Pill>
          </span>
        }
        aside={
          owner &&
          m.state !== 'revoked' && (
            <div className="flex gap-1">
              {m.state === 'active' && (
                <Button size="sm" variant="outline" onClick={() => setDialog('renew')}>
                  Renew…
                </Button>
              )}
              {m.state === 'active' && (
                <Button size="sm" variant="outline" onClick={() => setDialog('stop')}>
                  Stop
                </Button>
              )}
              <Button size="sm" variant="outline" onClick={() => setDialog('revoke')}>
                Revoke and void…
              </Button>
            </div>
          )
        }
      >
        <dl className="grid grid-cols-[120px_minmax(0,1fr)] gap-x-3 gap-y-1 text-[13px]">
          <dt className="text-text-muted">For</dt>
          <dd>{plain(m.issuer)} (issuer, owner)</dd>
          <dt className="text-text-muted">Orchestrator</dt>
          <dd className="min-w-0 truncate">
            <Raw>{m.orchestrator.name}</Raw> · <Raw>{m.orchestrator.identity}</Raw>
          </dd>
          <dt className="text-text-muted">Checker</dt>
          <dd>
            <Raw>{m.checker.identity}</Raw> (host-provisioned)
          </dd>
          <dt className="text-text-muted">Scope</dt>
          <dd>Steers the whole workspace in your name, except what stays yours (settings, addons, members and roles, devices, relay pairing, secrets and connections, protected paths)</dd>
          <dt className="text-text-muted">In force</dt>
          <dd>
            {fmtDateTime(m.issued_at)} until {weekday(m.expires)} {fmtExact(m.expires)} · {m.days} {m.days === 1 ? 'day' : 'days'} · renewable · revision {m.revision}
          </dd>
        </dl>
        {m.state === 'stopping' && <p className="mt-3 text-[13px] text-text-muted" role="status">Stopping… waiting for the host to acknowledge.</p>}
        {m.state === 'stopped' && (
          <p className="mt-3 text-[13px] text-text" role="status">
            Stopped at #{m.stop?.boundary_seq}: nothing after it is signed.{m.stop?.stop_agents ? ' Also stop agents was asked (preview: no agent was stopped).' : ''}
          </p>
        )}
        {m.state === 'revoked' && (
          <p className="mt-3 text-[13px] text-text" role="status">
            Revoked {m.revoked_at ? fmtWhen(m.revoked_at, now) : ''}: {m.decisions.filter((d) => d.voided).length} decisions voided, {m.decisions.filter((d) => d.landed).length} on landed work listed for your review.
          </p>
        )}

        <h4 className="mb-2 mt-4 text-[12px] font-semibold text-text">Limits</h4>
        <div className="grid grid-cols-1 gap-3 @[40rem]/page:grid-cols-2" data-testid="mandate-limits">
          <Meter label="Decisions" value={m.limits.decisions.used} max={m.limits.decisions.max} text={`${m.limits.decisions.used} / ${m.limits.decisions.max}`} />
          <Meter label="Days left" value={left} max={total} text={`${left} of ${total}`} reached={left <= 0} />
          <Meter label="Grants issued" value={m.limits.grants.used} max={m.limits.grants.max} text={`${m.limits.grants.used} / ${m.limits.grants.max}`} />
        </div>
        <p className="mt-2 text-[12px] text-text-muted">Money is not shown as a limit until the usage addon can enforce it.</p>
      </Section>

      <Section title={`Decision log · ${m.decisions.length}`} className="[&>div]:p-0">
        <ul aria-label="Decision log" data-testid="mandate-log">
          {log.map((d) => (
            <li key={d.id} className={cn('flex min-w-0 items-start gap-3 border-b border-border px-3 py-2 text-[13px] last:border-b-0', d.voided && 'text-text-muted line-through decoration-text-faint')}>
              <span className="w-12 shrink-0 font-mono text-[12px] text-text-muted">#{d.seq}</span>
              <DecisionSubject d={d} className="w-[84px] font-mono text-[12px]" />
              <DecisionLine m={m} d={d} className="flex-1 text-[13px]" />
              <span className="shrink-0 text-[12px] text-text-muted">{d.voided ? 'Voided' : d.review === 'veto' ? 'Vetoed' : d.landed ? 'Landed' : d.review === 'looks_right' ? 'Looked right' : 'New'}</span>
            </li>
          ))}
        </ul>
      </Section>

      <Section title={`Refused or skipped · ${m.refused.length}`} className="[&>div]:p-0">
        <ul aria-label="Refused or skipped" data-testid="mandate-refused">
          {m.refused.map((r) => (
            <li key={r.id} className="flex min-w-0 items-start gap-3 border-b border-border px-3 py-2 text-[13px] last:border-b-0">
              <Pill className="shrink-0">{REFUSAL_LABEL[r.reason]}</Pill>
              <DecisionSubject d={r} className="w-[84px] font-mono text-[12px]" />
              <span className="min-w-0 flex-1">
                <span className="block">{r.detail}</span>
                <span className="block text-[12px] text-text-muted">
                  {r.kind ? DECISION_KIND_LABEL[r.kind] : plain(r.asked ?? '')} · {fmtWhen(r.at, now)} · {r.ticket ? 'goes to you as a normal “needs you” item' : 'refused by the host; nothing happened'}
                </span>
              </span>
            </li>
          ))}
        </ul>
      </Section>

      <Section title="Revisions">
        <ul className="space-y-1 text-[13px]">
          {m.revisions.map((r) => (
            <li key={r.revision}>
              <span className="font-medium">Revision {r.revision}</span> · {fmtDateTime(r.at)} · <span className="text-text-muted">{r.what}</span>
            </li>
          ))}
        </ul>
        <p className="mt-2 text-[12px] text-text-muted">Renewing is one new signature: a new revision with a new length from then, the same scope and the lifetime counters kept.</p>
      </Section>

      {dialog === 'renew' && <RenewDialog ws={ws} m={m} now={now} onClose={() => setDialog(null)} />}
      {dialog === 'stop' && <StopDialog ws={ws} m={m} onClose={() => setDialog(null)} />}
      {dialog === 'revoke' && <RevokeDialog ws={ws} m={m} onClose={() => setDialog(null)} />}
    </>
  )
}

const until = (now: string, days: number) => new Date(Date.parse(now) + days * DAY).toISOString().replace(/\.\d{3}Z$/, 'Z')
const daysWord = (d: number) => `${d} ${d === 1 ? 'day' : 'days'}`

function DaysPicker({ id, value, onChange }: { id: string; value: number; onChange: (d: number) => void }) {
  return (
    <div className="space-y-1">
      <Label htmlFor={id}>Length</Label>
      <select id={id} value={value} onChange={(ev) => onChange(Number(ev.target.value))} className="h-9 w-full rounded-md border border-border bg-bg px-2 text-[13px]">
        {MANDATE_DAYS.map((d) => (
          <option key={d} value={d}>
            {daysWord(d)}
            {d === 30 ? ' (longest)' : ''}
          </option>
        ))}
      </select>
    </div>
  )
}

/** Issue a mandate (core dialog, preview): the orchestrator and the length are chosen; the scope is the owner's decision. */
function IssueDialog({ ws, state, now, onClose }: { ws: string; state: MandatesPreviewState; now: string; onClose: () => void }) {
  const op = useMandatesOp(ws)
  const [orch, setOrch] = useState(state.orchestrators[0]?.session ?? '')
  const [days, setDays] = useState(7)
  const o = state.orchestrators.find((x) => x.session === orch)
  return (
    <PreviewPrompt
      title="Issue a mandate"
      covers={[
        o ? <>Orchestrator: <Raw>{o.name}</Raw> (session <Raw>{o.session}</Raw>)</> : 'Orchestrator: none picked',
        'Steers the whole workspace in your name',
        'Always yours: settings, addons, members and roles, devices, relay pairing, secrets and connections, protected paths',
        'It never issues another mandate; grants it issues stay inside this mandate',
        `Duration: ${daysWord(days)}, until ${fmtExact(until(now, days))} · renewable with one new signature`,
        'For you: every decision reads “via mandate, for you — no person reviewed this”',
      ]}
      confirmLabel="Sign mandate (preview — nothing is signed)"
      disabled={!o}
      onConfirm={() => {
        onClose()
        void op({ op: 'issue', orchestrator: orch, days }, 'Mandate issued')
      }}
      onClose={onClose}
    >
      <div className="grid grid-cols-2 gap-3">
        <div className="space-y-1">
          <Label htmlFor="mandate-orch">Orchestrator</Label>
          <select id="mandate-orch" value={orch} onChange={(ev) => setOrch(ev.target.value)} className="h-9 w-full rounded-md border border-border bg-bg px-2 text-[13px]">
            {state.orchestrators.map((x) => (
              <option key={x.session} value={x.session}>
                {x.name} · {x.session}
              </option>
            ))}
          </select>
        </div>
        <DaysPicker id="mandate-days" value={days} onChange={setDays} />
      </div>
      <section aria-label="May do" className="space-y-1">
        <h3 className="text-[12px] font-semibold text-text">Steers the whole workspace in your name. It may:</h3>
        <ul className="list-disc space-y-0.5 pl-4 text-[12px] text-text" data-testid="mandate-may">
          {state.may.map((n) => (
            <li key={n}>{n}</li>
          ))}
        </ul>
      </section>
      <section aria-label="Always yours" className="space-y-1">
        <h3 className="text-[12px] font-semibold text-text">Always yours, by any path:</h3>
        <ul className="list-disc space-y-0.5 pl-4 text-[12px] text-text" data-testid="mandate-always-human">
          {state.always_human.map((n) => (
            <li key={n}>{n}</li>
          ))}
        </ul>
      </section>
      <section aria-label="Protected paths" className="space-y-1">
        <h3 className="text-[12px] font-semibold text-text">Protected paths (a person approves these, always):</h3>
        <ul className="list-disc space-y-0.5 pl-4 text-[12px] text-text-muted" data-testid="mandate-protected">
          {state.protected_paths.map((p) => (
            <li key={p}>{p}</li>
          ))}
        </ul>
      </section>
      <p className="text-[12px] text-text-muted">A factory run it starts with a Deliver target goes out after its hold window unless a person stops it: the hold notice is the only checkpoint there.</p>
    </PreviewPrompt>
  )
}

/** Renew (core dialog, preview): one new signature, a new length from now, the same scope. */
function RenewDialog({ ws, m, now, onClose }: { ws: string; m: PreviewMandate; now: string; onClose: () => void }) {
  const op = useMandatesOp(ws)
  const [days, setDays] = useState(m.days)
  return (
    <PreviewPrompt
      title={`Renew mandate ${plain(m.id)}`}
      covers={[
        <>Mandate <Raw>{m.id}</Raw>, for {plain(m.issuer)}, whole workspace · revision {m.revision + 1}</>,
        `New end: ${daysWord(days)} from now, until ${fmtExact(until(now, days))} (was ${fmtExact(m.expires)})`,
        'Same scope and the same always-yours list; the lifetime counters are kept',
      ]}
      confirmLabel="Renew mandate (preview — nothing is signed)"
      onConfirm={() => {
        onClose()
        void op({ op: 'renew', days }, `Mandate ${plain(m.id)} renewed for ${daysWord(days)}`)
      }}
      onClose={onClose}
    >
      <DaysPicker id="mandate-renew-days" value={days} onChange={setDays} />
    </PreviewPrompt>
  )
}
