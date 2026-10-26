import type { Terminal } from '@xterm/xterm'
import type { Run } from '@/api/runs'
import { runName } from './runName'

export function transcript(term: Pick<Terminal, 'buffer'>): string {
  const buffer = term.buffer.active
  // Soft-wrapped rows are one logical line: join them without a newline (the wrapped row keeps its full width).
  const lines: string[] = []
  for (let i = 0; i < buffer.length; i++) {
    const row = buffer.getLine(i)
    const wrapped = !!row?.isWrapped && lines.length > 0
    const text = row?.translateToString(!buffer.getLine(i + 1)?.isWrapped) ?? ''
    if (wrapped) lines[lines.length - 1] += text
    else lines.push(text)
  }
  while (lines.at(-1) === '') lines.pop()
  return lines.join('\n')
}

export function transcriptName(run: Run, now: Date): string {
  const slug = runName(run).toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 40).replace(/-$/, '') || 'terminal'
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${slug}-${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}-${pad(now.getHours())}${pad(now.getMinutes())}.txt`
}
