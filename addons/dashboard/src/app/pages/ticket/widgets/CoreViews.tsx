import { ArrowDown, ArrowUp, Check, Circle, CircleDot, Info, Loader, Minus, MinusCircle, StickyNote, TriangleAlert, X } from 'lucide-react'
import { cn } from '@/lib/utils'
import { gateOf, roleOf, seriesData, type Role } from './coreTypes'
import type { WidgetSpec } from './parse'

// The catalog's newer core types, drawn by core: plain HTML and inline SVG, no script, colours from the token palette
// (--chart-1..4 for series, the status tokens for roles and states). Meaning never by colour alone: every state has a
// glyph and a word, every series a name in the legend and at its line's end. Data was validated by coreTypes.ts first.

type Obj = Record<string, unknown>
const STROKE = ['stroke-chart-1', 'stroke-chart-2', 'stroke-chart-3', 'stroke-chart-4']
const FILL = ['fill-chart-1', 'fill-chart-2', 'fill-chart-3', 'fill-chart-4']
const SWATCH = ['bg-chart-1', 'bg-chart-2', 'bg-chart-3', 'bg-chart-4']
const n = (v: number) => (Number.isInteger(v) ? String(v) : String(Math.round(v * 100) / 100))
const unitOf = (u: unknown) => (u ? ` ${String(u)}` : '')

export const ROLE: Record<Role, { word: string; Icon: typeof Check; text: string; border: string }> = {
  ok: { word: 'OK', Icon: Check, text: 'text-success', border: 'border-l-success' },
  info: { word: 'Info', Icon: Info, text: 'text-info', border: 'border-l-info' },
  warn: { word: 'Warning', Icon: TriangleAlert, text: 'text-warning', border: 'border-l-warning' },
  err: { word: 'Problem', Icon: X, text: 'text-danger', border: 'border-l-danger' },
  neu: { word: 'Note', Icon: StickyNote, text: 'text-text-muted', border: 'border-l-border-strong' },
}
export const GATE = {
  pass: { word: 'Passed', Icon: Check, cls: 'text-success' },
  fail: { word: 'Failed', Icon: X, cls: 'text-danger' },
  skip: { word: 'Skipped', Icon: MinusCircle, cls: 'text-text-muted' },
  running: { word: 'Running', Icon: Loader, cls: 'text-info' },
} as const
export const STEP_STATE = {
  done: { word: 'Done', Icon: Check, cls: 'text-success' },
  current: { word: 'Now', Icon: CircleDot, cls: 'text-brand' },
  next: { word: 'Next', Icon: Circle, cls: 'text-text-muted' },
  failed: { word: 'Failed', Icon: X, cls: 'text-danger' },
} as const
const SEGMENT = { ok: 'bg-success', warn: 'bg-warning', err: 'bg-danger', neu: 'bg-chart-2' } as const
const SEGMENT_WORD = { ok: 'OK', warn: 'Warning', err: 'Problem', neu: '' } as const

function seconds(s: number): string {
  if (s < 60) return `${n(s)} s`
  const m = Math.floor(s / 60)
  return m >= 60 ? `${Math.floor(m / 60)} h ${m % 60} min` : `${m} min ${Math.round(s % 60)} s`
}
const deltaText = (d: unknown) => (typeof d === 'number' ? `${d > 0 ? '+' : d < 0 ? '−' : '±'}${n(Math.abs(d))}` : String(d))
const deltaDir = (d: unknown) => {
  const s = typeof d === 'number' ? d : /^\s*[-−]/.test(String(d)) ? -1 : /^\s*\+/.test(String(d)) ? 1 : 0
  return s > 0 ? ArrowUp : s < 0 ? ArrowDown : Minus
}

