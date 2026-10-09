import { ArrowDown, ArrowUp, Check, Circle, CircleDot, Gavel, Info, Loader, Minus, MinusCircle, TriangleAlert, X } from 'lucide-react'
import { cn } from '@/lib/utils'
import type { WidgetSpec } from './parse'

// The catalog's newer core types, drawn by core: plain HTML and inline SVG, no script, colours from the token palette
// (the --chart palette for series, the status tokens for states). Meaning never by colour alone: every state has a glyph and a
// word, every series a name in the legend and at its line's end. Data was validated by coreTypes.ts before it gets here.

type Obj = Record<string, unknown>
// Series colours from the chart palette. The fourth palette slot is left out: the orange guard (src/test/guards.test.ts)
// reserves that token name, so a line takes at most four series.
const STROKE = ['stroke-chart-1', 'stroke-chart-2', 'stroke-chart-3', 'stroke-chart-5']
const FILL = ['fill-chart-1', 'fill-chart-2', 'fill-chart-3', 'fill-chart-5']
const SWATCH = ['bg-chart-1', 'bg-chart-2', 'bg-chart-3', 'bg-chart-5']
const n = (v: number) => (Number.isInteger(v) ? String(v) : String(Math.round(v * 100) / 100))
const unitOf = (u: unknown) => (u ? ` ${String(u)}` : '')

export const LINE_STATE = {
  ok: { word: 'OK', Icon: Check, cls: 'text-success' },
  warn: { word: 'Warning', Icon: TriangleAlert, cls: 'text-warning' },
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
const SEGMENT = { ok: 'bg-success', warn: 'bg-warning', fail: 'bg-danger', neutral: 'bg-chart-2' } as const
const SEGMENT_WORD = { ok: 'OK', warn: 'Warning', fail: 'Failed', neutral: '' } as const
export const CALLOUT = {
  note: { word: 'Note', Icon: Info, cls: 'border-l-info', icon: 'text-info' },
  ok: { word: 'OK', Icon: Check, cls: 'border-l-success', icon: 'text-success' },
  warn: { word: 'Warning', Icon: TriangleAlert, cls: 'border-l-warning', icon: 'text-warning' },
  err: { word: 'Problem', Icon: X, cls: 'border-l-danger', icon: 'text-danger' },
  decision: { word: 'Decision', Icon: Gavel, cls: 'border-l-brand', icon: 'text-brand' },
} as const

/** Text alternatives of the newer types (CoreWidget.widgetText covers the first four). */
export function moreText(spec: WidgetSpec): string | undefined {
  const f = spec.fields
  switch (spec.type) {
    case 'line': {
      const x = f.x as (string | number)[]
      const u = unitOf(f.unit)
      return (f.series as Obj[]).map((s) => `${String(s.name)}: ${(s.values as (number | null)[]).map((y, i) => `${x[i]} ${y === null ? 'no value' : `${n(y)}${u}`}`).join(', ')}`).concat((f.markers as Obj[] | undefined)?.map((m) => `Marker at ${String(m.at)}: ${String(m.label)}`) ?? []).join('\n')
    }
    case 'sparkline': {
      const v = f.values as number[]
      return `${f.label ? `${String(f.label)}: ` : ''}${v.map(n).join(', ')}${unitOf(f.unit)} (now ${n(v[v.length - 1])}, min ${n(Math.min(...v))}, max ${n(Math.max(...v))})`
    }
    case 'metric':
      return (f.items as Obj[]).map((m) => `${String(m.label)}: ${String(m.value)}${unitOf(m.unit)}${typeof m.delta === 'number' ? `, ${deltaText(m)}` : ''}${m.hint ? ` (${String(m.hint)})` : ''}`).join('\n')
    case 'progress':
      return progressText(f)
    case 'timeline':
      return (f.items as Obj[]).map((s) => `${String(s.at)} ${String(s.label)}${s.status ? ` (${STEP_STATE[s.status as keyof typeof STEP_STATE].word})` : ''}${s.note ? `: ${String(s.note)}` : ''}`).join('\n')
    case 'diff':
      return `${f.file ? `${String(f.file)}\n` : ''}${String(f.lines)}`
    case 'status':
      return (f.items as Obj[]).map((s) => `${String(s.name)}: ${LINE_STATE[s.status as keyof typeof LINE_STATE].word}${s.detail ? `. ${String(s.detail)}` : ''}`).join('\n')
    case 'callout':
      return `${CALLOUT[f.role as keyof typeof CALLOUT].word}: ${String(f.text)}`
  }
}

function deltaText(m: Obj): string {
  const d = m.delta as number
  const sign = d > 0 ? '+' : d < 0 ? '−' : '±'
  const verdict = m.better && d !== 0 ? ((d > 0) === (m.better === 'up') ? ' (better)' : ' (worse)') : ''
  return `${sign}${n(Math.abs(d))}${unitOf(m.delta_unit ?? m.unit)}${verdict}`
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
    case 'line':
      return <LineChart spec={spec} />
    case 'sparkline':
      return <Sparkline spec={spec} />
    case 'metric':
      return (
        <div className="grid grid-cols-[repeat(auto-fit,minmax(150px,1fr))] gap-2">
          {(f.items as Obj[]).map((m, i) => (
            <Metric key={i} m={m} />
          ))}
        </div>
      )
    case 'progress':
      return <Progress f={f} title={spec.title} />
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
    case 'diff':
      return <Diff f={f} />
    case 'status':
      return (
        <ul className="divide-y divide-border rounded-md border border-border">
          {(f.items as Obj[]).map((s, i) => {
            const st = LINE_STATE[s.status as keyof typeof LINE_STATE]
            return (
              <li key={i} className="grid grid-cols-[5.5rem_minmax(0,12rem)_1fr] items-baseline gap-2 px-2 py-1 text-[12px]">
                <span className={cn('inline-flex items-center gap-1 font-medium', st.cls)}>
                  <st.Icon className="size-3" aria-hidden />
                  {st.word}
                </span>
                <span className="truncate text-text">{String(s.name)}</span>
                <span className="text-text-muted">{typeof s.detail === 'string' ? s.detail : ''}</span>
              </li>
            )
          })}
        </ul>
      )
    case 'callout': {
      const c = CALLOUT[f.role as keyof typeof CALLOUT]
      return (
        <div data-callout={String(f.role)} className={cn('flex gap-2 rounded-md border border-l-4 border-border bg-bg px-3 py-2 text-[13px]', c.cls)}>
          <c.Icon className={cn('mt-0.5 size-4 shrink-0', c.icon)} aria-hidden />
          <p className="whitespace-pre-wrap text-text">
            <span className="font-semibold">{c.word}: </span>
            {String(f.text)}
          </p>
        </div>
      )
    }
  }
  return null
}

