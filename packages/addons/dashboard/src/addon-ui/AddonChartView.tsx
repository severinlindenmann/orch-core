import { Bar, BarChart, CartesianGrid, LabelList, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import type { NodeOf } from './nodes'

const COLORS = ['var(--chart-1)', 'var(--chart-2)', 'var(--chart-3)', 'var(--chart-4)', 'var(--chart-5)']
const axis = { stroke: 'var(--text-faint)', fontSize: 11, tickLine: false, axisLine: false } as const
const tooltip = {
  cursor: { fill: 'var(--surface-2)' },
  contentStyle: { background: 'var(--popover)', border: '1px solid var(--border)', borderRadius: 8, fontSize: 12, color: 'var(--text)' },
  labelStyle: { color: 'var(--text-muted)' },
}

export function AddonChartView({ node }: { node: NodeOf<'chart'> }) {
  const horizontal = node.kind === 'bar' && node.layout === 'horizontal'
  const money = (v: unknown) => (node.unit ? `${node.unit} ${typeof v === 'number' ? v.toFixed(2) : String(v)}` : String(v))
  const label = node.title ?? node.series.map((s) => s.label).join(', ')
  // One row per bar needs room for its label: 34 px a row, at least the usual 200.
  const height = horizontal ? Math.max(200, node.points.length * 34 + 24) : 200
  return (
    <figure className="m-0">
      {node.title && <figcaption className="mb-1 text-[12px] font-medium text-text-muted">{node.title}</figcaption>}
      <div className="w-full" style={{ height }} role="img" aria-label={label}>
        <ResponsiveContainer width="100%" height="100%" minWidth={0}>
          {node.kind === 'bar' ? (
            <BarChart data={node.points} layout={horizontal ? 'vertical' : 'horizontal'} margin={horizontal ? { top: 4, right: 72, bottom: 0, left: 0 } : { top: 8, right: 8, bottom: 0, left: -12 }}>
              {/* Value labels carry the numbers: no gridlines then, so a label never sits on a line (B m4). */}
              {!node.valueLabels && <CartesianGrid vertical={horizontal} horizontal={!horizontal} stroke="var(--border)" />}
              {horizontal ? (
                <>
                  <XAxis type="number" hide />
                  <YAxis type="category" dataKey={node.xKey} {...axis} width={132} />
                </>
              ) : (
                <>
                  <XAxis dataKey={node.xKey} {...axis} />
                  <YAxis {...axis} width={44} />
                </>
              )}
              <Tooltip {...tooltip} formatter={(v) => money(v)} />
              {node.series.map((s, i) => (
                <Bar key={s.key} dataKey={s.key} name={s.label} fill={COLORS[i % COLORS.length]} radius={horizontal ? [0, 3, 3, 0] : [3, 3, 0, 0]} isAnimationActive={false}>
                  {node.valueLabels && <LabelList dataKey={s.key} position={horizontal ? 'right' : 'top'} formatter={(v: unknown) => money(v)} fill="var(--text-muted)" fontSize={11} />}
                </Bar>
              ))}
            </BarChart>
          ) : (
            <LineChart data={node.points} margin={{ top: 8, right: 8, bottom: 0, left: -12 }}>
              <CartesianGrid vertical={false} stroke="var(--border)" />
              <XAxis dataKey={node.xKey} {...axis} />
              <YAxis {...axis} width={44} />
              <Tooltip {...tooltip} formatter={(v) => money(v)} />
              {node.series.map((s, i) => (
                <Line key={s.key} dataKey={s.key} name={s.label} stroke={COLORS[i % COLORS.length]} strokeWidth={2} dot={false} isAnimationActive={false} />
              ))}
            </LineChart>
          )}
        </ResponsiveContainer>
      </div>
    </figure>
  )
}