/** Text alternatives of the newer types (CoreWidget.widgetText covers the first four). */
export function moreText(spec: WidgetSpec): string | undefined {
  const f = spec.fields
  switch (spec.type) {
    case 'series': {
      const d = seriesData(f)
      const u = unitOf(f.unit)
      return d.lines
        .map((s) => `${s.name}: ${s.values.map((y, i) => `${d.x[i]} ${y === null ? 'no value' : `${n(y)}${u}`}`).join(', ')}`)
        .concat(d.markers.map((m) => `Marker at ${String(m.x)}: ${m.label}`))
        .join('\n')
    }
    case 'spark': {
      const v = f.values as number[]
      return `${String(f.text).replace('{spark}', `[${v.map(n).join(', ')}]`)} (min ${n(Math.min(...v))}, max ${n(Math.max(...v))})`
    }
    case 'stats':
      return (f.items as Obj[]).map((m) => `${String(m.label)}: ${typeof m.value === 'number' ? n(m.value) : String(m.value).trim() || 'no value'}${m.delta !== undefined ? ` (${deltaText(m.delta)})` : ''}${m.role ? `, ${ROLE[roleOf(m.role)].word}` : ''}`).join('\n')
    case 'gates':
      return (f.items as Obj[]).map((g) => `${String(g.name)}: ${GATE[gateOf(g.status)].word}${typeof g.seconds === 'number' ? `, ${seconds(g.seconds)}` : ''}`).join('\n')
    case 'diff':
      return `${String(f.file)}\n${String(f.lines)}`
    case 'callout':
      return `${ROLE[roleOf(f.role)].word}: ${String(f.text)}`
    case 'timeline':
      return (f.items as Obj[]).map((s) => `${String(s.at)} ${String(s.label)}${s.status ? ` (${STEP_STATE[s.status as keyof typeof STEP_STATE].word})` : ''}${s.note ? `: ${String(s.note)}` : ''}`).join('\n')
    case 'progress':
      return progressText(f)
  }
}

function progressText(f: Obj): string {
  const max = f.max as number
  const u = unitOf(f.unit)
  if (typeof f.value === 'number') return `${n(f.value)} of ${n(max)}${u} (${Math.round((f.value / max) * 100)}%)`
  const segs = f.segments as Obj[]
  const sum = segs.reduce((a, s) => a + (s.value as number), 0)
  return `${segs.map((s) => `${String(s.label)}: ${n(s.value as number)}`).join(', ')}; ${n(sum)} of ${n(max)}${u}`
}

export function MoreCoreWidget({ spec }: { spec: WidgetSpec }) {
  const f = spec.fields
  switch (spec.type) {
    case 'series':
      return <SeriesChart spec={spec} />
    case 'spark':
      return <Spark spec={spec} />
    case 'stats':
      return (
        <div className="grid grid-cols-[repeat(auto-fit,minmax(150px,1fr))] gap-2">
          {(f.items as Obj[]).map((m, i) => (
            <Stat key={i} m={m} />
          ))}
        </div>
      )
    case 'gates':
      return (
        <ul className="divide-y divide-border rounded-md border border-border">
          {(f.items as Obj[]).map((g, i) => {
            const st = GATE[gateOf(g.status)]
            return (
              <li key={i} className="grid grid-cols-[5.5rem_1fr_auto] items-baseline gap-2 px-2 py-1 text-[12px]">
                <span className={cn('inline-flex items-center gap-1 font-medium', st.cls)}>
                  <st.Icon className="size-3" aria-hidden />
                  {st.word}
                </span>
                <span className="truncate text-text">{String(g.name)}</span>
                <span className="tabular-nums text-text-muted">{typeof g.seconds === 'number' ? seconds(g.seconds) : ''}</span>
              </li>
            )
          })}
        </ul>
      )
    case 'diff':
      return <Diff f={f} />
    case 'callout': {
      const c = ROLE[roleOf(f.role)]
      return (
        <div data-callout={roleOf(f.role)} className={cn('flex gap-2 rounded-md border border-l-4 border-border bg-bg px-3 py-2 text-[13px]', c.border)}>
          <c.Icon className={cn('mt-0.5 size-4 shrink-0', c.text)} aria-hidden />
          <p className="whitespace-pre-wrap text-text">
            <span className="font-semibold">{c.word}: </span>
            {String(f.text)}
          </p>
        </div>
      )
    }
    case 'timeline':
      return (
        <ol className="space-y-1.5 text-[12px]">
          {(f.items as Obj[]).map((s, i) => {
            const st = s.status ? STEP_STATE[s.status as keyof typeof STEP_STATE] : undefined
            const Icon = st?.Icon ?? Circle
            return (
              <li key={i} className="grid grid-cols-[1rem_1fr] gap-x-1.5">
                <Icon className={cn('mt-0.5 size-3', st?.cls ?? 'text-text-faint')} aria-hidden />
                <div className="min-w-0">
                  <div className="flex flex-wrap items-baseline gap-x-2">
                    <time className="font-mono text-[11px] text-text-muted">{String(s.at).replace('T', ' ')}</time>
                    <span className="font-medium text-text">{String(s.label)}</span>
                    {st && <span className={cn('text-[11px]', st.cls)}>{st.word}</span>}
                  </div>
                  {typeof s.note === 'string' && <p className="text-text-muted">{s.note}</p>}
                </div>
              </li>
            )
          })}
        </ol>
      )
    case 'progress':
      return <Progress f={f} title={spec.title} />
  }
  return null
}