function Metric({ m }: { m: Obj }) {
  const d = typeof m.delta === 'number' ? m.delta : undefined
  const good = d !== undefined && m.better && d !== 0 ? (d > 0) === (m.better === 'up') : undefined
  const Arrow = d === undefined || d === 0 ? Minus : d > 0 ? ArrowUp : ArrowDown
  return (
    <div className="rounded-md border border-border bg-bg px-3 py-2">
      <div className="truncate text-[11px] text-text-muted">{String(m.label)}</div>
      <div className="mt-0.5 flex items-baseline gap-1">
        <span className="text-xl font-semibold tabular-nums tracking-tight text-text">{typeof m.value === 'number' ? n(m.value) : String(m.value)}</span>
        {typeof m.unit === 'string' && <span className="text-[12px] text-text-muted">{m.unit}</span>}
      </div>
      {d !== undefined && (
        <div className={cn('mt-0.5 inline-flex items-center gap-1 text-[11px] tabular-nums', good === true ? 'text-success' : good === false ? 'text-danger' : 'text-text-muted')}>
          <Arrow className="size-3" aria-hidden />
          {deltaText(m)}
        </div>
      )}
      {typeof m.hint === 'string' && <div className="text-[11px] text-text-faint">{m.hint}</div>}
    </div>
  )
}

function Progress({ f, title }: { f: Obj; title?: string }) {
  const max = f.max as number
  const segs: { label: string; value: number; status: keyof typeof SEGMENT }[] =
    typeof f.value === 'number' ? [{ label: 'Done', value: f.value, status: 'ok' }] : (f.segments as Obj[]).map((s) => ({ label: String(s.label), value: s.value as number, status: (s.status as keyof typeof SEGMENT) ?? 'neutral' }))
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
        {typeof f.value !== 'number' && <span>of {n(max)}{unitOf(f.unit)}</span>}
      </div>
    </div>
  )
}

