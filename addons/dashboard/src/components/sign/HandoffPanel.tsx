// The hand-off panel (design 4.1): where the dashboard stops and the terminal begins. The dashboard is not where you
// sign. It shows the command to run, the fingerprint to compare, and an informational preview rendered ONLY from the
// host's `material` (never from a cached ticket) through visible.tsx, so every value is exact and nothing is hidden.
import { SignContent, SignDetails } from './SignContent'
import { Check, Copy, Terminal } from 'lucide-react'
import { useEffect, useState, type ReactNode } from 'react'
import { confirmWord, sequenceWord, signCommand, workspaceConfirmWord, type HandoffMaterial, type HandoffRequest } from '@/api/handoff'
import { Button } from '@/components/ui/button'
import { Prose, Raw, RawValue, plain } from './visible'

/** Core's words for what an event is, shown above its exact values. */
const EVENT_WORDS: Record<string, string> = {
  'gate.approved': 'Approval',
  'gate.changes_requested': 'Request for changes',
  'verdict.given': 'Verdict',
  'question.answered': 'Answer',
  'log.added': 'Comment',
  'ticket.updated': 'Change to the ticket',
  'ticket.closed': 'Close',
  'ticket.reopened': 'Reopen',
  'ticket.created': 'New ticket',
  'settings.changed': 'Change to the workspace settings',
  'policy.changed': 'Change to a gate policy',
  'member.added': 'Add a member',
  'role.changed': 'Change of a member\'s role',
  'member.removed': 'Remove a member',
  'grant.revoked': 'Revoke an agent grant',
  'addon.granted': 'Turn on an add-on',
  'addon.disabled': 'Turn off an add-on',
}

/** The tail of the ticket's log id, shown by the dashboard and by the signer so the two reviews read alike. */
export const uidFragment = (uid: string): string => uid.slice(-8)

/** Any JSON value, exactly: text as the person reads it, numbers and booleans bare, nothing cut. */
function Value({ value, depth = 0 }: { value: unknown; depth?: number }): ReactNode {
  // A single-line string is quoted and a number or boolean is bare, so "1" and 1 never read alike (visibleValue).
  if (typeof value === 'string') return value.includes('\n') ? <Prose className="text-text">{value}</Prose> : <RawValue value={value} />
  if (value === null || typeof value !== 'object') return <RawValue value={value} />
  if (Array.isArray(value)) {
    if (!value.length) return <span className="text-text-muted">none</span>
    return (
      <ul className="space-y-1">
        {value.map((v, i) => (
          <li key={i}>
            <Value value={v} depth={depth + 1} />
          </li>
        ))}
      </ul>
    )
  }
  const entries = Object.entries(value as Record<string, unknown>).sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
  if (!entries.length) return <span className="text-text-muted">none</span>
  return (
    <dl className="space-y-1">
      {entries.map(([k, v]) => (
        <div key={k} className="grid grid-cols-[minmax(0,9rem)_minmax(0,1fr)] gap-x-3">
          <dt className="text-text-muted">
            <Raw>{k}</Raw>
          </dt>
          <dd className="min-w-0">
            <Value value={v} depth={depth + 1} />
          </dd>
        </div>
      ))}
    </dl>
  )
}

function Block({ title, children }: { title: ReactNode; children: ReactNode }) {
  return (
    <section className="space-y-1.5 border-t border-border pt-2 first:border-t-0 first:pt-0">
      <h3 className="text-[12px] font-medium text-text-muted">{title}</h3>
      <div className="text-[13px]">{children}</div>
    </section>
  )
}

