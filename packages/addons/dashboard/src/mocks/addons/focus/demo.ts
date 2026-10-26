// Demo data of focus mode for the mock host (no PTY, no runner): one RUNNING agent run on DEMO-0043 with a transcript,
// delegations (one the agent spawned, one the person asked for that needs them), a live worktree diff where two authors
// touched one file, and research sources. Only `!isRealHost()` reads this; a real host answers from the real routes.
import type { Delegations, Transcript, WorktreeDiff } from '@/api/focus'
import type { Run, RunList } from '@/api/runs'
import sources from './sources.json'

export const DEMO_FOCUS_TICKET = 'DEMO-0043'
export const DEMO_MAIN_RUN = 'r_01J0000000000000000000043A'
export const DEMO_CHILD_RUN = 'r_01J0000000000000000000043B'

const base = {
  rev: 1, kind: 'agent' as const, ticket: DEMO_FOCUS_TICKET, harness: 'claude-code', mode: 'work', as_person: false, state: 'running' as const,
  viewers: 1, writer: false, cols: 120, rows: 32, replay_until: null, label_hidden: false, cwd: { kind: 'ticket', ticket: DEMO_FOCUS_TICKET }, exit: null,
}

export function demoRunList(): RunList {
  const runs: Run[] = [
    { ...base, run: DEMO_MAIN_RUN, model: 'sonnet', started_at: '2026-10-26T10:40:00Z', label: 'Load tariff tables as dbt seeds' },
    { ...base, run: DEMO_CHILD_RUN, model: 'sonnet', started_at: '2026-10-26T10:38:00Z', label: 'Check tenant isolation', grant_state: 'ended' },
  ]
  return { runs, runner: null, truncated: false }
}

export function demoDelegations(): Delegations {
  return {
    ticket: DEMO_FOCUS_TICKET, supported: true,
    sessions: [{
      run: DEMO_MAIN_RUN, session: 's_main', harness: 'claude-code', model: 'sonnet', state: 'running',
      policy: { max_children: 4, max_depth: 1 }, files: ['seeds/t1_load.csv'], visibility: { available: true, reason: null },
      children: [
        { id: 'cc:agent-a04b67fe', parent_run: DEMO_MAIN_RUN, title: 'Write boundary tests', spawned_by: 'agent', harness: 'claude-code', model: 'sonnet', depth: 1, state: 'done',
          started_at: '2026-10-26T10:01:00Z', ended_at: '2026-10-26T10:04:00Z', run: null, transcript: { run: DEMO_MAIN_RUN, child: 'agent-a04b67fe' }, files: ['seeds/t1_load.csv', 'tests/test_seeds.py'] },
        { id: 'cc:agent-b19c33d0', parent_run: DEMO_MAIN_RUN, title: 'Check tenant isolation', spawned_by: 'human', harness: 'claude-code', model: 'sonnet', depth: 1, state: 'running',
          started_at: '2026-10-26T10:38:00Z', ended_at: null, run: DEMO_CHILD_RUN, transcript: { run: DEMO_CHILD_RUN, child: null }, files: [] },
      ],
    }],
  } as unknown as Delegations
}

export function demoTranscript(): Transcript {
  return {
    run: DEMO_MAIN_RUN, harness: 'claude-code', available: true, reason: null, next: 'o:20411', eof: true,
    items: [
      { id: 'u1', role: 'user', kind: 'text', text: 'Load the tariff tables as dbt seeds. Keep the meter join unchanged.', ts: '2026-10-26T10:40:02Z' },
      { id: 'a1', role: 'assistant', kind: 'text', text: 'I read the billing models and the two tariff CSVs. I will load them as seeds and add a not-null test on valid_from.', ts: '2026-10-26T10:41:00Z' },
      { id: 'a2', role: 'assistant', kind: 'tool', text: 'Edit seeds/t1_load.csv', ts: '2026-10-26T10:41:20Z', tool: { name: 'Edit', call: 'toolu_01' }, files: ['seeds/t1_load.csv'] },
      { id: 'toolu_01:r', role: 'tool', kind: 'result', text: 'ok', ts: '2026-10-26T10:41:21Z', error: false },
      { id: 'a3', role: 'assistant', kind: 'question', text: 'Should valid_from in the seeds be a DATE or a TIMESTAMP?', ts: '2026-10-26T10:42:00Z', options: ['DATE (local midnight)', 'TIMESTAMP (UTC)'] },
      { id: 's1', role: 'system', kind: 'status', text: 'waiting for you', ts: '2026-10-26T10:42:05Z' },
    ],
  } as Transcript
}

export function demoDiff(): WorktreeDiff {
  return {
    ticket: DEMO_FOCUS_TICKET, repo: 'billing', available: true, reason: null, path_label: 'billing/ab12cd34-DEMO-0043', branch: 'focus/demo-0043',
    head: '9c1a72b0000000000000000000000000000000aa', checked_at: '2026-10-26T10:50:00Z', truncated: false,
    files: [
      { path: 'seeds/t1_load.csv', status: 'M', added: 9, removed: 2, binary: false, too_large: false },
      { path: 'tests/test_seeds.py', status: '?', added: 2, removed: 0, binary: false, too_large: false },
    ],
    unified: 'diff --git a/seeds/t1_load.csv b/seeds/t1_load.csv\n--- a/seeds/t1_load.csv\n+++ b/seeds/t1_load.csv\n@@ -1,3 +1,4 @@\n tariff,valid_from,rate\n-T1,2025-01-01,0.21\n+T1,2025-01-01,0.22\n+T1,2026-01-01,0.24\n T2,2025-01-01,0.18\n' +
      'diff --git a/tests/test_seeds.py b/tests/test_seeds.py\nnew file mode 100644\n--- /dev/null\n+++ b/tests/test_seeds.py\n@@ -0,0 +1,2 @@\n+def test_valid_from_not_null():\n+    pass\n',
  }
}

export const demoSources = () => sources

/** The two runs above: the mock host draws a canned terminal and chat for them instead of attaching a PTY. */
export const isDemoRun = (run: string) => run === DEMO_MAIN_RUN || run === DEMO_CHILD_RUN
