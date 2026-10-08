import { Check, CircleHelp, X } from 'lucide-react'
import { cn } from '@/lib/utils'
import { barPairs, type WidgetSpec } from './parse'

// The four core types, drawn by core: server-style HTML and inline SVG, no script, tokens only. Meaning is never by
// colour alone (a word or glyph as well). Data was validated by parse.ts before it gets here.

type Row = Record<string, unknown>
const fmt = (v: unknown) => (v === null || v === undefined ? '' : typeof v === 'boolean' ? (v ? 'Yes' : 'No') : String(v))

const VERDICT = {
  met: { word: 'Met', Icon: Check, cls: 'text-success' },
  not_met: { word: 'Not met', Icon: X, cls: 'text-danger' },
  unproven: { word: 'Unproven', Icon: CircleHelp, cls: 'text-text-muted' },
} as const

/** The text alternative: what a screen reader, `Show text` and a text-only reader get instead of the picture. */
export function widgetText(spec: WidgetSpec): string {
  const f = spec.fields
  const head = spec.title ? `${spec.title}\n` : ''
  switch (spec.type) {
    case 'bars': {
      const unit = f.unit ? ` ${f.unit}` : ''
      return head + barPairs(f.data).map(([k, v]) => `${k}: ${v}${unit}`).join('\n')
    }
    case 'table':
      return head + [(f.columns as string[]).join(' | '), ...(f.rows as unknown[][]).map((r) => r.map(fmt).join(' | '))].join('\n')
    case 'checks':
      return head + (f.rows as Row[]).map((r) => `${r.ac}: ${VERDICT[r.verdict as keyof typeof VERDICT].word}. ${r.evidence}`).join('\n')
    case 'kv':
      return head + Object.entries(f.items as Record<string, unknown>).map(([k, v]) => `${k}: ${fmt(v)}`).join('\n')
  }
  return head
}

export function CoreWidget({ spec }: { spec: WidgetSpec }) {
  const f = spec.fields
  switch (spec.type) {
    case 'bars':
      return <Bars spec={spec} />
    case 'table':
      return <DataTable spec={spec} />
    case 'checks':
      return (
        <div>
          <p className="mb-1 text-[11px] text-text-muted">The agent&apos;s check: a claim, not a verdict.</p>
          <ul className="divide-y divide-border rounded-md border border-border">
            {(f.rows as Row[]).map((r, i) => {
              const v = VERDICT[r.verdict as keyof typeof VERDICT]
              return (
                <li key={i} className="grid grid-cols-[3.5rem_6.5rem_1fr] items-baseline gap-2 px-2 py-1 text-[12px]">
                  <span className="font-mono text-text-muted">{String(r.ac)}</span>
                  <span className={cn('inline-flex items-center gap-1 font-medium', v.cls)}>
                    <v.Icon className="size-3" aria-hidden />
                    {v.word}
                  </span>
                  <span className="text-text">{String(r.evidence)}</span>
                </li>
              )
            })}
          </ul>
        </div>
      )
    case 'kv':
      return (
        <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-0.5 text-[12px]">
          {Object.entries(f.items as Record<string, unknown>).map(([k, v]) => (
            <div key={k} className="contents">
              <dt className="text-text-muted">{k}</dt>
              <dd className="text-text">{fmt(v)}</dd>
            </div>
          ))}
        </dl>
      )
  }
  return null
}

const LABEL_W = 132
const VALUE_W = 56
const BAR_W = 260
const ROW = 20

function Bars({ spec }: { spec: WidgetSpec }) {
  const pairs = barPairs(spec.fields.data)
  const unit = spec.fields.unit ? ` ${spec.fields.unit}` : ''
  const highlight = spec.fields.highlight as string | undefined
  const max = Math.max(...pairs.map(([, v]) => Math.abs(v)), 1)
  const w = LABEL_W + BAR_W + VALUE_W
  const h = pairs.length * ROW + 4
  return (
    <div className={cn('overflow-y-auto', pairs.length > 10 && 'max-h-[240px]')} tabIndex={pairs.length > 10 ? 0 : undefined} role={pairs.length > 10 ? 'region' : undefined} aria-label={pairs.length > 10 ? `Scrollable chart: ${spec.title ?? 'bars'}` : undefined}>
      <svg role="img" viewBox={`0 0 ${w} ${h}`} width="100%" style={{ maxWidth: w * 1.6 }} className="block">
        <title>{`${spec.title ?? 'Bars'}: ${pairs.map(([k, v]) => `${k} ${v}${unit}`).join(', ')}`}</title>
        {pairs.map(([k, v], i) => {
          const y = i * ROW + 2
          const hi = k === highlight
          return (
            <g key={`${k}-${i}`}>
              <text x={LABEL_W - 8} y={y + 13} textAnchor="end" className={cn('fill-text-muted text-[11px]', hi && 'fill-text font-semibold')}>
                {k.length > 20 ? `${k.slice(0, 19)}…` : k}
              </text>
              <rect data-bar={k} x={LABEL_W} y={y + 2} width={Math.max(1, (Math.abs(v) / max) * BAR_W)} height={ROW - 6} rx={2} className={hi ? 'fill-brand' : 'fill-info'} />
              <text x={LABEL_W + Math.max(1, (Math.abs(v) / max) * BAR_W) + 6} y={y + 13} className="fill-text text-[11px]">
                {v}
                {unit}
                {hi ? ' (highlighted)' : ''}
              </text>
            </g>
          )
        })}
      </svg>
    </div>
  )
}

function DataTable({ spec }: { spec: WidgetSpec }) {
  const columns = spec.fields.columns as string[]
  const rows = spec.fields.rows as unknown[][]
  return (
    <div role="region" aria-label={`Scrollable table: ${spec.title ?? 'table'}`} tabIndex={0} className="max-h-60 overflow-auto rounded-md border border-border">
      <table className="w-full border-collapse text-[12px]">
        <thead className="sticky top-0 bg-surface-2">
          <tr>
            {columns.map((c, i) => (
              <th key={i} scope="col" className="border-b border-border px-2 py-1 text-left font-medium text-text-muted">
                {c}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i} className="border-b border-border last:border-b-0">
              {r.map((c, j) => (
                <td key={j} className="px-2 py-1 align-top text-text">
                  {fmt(c)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