/** The preview: the host's `material` and nothing else. It says it is a preview and that the terminal shows what is signed. */
export function MaterialPreview({ material, events = [], word = null }: { material: HandoffMaterial; events?: HandoffRequest['events']; word?: string | null }) {
  const total = material.review.length
  return (
    <div data-handoff-preview className="max-h-[38vh] space-y-3 overflow-auto rounded-md border border-border bg-bg p-3">
      <p className="text-[12px] text-text-muted">Preview. Your terminal shows what you sign.</p>
      {material.ticket?.key && (
        <Block title="Ticket">
          <Raw>{material.ticket.key}</Raw>
          <p className="text-[12px] text-text-muted">
            Ticket id <Raw>{material.ticket.uid}</Raw> (signed; its last 8 characters are <Raw>{uidFragment(material.ticket.uid)}</Raw>)
          </p>
          <p className="text-[12px] text-text-muted">
            In your terminal you type <Raw>{sequenceWord(material) ?? confirmWord(material.ticket.key, material.ticket.uid)}</Raw> to continue.
          </p>
        </Block>
      )}
      {!material.ticket && word && (
        <Block title="Workspace operation">
          <p className="text-[12px] text-text-muted">
            In your terminal you type <Raw>{word}</Raw> to continue.
          </p>
        </Block>
      )}
      {material.sequence && (
        <Block title={`One review for ${material.sequence.of} signatures, in this order (the title, then the fields, each written section, each acceptance criterion and the people)`}>
          <Value value={material.sequence.intent} />
        </Block>
      )}
      {material.kind === 'gate' && (
        <Block title={`Gate: ${plain(material.gate ?? '')} (your terminal recomputes the content hash from this)`}>
          <Value value={material.input ?? {}} />
        </Block>
      )}
      {material.policy && Object.keys(material.policy).length > 0 && (
        <Block title="Gate policy (stated by the host, not recomputed)">
          <Value value={material.policy} />
        </Block>
      )}
      {material.kind === 'question' && (
        <Block title="Question">
          <Value value={material.question ?? {}} />
        </Block>
      )}
      {material.review.map((r, n) => (
        <Block key={r.event_id} title={`${EVENT_WORDS[r.type] ?? 'Event'}${total > 1 ? ` (${n + 1} of ${total})` : ''}`}>
          <p className="mb-1 text-[11px] text-text-faint">
            Event type <Raw>{r.type}</Raw>
          </p>
          <Value value={r.payload} />
          {events[n] && (
            <div className="mt-2">
              <p className="text-[11px] text-text-faint">Signed with the event</p>
              <Value value={Object.fromEntries((['based_on', 'gate_gen', 'roster_v'] as const).filter((k) => events[n][k] !== undefined).map((k) => [k, events[n][k]]))} />
            </div>
          )}
          {Object.keys(r.derived).length > 0 && (
            <div className="mt-2">
              <p className="text-[11px] text-text-faint">Stated by the host; your terminal works these out again</p>
              <Value value={r.derived} />
            </div>
          )}
        </Block>
      ))}
      {material.texts && Object.keys(material.texts).length > 0 && (
        <Block title="Section texts">
          <Value value={material.texts} />
        </Block>
      )}
      {material.before && Object.keys(material.before).length > 0 && (
        <Block title="Current values according to the host, not signed">
          <Value value={material.before} />
        </Block>
      )}
    </div>
  )
}