function Stat({ m }: { m: Obj }) {
  const role = m.role ? ROLE[roleOf(m.role)] : undefined
  const Arrow = deltaDir(m.delta)
  return (
    <div className={cn('rounded-md border border-border bg-bg px-3 py-2', role && 'border-l-4', role?.border)}>
      <div className="flex items-baseline gap-1.5 text-[11px] text-text-muted">
        <span className="truncate">{String(m.label)}</span>
        {role && (roleOf(m.role) === 'ok' || roleOf(m.role) === 'warn' || roleOf(m.role) === 'err') && <span className={cn('shrink-0', role.text)}>{role.word}</span>}
      </div>
      <div className="mt-0.5 text-xl font-semibold tabular-nums tracking-tight text-text">{typeof m.value === 'number' ? n(m.value) : String(m.value).trim() || '—'}</div>
      {m.delta !== undefined && (
        <div className="mt-0.5 inline-flex items-center gap-1 text-[11px] tabular-nums text-text-muted">
          <Arrow className="size-3" aria-hidden />
          {deltaText(m.delta)}
        </div>
      )}
    </div>
  )
}

function Progress({ f, title }: { f: Obj; title?: string }) {
  const max = f.max as number
  const segs: { label: string; value: number; status: keyof typeof SEGMENT }[] =
    typeof f.value === 'number' ? [{ label: 'Done', value: f.value, status: 'ok' }] : (f.segments as Obj[]).map((s) => ({ label: String(s.label), value: s.value as number, status: (s.status as keyof typeof SEGMENT) ?? 'neu' }))
  return (
    <div className="space-y-1.5">
      <div role="img" aria-label={`${title ?? 'Progress'}: ${progressText(f)}`} className="flex h-3 overflow-hidden rounded-full bg-surface-3">
        {segs.map((s, i) => (
          <div key={i} data-segment={s.label} className={cn('h-full border-r border-bg last:border-r-0', SEGMENT[s.status])} style={{ width: `${(s.value / max) * 100}%` }} />
        ))}
      </div>
      <div className="flex flex-wrap gap-x-3 gap-y-0.5 text-[12px] tabular-nums text-text-muted">
        {typeof f.value === 'number' ? (
          <span className="text-text">{progressText(f)}</span>
        ) : (
          segs.map((s, i) => (
            <span key={i} className="inline-flex items-center gap-1">
              <span className={cn('size-2 rounded-sm', SEGMENT[s.status])} aria-hidden />
              {s.label}: <span className="text-text">{n(s.value)}</span>
              {SEGMENT_WORD[s.status] && <span>({SEGMENT_WORD[s.status]})</span>}
            </span>
          ))
        )}
        {typeof f.value !== 'number' && (
          <span>
            of {n(max)}
            {unitOf(f.unit)}
          </span>
        )}
      </div>
    </div>
  )
}

