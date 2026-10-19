// A run's name (light: the Terminals page uses it without loading xterm).
import type { Run } from '@/api/runs'

/** A run's name in the dock: its label (untrusted, shown through visible.tsx), else the harness and the ticket. */
export function runName(run: Run): string {
  if (run.label) return run.label
  const what = run.harness === 'shell' ? 'Shell' : run.harness === 'claude-code' ? 'Claude' : run.harness === 'codex' ? 'Codex' : 'Terminal'
  return run.ticket ? `${run.ticket} ${what}` : what
}