function Diff({ f }: { f: Obj }) {
  const lines = String(f.lines).split('\n')
  return (
    <div className="overflow-hidden rounded-md border border-border">
      {typeof f.file === 'string' && <div className="border-b border-border bg-surface-2 px-2 py-1 font-mono text-[11px] text-text-muted">{f.file}</div>}
      <pre className="overflow-x-auto bg-bg py-1 font-mono text-[12px] leading-5">
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

function Sparkline({ spec }: { spec: WidgetSpec }) {
  const v = spec.fields.values as number[]
  const W = 120
  const H = 28
  const lo = Math.min(...v)
  const hi = Math.max(...v)
  const X = (i: number) => 2 + (i / (v.length - 1)) * (W - 4)
  const Y = (y: number) => (hi === lo ? H / 2 : H - 3 - ((y - lo) / (hi - lo)) * (H - 6))
  const last = v[v.length - 1]
  return (
    <p className="flex flex-wrap items-center gap-2 text-[13px] text-text">
      {typeof spec.fields.label === 'string' && <span className="text-text-muted">{spec.fields.label}</span>}
      <svg role="img" viewBox={`0 0 ${W} ${H}`} width={W} height={H} className="inline-block">
        <title>{moreText(spec)}</title>
        <polyline points={v.map((y, i) => `${X(i)},${Y(y)}`).join(' ')} className="fill-none stroke-chart-1" strokeWidth={1.5} />
        <circle cx={X(v.length - 1)} cy={Y(last)} r={2.5} className="fill-chart-1" />
      </svg>
      <span className="font-semibold tabular-nums">
        {n(last)}
        {unitOf(spec.fields.unit)}
      </span>
      <span className="text-[11px] tabular-nums text-text-faint">
        min {n(lo)} · max {n(hi)}
      </span>
    </p>
  )
}

const LW = 480
const LH = 190
const PAD = { l: 40, r: 70, t: 10, b: 24 }

function niceTicks(lo: number, hi: number): number[] {
  if (hi === lo) return [lo]
  const raw = (hi - lo) / 3
  const mag = 10 ** Math.floor(Math.log10(raw))
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) ?? raw
  const out: number[] = []
  // From the step at or below lo to the step at or above hi, so the top line is labelled.
  for (let t = Math.floor(lo / step) * step; t < hi + step - step * 1e-9; t += step) out.push(Math.round(t * 1e6) / 1e6)
  return out
}

function LineChart({ spec }: { spec: WidgetSpec }) {
  const f = spec.fields
  const x = f.x as (string | number)[]
  const series = f.series as { name: string; values: (number | null)[] }[]
  const markers = (f.markers as { at: string | number; label: string }[] | undefined) ?? []
  const ys = series.flatMap((s) => s.values.filter((y): y is number => y !== null))
  const ticks = niceTicks(Math.min(0, ...ys), Math.max(...ys))
  const lo = Math.min(ticks[0], ...ys)
  const hi = Math.max(ticks[ticks.length - 1], ...ys)
  const numeric = typeof x[0] === 'number'
  const x0 = numeric ? (x[0] as number) : 0
  const x1 = numeric ? (x[x.length - 1] as number) : x.length - 1
  const X = (i: number) => PAD.l + ((numeric ? (x[i] as number) - x0 : i) / (x1 - x0 || 1)) * (LW - PAD.l - PAD.r)
  const Y = (y: number) => LH - PAD.b - ((y - lo) / (hi - lo || 1)) * (LH - PAD.t - PAD.b)
  const unit = unitOf(f.unit)
  const labelIdx = [...new Set([0, Math.floor((x.length - 1) / 2), x.length - 1])]
  return (
    <div className="space-y-1">
      <svg role="img" viewBox={`0 0 ${LW} ${LH}`} width="100%" style={{ maxWidth: LW * 1.5 }} className="block">
        <title>{`${spec.title ?? 'Line chart'}: ${moreText(spec)}`}</title>
        {ticks.map((t) => (
          <g key={t}>
            <line x1={PAD.l} x2={LW - PAD.r} y1={Y(t)} y2={Y(t)} className="stroke-border" strokeWidth={1} />
            <text x={PAD.l - 6} y={Y(t) + 3} textAnchor="end" className="fill-text-muted text-[10px]">
              {n(t)}
            </text>
          </g>
        ))}
        {labelIdx.map((i) => (
          <text key={i} x={X(i)} y={LH - 6} textAnchor={i === 0 ? 'start' : i === x.length - 1 ? 'end' : 'middle'} className="fill-text-muted text-[10px]">
            {String(x[i])}
          </text>
        ))}
        {markers.map((m, i) => {
          const at = X(x.findIndex((xv) => xv === m.at))
          return (
            <g key={i} data-marker={m.label}>
              <line x1={at} x2={at} y1={PAD.t} y2={LH - PAD.b} className="stroke-text-faint" strokeDasharray="3 3" strokeWidth={1} />
              <text x={at + 3} y={PAD.t + 9 + i * 11} className="fill-text-muted text-[10px]">
                {m.label}
              </text>
            </g>
          )
        })}
        {series.map((s, si) => {
          // A null is a gap: the line breaks there instead of drawing through it.
          const runs: string[][] = [[]]
          s.values.forEach((y, i) => (y === null ? runs.push([]) : runs[runs.length - 1].push(`${X(i)},${Y(y)}`)))
          const lastI = s.values.map((y, i) => (y === null ? -1 : i)).filter((i) => i >= 0).pop()!
          return (
            <g key={s.name} data-series={s.name}>
              {runs.filter((r) => r.length).map((r, ri) => (r.length === 1 ? <circle key={ri} cx={r[0].split(',')[0]} cy={r[0].split(',')[1]} r={2} className={FILL[si]} /> : <polyline key={ri} points={r.join(' ')} className={cn('fill-none', STROKE[si])} strokeWidth={2} />))}
              <text x={X(lastI) + 5} y={Y(s.values[lastI]!) + 3} className="fill-text text-[10px]">
                {`${n(s.values[lastI]!)}${unit}`}
              </text>
            </g>
          )
        })}
      </svg>
      {series.length > 1 && (
        <ul className="flex flex-wrap gap-x-3 text-[11px] text-text-muted">
          {series.map((s, si) => (
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