export function Diff({ f }: { f: Obj }) {
  const lines = String(f.lines).split('\n')
  return (
    <div className="overflow-hidden rounded-md border border-border">
      <div className="border-b border-border bg-surface-2 px-2 py-1 font-mono text-[11px] text-text-muted">{String(f.file)}</div>
      {/* `relative`: the lines' sr-only labels are absolutely positioned; anchored here they stay inside this scroller
          (and inside main) instead of stretching an outer box. */}
      <pre className="relative overflow-x-auto bg-bg py-1 font-mono text-[12px] leading-5">
        {lines.map((l, i) => {
          const kind = l.startsWith('@@') ? 'hunk' : l.startsWith('+') ? 'add' : l.startsWith('-') ? 'del' : 'ctx'
          return (
            <div key={i} data-line={kind} className={cn('px-2', kind === 'add' && 'bg-success-soft text-text', kind === 'del' && 'bg-danger-soft text-text', kind === 'hunk' && 'text-info', kind === 'ctx' && 'text-text-muted')}>
              {kind === 'add' && <span className="sr-only">added: </span>}
              {kind === 'del' && <span className="sr-only">removed: </span>}
              {l || ' '}
            </div>
          )
        })}
      </pre>
    </div>
  )
}

/** widgets.md `spark`: a word-sized line inside a sentence, at the `{spark}` placeholder. */
function Spark({ spec }: { spec: WidgetSpec }) {
  const v = spec.fields.values as number[]
  const [before, after] = String(spec.fields.text).split('{spark}')
  const W = 80
  const H = 18
  const lo = Math.min(...v)
  const hi = Math.max(...v)
  const X = (i: number) => 2 + (i / (v.length - 1)) * (W - 4)
  const Y = (y: number) => (hi === lo ? H / 2 : H - 2 - ((y - lo) / (hi - lo)) * (H - 4))
  return (
    <p className="text-[13px] text-text">
      {before}
      <svg role="img" viewBox={`0 0 ${W} ${H}`} width={W} height={H} className="mx-1 inline-block align-middle">
        <title>{`${v.map(n).join(', ')} (min ${n(lo)}, max ${n(hi)})`}</title>
        <polyline points={v.map((y, i) => `${X(i)},${Y(y)}`).join(' ')} className="fill-none stroke-chart-1" strokeWidth={1.5} />
        <circle cx={X(v.length - 1)} cy={Y(v[v.length - 1])} r={2} className="fill-chart-1" />
      </svg>
      {after}
    </p>
  )
}

const LW = 480
const LH = 190
const PAD = { l: 40, r: 70, t: 10, b: 24 }
const MAX_TICKS = 12

/** Up to 12 round ticks from the step at or below lo to the step at or above hi, built by index (never by adding). */
export function niceTicks(lo: number, hi: number): number[] {
  if (!(hi > lo) || !Number.isFinite(hi - lo)) return [lo]
  const raw = (hi - lo) / 3
  const mag = 10 ** Math.floor(Math.log10(raw))
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) ?? raw
  if (!(step > 0) || !Number.isFinite(step)) return [lo, hi]
  const start = Math.floor(lo / step) * step
  const count = Math.min(MAX_TICKS, Math.ceil((hi - start) / step - 1e-9) + 1)
  // Round to the step's own decimals (plus one), so 1e-7 steps stay distinct and 0.1 + 0.2 reads 0.3.
  const decimals = Math.min(20, Math.max(0, Math.ceil(-Math.log10(step)) + 1))
  const out: number[] = []
  for (let i = 0; i < count; i++) out.push(Number((start + i * step).toFixed(decimals)))
  return out
}