export function formatRemaining(ms: number): string {
  const s = Math.max(0, Math.ceil(ms / 1000))
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`
}

function useRemaining(expiresAt: string): number {
  const end = Date.parse(expiresAt)
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(t)
  }, [])
  return Math.max(0, end - now)
}

/** The command to run, with a copy button. The code names a request; it is not a secret and authorises nothing. */
export function CommandLine({ code }: { code: string }) {
  return <ExactCommandLine command={signCommand(code)} />
}

/** Copy the server's exact command without constructing or signing anything in the browser. */
export function ExactCommandLine({ command, testId = 'handoff-command' }: { command: string; testId?: string }) {
  const [copied, setCopied] = useState(false)
  return (
    <div className="flex items-center gap-2 rounded-md border border-border bg-bg px-3 py-2">
      <Terminal className="size-4 shrink-0 text-text-muted" aria-hidden />
      <code data-testid={testId} className="min-w-0 flex-1 break-all font-mono text-[14px] text-text">
        <Raw>{command}</Raw>
      </code>
      <Button
        type="button"
        variant="ghost"
        size="icon-sm"
        aria-label="Copy command"
        data-testid={testId + '-copy'}
        onClick={() => {
          void navigator.clipboard?.writeText(command).then(
            () => {
              setCopied(true)
              setTimeout(() => setCopied(false), 1500)
            },
            () => {},
          )
        }}
      >
        {copied ? <Check /> : <Copy />}
      </Button>
    </div>
  )
}

/** The open request: command, fingerprint, content hash, countdown and the preview. Cancel lives in the dialog footer. */
export function HandoffPanel({ request, workspace = null }: { request: HandoffRequest; workspace?: { prefix: string; id: string } | null }) {
  const remaining = useRemaining(request.expires_at)
  const { material } = request
  const word = material.ticket ? (sequenceWord(material) ?? confirmWord(material.ticket.key, material.ticket.uid)) : workspaceConfirmWord(request, workspace)
  // Core's own prompt `[r] hand to the orch runner` follows the signature of a runner grant (the access is the signed
  // `label`); an Extend also takes back the access it replaces. Both are read from the SIGNED events, never from a host word.
  const runnerGrant = request.events.some(e => e.type === 'grant.issued' && e.label === 'runner')
  const hostStated = !!Object.keys(material.policy ?? {}).length || !!Object.keys(material.before ?? {}).length || !!Object.keys(material.grants ?? {}).length || material.review.some(r => Object.keys(r.derived).length > 0)
  const preview = <MaterialPreview material={request.material} events={request.events} word={workspaceConfirmWord(request, workspace)} />
  // ONE "Technical details" expander: with core's review it is the content's own (ids), plus these facts and the preview.
  const technical = (
    <>
      <p>The ticket key is host-local, not proven; the signed ticket id identifies the ticket. The requester’s client name is self-declared, unverified.</p>
      <p>Request <Raw>{request.id}</Raw> · Workspace <Raw>{workspace?.id ?? ''}</Raw></p>
      <dl className="grid grid-cols-[auto_1fr] items-baseline gap-x-3 gap-y-1 text-[13px]">
        <dt className="text-text-muted">Verification fingerprint</dt>
        <dd>
          <code className="font-mono text-[14px] text-text">
            {request.fingerprint}
          </code>
          <span className="block text-[12px] text-text-muted">The first 12 hex digits of a fingerprint of the request (a hash over the operation, its review and its events). Your terminal shows the same value on its “fingerprint:” line.</span>
        </dd>
        {request.gate_hash && (
          <>
            <dt className="text-text-muted">Content hash</dt>
            <dd className="min-w-0">
              <Raw>{request.gate_hash}</Raw>
            </dd>
          </>
        )}
        <dt className="text-text-muted">Operation</dt>
        <dd>
          <Raw>{request.op}</Raw>
        </dd>
        {request.origin && (
          <>
            <dt className="text-text-muted">Requested by</dt>
            <dd className="min-w-0 text-[12px]">
              uid <Raw>{String(request.origin.uid)}</Raw> pid <Raw>{String(request.origin.pid)}</Raw> at <Raw>{request.origin.created_at}</Raw>; client <Raw>{request.origin.client}</Raw> (named by the client itself, unverified)
            </dd>
          </>
        )}
        <dt className="text-text-muted">Expires in</dt>
        <dd>
          <span role="timer" data-testid="handoff-expiry" className="font-mono text-text">
            {formatRemaining(remaining)}
          </span>
        </dd>
      </dl>
      {request.content && preview}
    </>
  )
  return (
    <div data-handoff-panel className="space-y-3">
      {request.content && <SignContent content={request.content} ws={workspace?.id} technical={technical} />}
      <p className="text-[13px] text-text">
        Sign in your terminal. This dashboard is not where you sign: it never sees your passphrase or your key.
      </p>
      {runnerGrant && (
        <div data-testid="handoff-runner-note" className="space-y-1 text-[13px] text-text">
          <p>After approving, choose <kbd className="rounded border border-border px-1 font-mono text-[12px]">r</kbd> in Terminal to give the new access to this Mac’s agent runner.</p>
          <p className="text-[12px] text-text-muted">The agent runner is the part of orch on this Mac that starts agents for you.</p>
        </div>
      )}
      {request.code ? <>
        <p className="text-[13px] font-medium text-text">Copy this command into Terminal</p>
        <CommandLine code={request.code} />
      </> : <p className="text-[13px] text-warning">This request has no code any more.</p>}
      <p className="text-[12px] text-text-muted">
        Verification fingerprint: <code data-testid="handoff-fingerprint" className="font-mono text-[13px] text-text">{request.fingerprint}</code>.
        {' '}Check that this matches the fingerprint in your terminal.
      </p>
      {request.content && word && <p className="text-[12px] text-text-muted">In your terminal you type <Raw>{word}</Raw> to continue.</p>}
      {request.op === 'grant' && <p className="text-[12px] text-text-muted" data-testid="grant-handoff-note">After you sign, the terminal asks what to do with the new access. Press r to hand it to the orch runner, otherwise the runner has no access and agents stay off.</p>}
      {request.content && hostStated && <p role="note" className="text-[12px] text-warning">Some values come from orch on this Mac (see details)</p>}
      {!request.content && <SignDetails>{technical}</SignDetails>}
      {!request.content && preview}
      <p className="text-[12px] text-text-muted">Only sign a code you created yourself. This page follows the request and shows when it is signed.</p>
    </div>
  )
}
