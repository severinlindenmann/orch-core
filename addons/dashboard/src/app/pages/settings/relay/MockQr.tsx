/**
 * A mock QR code: the three finder squares and modules drawn from a seed. It encodes nothing and no camera reads it;
 * the real host draws the pairing offer (orch v2 §7). Inline SVG, theme tokens only.
 */
export function MockQr({ seed, size = 184 }: { seed: string; size?: number }) {
  const n = 25
  let h = 2166136261
  for (const c of seed) h = Math.imul(h ^ c.charCodeAt(0), 16777619) >>> 0
  const rnd = () => {
    h = Math.imul(h ^ (h >>> 15), 2246822507) >>> 0
    h = Math.imul(h ^ (h >>> 13), 3266489909) >>> 0
    return ((h ^= h >>> 16) >>> 0) / 4294967296
  }
  const finder = (x: number, y: number) => x < 8 && y < 8 ? [0, 0] : x >= n - 8 && y < 8 ? [n - 7, 0] : x < 8 && y >= n - 8 ? [0, n - 7] : null
  const cells: [number, number][] = []
  for (let y = 0; y < n; y++)
    for (let x = 0; x < n; x++) {
      if (finder(x, y)) continue
      if (rnd() < 0.48) cells.push([x, y])
    }
  const eye = (x: number, y: number) => (
    <g key={`${x}-${y}`}>
      <rect x={x} y={y} width={7} height={7} fill="var(--text)" />
      <rect x={x + 1} y={y + 1} width={5} height={5} fill="var(--surface)" />
      <rect x={x + 2} y={y + 2} width={3} height={3} fill="var(--text)" />
    </g>
  )
  return (
    <svg viewBox={`-2 -2 ${n + 4} ${n + 4}`} width={size} height={size} role="img" aria-label="Mock QR code (not scannable)" shapeRendering="crispEdges" className="shrink-0 rounded-md border border-border">
      <rect x={-2} y={-2} width={n + 4} height={n + 4} fill="var(--surface)" />
      {cells.map(([x, y]) => (
        <rect key={`${x}.${y}`} x={x} y={y} width={1} height={1} fill="var(--text)" />
      ))}
      {eye(0, 0)}
      {eye(n - 7, 0)}
      {eye(0, n - 7)}
    </svg>
  )
}