function SeriesChart({ spec }: { spec: WidgetSpec }) {
  const d = seriesData(spec.fields)
  const { x, lines, markers, numeric } = d
  const ys = lines.flatMap((s) => s.values.filter((y): y is number => y !== null))
  const ticks = niceTicks(Math.min(0, ...ys), Math.max(...ys))
  const lo = Math.min(ticks[0], ...ys)
  const hi = Math.max(ticks[ticks.length - 1], ...ys)
  const xn = x as number[]
  const x0 = numeric ? Math.min(...xn) : 0
  const x1 = numeric ? Math.max(...xn) : x.length - 1
  const XV = (v: number) => PAD.l + ((v - x0) / (x1 - x0 || 1)) * (LW - PAD.l - PAD.r)
  const X = (i: number) => XV(numeric ? xn[i] : i)
  const Y = (y: number) => LH - PAD.b - ((y - lo) / (hi - lo || 1)) * (LH - PAD.t - PAD.b)
  const unit = unitOf(spec.fields.unit)
  // A marker outside the data's x range is valid but not drawn (it stays in the text alternative).
  const shownMarkers = numeric ? markers.filter((m) => (m.x as number) >= x0 && (m.x as number) <= x1) : markers
  const labelIdx = numeric ? [] : [...new Set([0, Math.floor((x.length - 1) / 2), x.length - 1])]
  const xLabels: [number, string][] = numeric ? [...new Set([x0, x1])].map((v) => [XV(v), n(v)]) : labelIdx.map((i) => [X(i), String(x[i])])
  return (
    <div className="space-y-1">
      <svg role="img" viewBox={`0 0 ${LW} ${LH}`} width="100%" style={{ maxWidth: LW * 1.5 }} className="block">
        <title>{`${spec.title ?? 'Series'}: ${moreText(spec)}`}</title>
        {ticks.map((t, i) => (
          <g key={i}>
            <line x1={PAD.l} x2={LW - PAD.r} y1={Y(t)} y2={Y(t)} className="stroke-border" strokeWidth={1} />
            <text x={PAD.l - 6} y={Y(t) + 3} textAnchor="end" className="fill-text-muted text-[10px]">
              {n(t)}
            </text>
          </g>
        ))}
        {xLabels.map(([px, label], i) => (
          <text key={i} x={px} y={LH - 6} textAnchor={i === 0 ? 'start' : i === xLabels.length - 1 ? 'end' : 'middle'} className="fill-text-muted text-[10px]">
            {label}
          </text>
        ))}
        {shownMarkers.map((m, i) => {
          const at = numeric ? XV(m.x as number) : X(x.findIndex((xv) => xv === m.x))
          return (
            <g key={i} data-marker={m.label}>
              <line x1={at} x2={at} y1={PAD.t} y2={LH - PAD.b} className="stroke-text-faint" strokeDasharray="3 3" strokeWidth={1} />
              <text x={at + 3} y={PAD.t + 9 + i * 11} className="fill-text-muted text-[10px]">
                {m.label}
              </text>
            </g>
          )
        })}
        {lines.map((s, si) => {
          // A null is a gap: the line breaks there instead of drawing through it.
          const runs: string[][] = [[]]
          s.values.forEach((y, i) => (y === null ? runs.push([]) : runs[runs.length - 1].push(`${X(i)},${Y(y)}`)))
          const lastI = s.values.map((y, i) => (y === null ? -1 : i)).filter((i) => i >= 0).pop()!
          return (
            <g key={s.name} data-series={s.name}>
              {runs
                .filter((r) => r.length)
                .map((r, ri) => (r.length === 1 ? <circle key={ri} cx={r[0].split(',')[0]} cy={r[0].split(',')[1]} r={2} className={FILL[si]} /> : <polyline key={ri} points={r.join(' ')} className={cn('fill-none', STROKE[si])} strokeWidth={2} />))}
              <text x={X(lastI) + 5} y={Y(s.values[lastI]!) + 3} className="fill-text text-[10px]">
                {`${n(s.values[lastI]!)}${unit}`}
              </text>
            </g>
          )
        })}
      </svg>
      {lines.length > 1 && (
        <ul className="flex flex-wrap gap-x-3 text-[11px] text-text-muted">
          {lines.map((s, si) => (
            <li key={s.name} className="inline-flex items-center gap-1">
              <span className={cn('h-0.5 w-3 rounded', SWATCH[si])} aria-hidden />
              {s.name}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
