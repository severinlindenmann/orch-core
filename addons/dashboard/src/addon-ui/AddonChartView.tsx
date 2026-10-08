import { Bar, BarChart, CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import type { NodeOf } from './nodes'

const COLORS = ['var(--chart-1)', 'var(--chart-2)', 'var(--chart-3)', 'var(--chart-4)', 'var(--chart-5)']
const axis = { stroke: 'var(--text-faint)', fontSize: 11, tickLine: false, axisLine: false } as const

export function AddonChartView({ node }: { node: NodeOf<'chart'> }) {
  const Chart = node.kind === 'bar' ? BarChart : LineChart
  return (
    <div className="h-[200px] w-full" role="img" aria-label={node.series.map((s) => s.label).join(', ')}>
      <ResponsiveContainer width="100%" height="100%" minWidth={0}>
        <Chart data={node.points} margin={{ top: 8, right: 8, bottom: 0, left: -12 }}>
          <CartesianGrid vertical={false} stroke="var(--border)" />
          <XAxis dataKey={node.xKey} {...axis} />
          <YAxis {...axis} width={44} />
          <Tooltip
            cursor={{ fill: 'var(--surface-2)' }}
            contentStyle={{ background: 'var(--popover)', border: '1px solid var(--border)', borderRadius: 8, fontSize: 12, color: 'var(--text)' }}
            labelStyle={{ color: 'var(--text-muted)' }}
          />
          {node.series.map((s, i) =>
            node.kind === 'bar' ? (
              <Bar key={s.key} dataKey={s.key} name={s.label} fill={COLORS[i % COLORS.length]} radius={[3, 3, 0, 0]} isAnimationActive={false} />
            ) : (
              <Line key={s.key} dataKey={s.key} name={s.label} stroke={COLORS[i % COLORS.length]} strokeWidth={2} dot={false} isAnimationActive={false} />
            ),
          )}
        </Chart>
      </ResponsiveContainer>
    </div>
  )
}
