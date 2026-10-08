import { CheckCircle2, CircleDashed, ChevronRight, MessageSquareWarning, ShieldAlert, ShieldCheck, ShieldX } from 'lucide-react'
import type { GateName, GateStatus, TicketDocument } from '@/api/types'
import { GATE_LABEL, policyText } from './actions'
import { fmtTime, Mono, Pill, shortHash, type Viewer } from './shared'

const GATES: GateName[] = ['requirements', 'plan', 'verify']

const STATE: Record<GateStatus['state'], { label: string; tone: 'success' | 'warning' | 'danger' | 'neutral'; Icon: typeof CheckCircle2 }> = {
  approved: { label: 'Approved', tone: 'success', Icon: CheckCircle2 },
  pending: { label: 'Pending', tone: 'neutral', Icon: CircleDashed },
  invalidated: { label: 'Invalidated', tone: 'warning', Icon: ShieldAlert },
  changes_requested: { label: 'Changes requested', tone: 'danger', Icon: MessageSquareWarning },
}

const VIA: Record<string, string> = { cli: 'CLI', dashboard: 'dashboard', phone: 'phone' }
const PRESENCE: Record<string, string> = { touchid: 'Touch ID', passkey: 'passkey', password: 'password' }

function Gate({ name, gate, viewer }: { name: GateName; gate: GateStatus; viewer: Viewer }) {
  const s = STATE[gate.state]
  return (
    <section className="min-w-0 flex-1 rounded-lg border border-border bg-surface p-3" data-testid={`gate-${name}`} data-state={gate.state}>
      <div className="flex items-center gap-2">
        <h3 className="text-[13px] font-semibold">{GATE_LABEL[name]}</h3>
        <Pill tone={s.tone} className="ml-auto">
          <s.Icon />
          {s.label}
        </Pill>
      </div>
      <p className="mt-1 text-[12px] text-text-muted">{policyText(gate)}</p>

      {gate.state === 'invalidated' && gate.reason && (
        <p className="mt-2 flex items-start gap-1.5 rounded-md border border-warning/30 bg-warning-soft px-2 py-1.5 text-[12px] text-warning">
          <ShieldX className="mt-0.5 size-3.5 shrink-0" />
          {gate.reason}
        </p>
      )}
      {gate.state === 'changes_requested' && gate.note && (
        <p className="mt-2 rounded-md border border-danger/30 bg-danger-soft px-2 py-1.5 text-[12px] text-danger">{gate.note}</p>
      )}

      {gate.approvals.length > 0 ? (
        <ul className="mt-2 space-y-1.5">
          {gate.approvals.map((a, i) => (
            <li key={i} className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[12px]">
              <ShieldCheck className={gate.state === 'invalidated' ? 'size-3.5 text-text-faint' : 'size-3.5 text-success'} />
              <span className={gate.state === 'invalidated' ? 'text-text-muted line-through decoration-text-faint' : 'text-text'}>{viewer.name(a.by)}</span>
              <span className="text-text-faint">
                {fmtTime(a.at)} · via {VIA[a.via ?? 'cli']} · {PRESENCE[a.presence ?? 'touchid']}
              </span>
              <span className={a.sig_ok === false ? 'text-danger' : 'text-success'}>{a.sig_ok === false ? 'signature failed' : 'sig ok'}</span>
            </li>
          ))}
        </ul>
      ) : (
        <p className="mt-2 text-[12px] text-text-faint">0 of {gate.needed} approvals</p>
      )}
      {gate.hash && (
        <p className="mt-2 truncate text-[11px] text-text-faint" title={gate.hash}>
          hash <Mono className="text-[11px]">{shortHash(gate.hash, 12)}</Mono>
        </p>
      )}
    </section>
  )
}

export function GatesStrip({ ticket, viewer }: { ticket: TicketDocument; viewer: Viewer }) {
  return (
    <div role="group" aria-label="Gates" className="flex items-stretch gap-1.5" data-testid="gates-strip">
      {GATES.map((g, i) => (
        <div key={g} className="flex min-w-0 flex-1 items-stretch gap-1.5">
          <Gate name={g} gate={ticket.gates[g]} viewer={viewer} />
          {i < GATES.length - 1 && <ChevronRight className="size-4 shrink-0 self-center text-text-faint" aria-hidden />}
        </div>
      ))}
    </div>
  )
}
