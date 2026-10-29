// DEV ONLY (reached through the `__layout/$name` route, whose component loads only when `import.meta.env.DEV`; production redirects to /).
//
// The quick tasks page, the Settings "Records" tab and the "Dashboard execution" card are drawn only on a real host
// (features `quick`, `records`, `runner`), so the layout guard, which runs against the mock app, never sees them. This
// draws the REAL components with fixture answers: the mock `api` methods they call are replaced here, per scenario, by
// functions that return plain data. Nothing of this is in a production build and nothing in it reaches a host.
import { useEffect, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { api } from '@/api/client'
import type { QuickRow, RecordsStatus } from '@/api/quickRecords'
import { ApiError } from '@/api/types'
import { SettingsFrame } from '@/app/pages/settings'
import { HostGeneral } from '@/app/pages/settings/host/General'
import { RecordsBlock } from '@/app/pages/settings/host/Records'
import { HostGates } from '@/app/pages/settings/host/Gates'
import { HostGrants } from '@/app/pages/settings/host/Grants'
import { HostMembers } from '@/app/pages/settings/host/Members'
import { HostDevices } from '@/app/pages/settings/host/Devices'
import { DropError, dropError } from '@/api/drop'
import { DropErrorLine, DropView, type DropPreviewStart } from '@/app/pages/drop'
import { AGENT, PHONE_SHARE, createDropFixture, dropItem, dropVersion, installDropFixture, ME, PHONE, type DropFixture, type DropFixtureOptions } from './dropFixture'
import { HOST_TABS } from '@/app/pages/settings/tabs'
import { QuickPage } from '@/app/pages/quick'
import { AddonNode } from '@/addon-ui/AddonNode'
import { WIKI_PREVIEW_NODE, WIKI_PREVIEW_PAGES } from './wikiPreview'
import { useWorkspace } from '@/app/workspace'
import { runsApi, type Run, type RunList } from '@/api/runs'
import { TerminalsPage } from '@/app/pages/terminals'
import { RealDock } from '@/app/terminal/real/RealDock'
import { termTransport, type TermConnection, type TermHandlers } from '@/app/terminal/real/termProtocol'
import { agentsApi, type AgentPreview } from '@/api/agents'
import { routingApi, type RoutingChoices } from '@/api/modelRouting'
import { sharesApi } from '@/api/shares'
import { StartAgentDialog } from '@/app/terminal/real/StartAgentDialog'
import { AgentAccess } from '@/app/shell/AgentAccess'
import { ConfirmWords } from '@/setup/Setup'
import { nowMs } from '@/lib/time'
import { clickProblem, CLICK_RISK, ROTATE_LINE, type ClickCard, type ClickViewAnswer } from '@/api/click'
import { pastedIn } from '@/api/pasteImage'
import { Section } from '@/app/pages/new-ticket/SectionsEditor'
import { ClickSignPanel } from '@/components/sign/ClickSignPanel'
import { storyOf } from '@/components/sign/story'
import { signerProblem } from '@/api/refusals'
import type { HostGrant, HostPolicy, PolicyGate, RosterMember } from '@/api/settings'
import type { Workspace } from '@/api/types'
import { ClickProblemBox } from '@/components/sign/RefusalDevices'
import { ME as READER_ME, MARA, readersOf, readersRoster, refusalSamples } from './readersFixture'
import { setFeatures } from '@/api/features'
import { NewTicketPage } from '@/app/pages/new-ticket'
import { TicketPage } from '@/app/pages/ticket'

export const LAYOUT_SCENARIOS = [
  // R2-2: Settings > Add-ons with one of each state.
  'addons-mixed',
  'quick-normal',
  'quick-many',
  'quick-add',
  'quick-off',
  'wiki-empty',
  'wiki-created',
  'wiki-many',
  'records-ok',
  'records-hidden-url',
  'records-first',
  'records-push-failed',
  'records-push-mismatch',
  'runner-on',
  'runner-off',
  // M7a: the real-host terminal dock (RealDock with fixture runs and a fixture socket).
  'terminal-dock',
  'terminal-colours',
  'terminals-page',
  'terminals-page-empty',
  'terminal-preview',
  'terminal-readonly',
  'terminal-take',
  'terminal-exited',
  'terminal-stop',
  // M7b: core's Start agent dialog with fixture answers (preview, blocked reasons, the runner grant card, outcomes).
  'agent-dialog',
  'agent-dialog-acked',
  'agent-blocked',
  'agent-grant',
  // The Start agent dialog with a model chosen by an add-on.
  'agent-model',
  'addons-publish',
  'addons-publish-open',
  'addons-publish-unset',
  // B11-P1: the header pill (live, stopped) and the runner grant on a terminal-approval workspace.
  'agent-pill',
  'agent-pill-stopped',
  'agent-pill-off',
  'setup-words',
  // Core H12d-2: Settings → Devices with reader levels, and every click-to-sign refusal sentence.
  'readers-devices',
  'readers-solo',
  'convenience-refusals',
  // The Settings → General signing card ("Sign in the dashboard"): the five states of #578, with a long name, prefix and device.
  'signing-on',
  'signing-off',
  'signing-terminal-on',
  'signing-idle-off', // #1198 C17: One click switched itself off; a plain click turns it on again
  'signing-unsafe',
  'signing-unknown',
  // Core D1 (#582): the dev sandbox host. The DEV SANDBOX badge (normal and with a long workspace name), the alarms list,
  // every host.dev_refused reason, and the signing card on a dev host.
  'signing-dev-host',
  'dev-badge',
  'dev-badge-long',
  'dev-alarms',
  'dev-status-failed', // the host status read fails: a neutral "Host status unavailable" cue, never a normal-looking host
  'dev-refused',
  // Drop (proposed drop/1): Workspace → Drop with the fixture drop api.
  'drop-unavailable',
  'drop-list',
  'drop-long-names',
  'drop-upload-progress',
  'drop-errors',
  'drop-versions',
  'drop-phone',
  // Run 8 V1: the final Settings pages (General, People, Approvals, Agent access) with fixture answers.
  'settings-general',
  'settings-members',
  'settings-approvals',
  'settings-approvals-stuck',
  'settings-access',
  'settings-access-empty',
  // B11 (finding 9): the New ticket form with a signed hand-off; Create opens ONE review of the ticket and its text.
  'new-sequence',
  // U4: the same form on a host that can start agents: with a usable agent access, and without one.
  'new-sequence-start',
  'new-sequence-start-nogrant',
  // B12D (N3): the ticket page's edit of text, criteria and people, and its ONE review.
  'edit-ticket',
  // R3 part B: paste a screenshot into a section, and the sign dialog's picture check (match, and a swapped file).
  'paste-sign',
  // R3 variant B: the simpler sign dialog, one scenario per case.
  'sign-b-new', 'sign-b-new-access', 'sign-b-requirements', 'sign-b-plan', 'sign-b-extend',
] as const
export type LayoutScenario = (typeof LAYOUT_SCENARIOS)[number]

const LONG_URL = 'https://git.example.test/' + 'a-very-long-organisation-name/'.repeat(6) + 'records-of-the-workspace-with-an-unbreakable-name.git'
const LONG_WORD = 'U'.repeat(90)

// The signing card fixtures: a placeholder device id (d_ + 32 hex of an obvious pattern), never a real key, and no passphrase prefilled.
const PLACEHOLDER_DEVICE = 'd_' + '0123456789abcdef'.repeat(2)
const SIGNING_LONG_NAME = 'The quarterly orchestration workspace of the very long named organisation ' + LONG_WORD
const SIGNING_LONG_PREFIX = 'LONGPREFIX-' + 'WORKSPACE-'.repeat(5) + 'END' // 64 characters at most: a safe shell word
let idleResumed = false // the signing-idle-off fixture after its plain click
const signingCard = (scenario: LayoutScenario): ClickCard =>
  scenario === 'signing-idle-off'
    ? idleResumed
      ? { custody: 'convenience', enabled: true, available: true, switchable: true, device: PLACEHOLDER_DEVICE }
      : { custody: 'convenience', enabled: false, available: false, problem: 'idle', idle_hours: 12, switchable: true, device: PLACEHOLDER_DEVICE }
    : scenario === 'signing-dev-host'
    ? { custody: 'passphrase', enabled: false, available: false, problem: 'dev_host', switchable: true, device: PLACEHOLDER_DEVICE }
    : scenario === 'signing-on' || scenario === 'signing-unknown'
    ? { custody: 'passphrase', enabled: false, available: false, problem: 'not_convenience', switchable: true, device: PLACEHOLDER_DEVICE }
    : scenario === 'signing-off'
      ? { custody: 'convenience', enabled: true, available: true, switchable: true, device: PLACEHOLDER_DEVICE }
      : { custody: 'convenience', enabled: false, available: false, problem: 'disabled', switchable: true, device: PLACEHOLDER_DEVICE }
const signingWorkspace = (scenario: LayoutScenario, workspace: Workspace): Workspace => ({
  ...workspace,
  name: SIGNING_LONG_NAME,
  prefix: scenario === 'signing-unsafe' ? 'X;rm -rf ~' : SIGNING_LONG_PREFIX,
  click: signingCard(scenario),
})

// Core D1 fixtures: obvious placeholder ids, never real ones.
const DEV_WS = 'f'.repeat(32)
const hostInfoFor = (scenario: LayoutScenario): unknown =>
  scenario === 'dev-alarms'
    ? {
        dev: { sim_convenience: true, sandbox: '0123abcd' },
        alarms: [
          { code: 'sandbox_workspace_registered', workspace_id: DEV_WS },
          { code: 'sandbox_workspace_registered', workspace_id: 'not-a-workspace-id-' + LONG_WORD },
          { code: 'dev_convenience_refused', count: 3 },
          { code: 'some_future_alarm' },
          { code: 'Not a code <b>x</b>' },
        ],
      }
    : scenario === 'dev-badge' || scenario === 'dev-badge-long' || scenario === 'signing-dev-host'
      ? { dev: { sim_convenience: true, sandbox: '0123abcd' } }
      : {}
const DEV_REFUSED_REASONS = ['sandbox_workspace', 'not_sandbox', 'dev_origin', 'dev_host', 'not_foreground', 'service_managed', 'home_not_in_root']
const devRefusedSamples = () => [
  ...DEV_REFUSED_REASONS.map((r) => ({ title: `host.dev_refused · ${r}`, error: new ApiError(409, { code: 'host.dev_refused', message: `refused: as the host says it, with a long tail ${LONG_WORD}`, retryable: false, detail: { reason: r } }) })),
  { title: 'host.dev_refused · a reason this page does not know', error: new ApiError(409, { code: 'host.dev_refused', message: 'refused', retryable: false, detail: { reason: 'brand_new_reason' } }) },
  { title: 'host.dev_refused · no reason', error: new ApiError(409, { code: 'host.dev_refused', message: 'refused', retryable: false }) },
]

const quickRow = (i: number, over: Partial<QuickRow> = {}): QuickRow =>
  ({
    key: `DEMO-${String(100 + i).padStart(4, '0')}`,
    uid: `U${i}`,
    title: `Quick task number ${i}`,
    type: 'chore',
    priority: null,
    size: null,
    status: 'open',
    labels: ['quick'],
    parent: null,
    owner: null,
    assignees: [],
    claim: null,
    turn: { who: 'nobody', why: '' },
    progress: null,
    open_questions: 0,
    blocking_questions: null,
    restricted: false,
    awaiting_gate: null,
    addons: {},
    updated_at: '2026-10-12T09:00:00Z',
    quick: { commits: 1, files: 2, over: false },
    ...over,
  }) as QuickRow

const quickRows = (scenario: LayoutScenario): QuickRow[] =>
  scenario === 'quick-many'
    ? Array.from({ length: 60 }, (_, i) => quickRow(i, { title: i % 7 === 0 ? `Fix ${LONG_URL}` : `Quick task number ${i} with a title that goes on for a while to see how a row wraps`, labels: i % 3 === 0 ? ['quick', 'docs'] : ['quick'], quick: { commits: i % 5 === 0 ? null : i, files: i % 4 === 0 ? null : i * 2, over: i % 6 === 0 ? true : i % 5 === 0 ? null : false } }))
    : [
        quickRow(1, { title: `Read ${LONG_URL} and fix the link`, labels: ['quick', 'docs', 'a-rather-long-label-name', 'web'], quick: { commits: 4, files: 7, over: true } }),
        quickRow(2, { title: LONG_WORD, status: 'in-progress', claim: { agent: 'claude-code', for: 'p_sev', session: 's1', live: true } }),
        quickRow(3, { title: 'No repository is linked yet', status: 'backlog', quick: { commits: null, files: null, over: null }, awaiting_gate: 'verify' }),
        quickRow(4, { title: 'Fix the typo in CONTRIBUTING' }),
        quickRow(5, { title: 'Choose a photo format', open_questions: 1, claim: { agent: 'codex', for: 'p_sev', session: 's2', live: true } }),
      ]

const gatePolicy = (over: Partial<HostPolicy> = {}): HostPolicy => ({ approvers: ['owner'], count: 1, not: [], applies: 'all', independent: false, ...over })
/** The three questions, one of each kind: a preset, another preset, a custom rule (and, when stuck, a count nobody can meet). */
const settingsGates = (scenario: LayoutScenario): Record<PolicyGate, HostPolicy> => ({
  requirements: gatePolicy(),
  plan: scenario === 'settings-approvals-stuck' ? gatePolicy({ approvers: ['owner', 'maintainer'], count: 9 }) : gatePolicy({ approvers: ['maintainer', 'owner'] }),
  verify: gatePolicy({ approvers: ['reviewers'], not: ['assignees'] }),
  code: gatePolicy({ approvers: ['maintainer', 'owner'], not: ['assignees'], applies: 'off', independent: true }),
})
const accessGrants = (scenario: LayoutScenario): HostGrant[] =>
  scenario === 'settings-access-empty'
    ? []
    : [
        { id: 'gr_' + '0'.repeat(25) + 'A', person: READER_ME, scope: 'workable', verbs: 'agent', issued_at: '2026-10-23T08:00:00Z', expires_at: new Date(Date.now() + 3 * 86_400_000).toISOString(), revoked: null, sessions: 2, session_ids: [], sessions_more: null, label: 'runner' },
        { id: 'gr_' + '0'.repeat(25) + 'B', person: MARA, scope: 'workable', verbs: 'agent', issued_at: '2026-10-20T08:00:00Z', expires_at: new Date(Date.now() - 86_400_000).toISOString(), revoked: { at: '2026-10-21T09:00:00Z', by: READER_ME, seq: 4 }, sessions: 0, session_ids: [], sessions_more: null, label: null },
      ]
const records = (scenario: LayoutScenario): RecordsStatus => {
  const base: RecordsStatus = {
    available: true,
    configured: true,
    repo: { state: 'ok', problem: null, problem_hidden: false },
    pending: { events: 120, tickets: Array.from({ length: 12 }, (_, i) => ({ key: `DEMO-${String(i + 1).padStart(4, '0')}`, events: i + 3 })), more: true, workspace_events: 5 },
    last: { commit: 'c'.repeat(40), at: '2026-10-12T08:00:00Z', by: 'p_' + 'a'.repeat(32), ws_seq: 40, pushed_at: '2026-10-12T08:01:00Z' },
    last_noted: { by: 'p_' + 'a'.repeat(32), at: '2026-10-12T08:00:00Z' },
    auto: { on: true, every_min: 15, push: true },
    remote: { url: LONG_URL, url_hidden: false, private: true, export: 'ok' },
  }
  if (scenario === 'records-hidden-url')
    return { ...base, repo: { state: 'broken', problem: null, problem_hidden: true }, remote: { url: null, url_hidden: true, private: null, export: 'blocked_convenience' } }
  if (scenario === 'records-first') return { ...base, configured: false, repo: null, last: null, last_noted: null, auto: null, remote: null }
  return base
}

const RUN_BASE = 'r_01M4Y46B485AXD4CBEJZ15Z1'
const fixtureRun = (id: string, over: Partial<Run> = {}): Run => ({
  run: RUN_BASE + id, rev: 1, kind: 'shell', ticket: null, harness: 'shell', mode: null, model: null, as_person: true, state: 'running',
  started_at: '2026-10-12T09:00:00Z', viewers: 1, writer: false, cols: 100, rows: 30, replay_until: null, label: null, label_hidden: false,
  cwd: { kind: 'workspace' }, exit: null, ...over,
})
const ENDED = { code: 1, signal: null, at: '2026-10-12T09:30:00Z', reason: 'stopped' as const, cleanup: 'complete' as const }
const runList = (scenario: LayoutScenario): RunList => {
  const runner = { connected: true, enabled: true, terminals: true }
  if (scenario === 'terminal-exited')
    return { runner, truncated: false, runs: [fixtureRun('EX', { state: 'exited', replay_until: '2026-01-01T00:00:00Z', exit: ENDED, label: `Build ${LONG_WORD}` })] }
  if (scenario === 'terminals-page-empty') return { runner, truncated: false, runs: [] }
  if (scenario === 'terminal-stop')
    return { runner, truncated: false, runs: [fixtureRun('ST', { state: 'exited', replay_until: '2099-01-01T00:00:00Z', exit: { ...ENDED, cleanup: 'incomplete' } })] }
  return {
    runner,
    truncated: false,
    runs: [
      fixtureRun('AA', { writer: scenario === 'terminal-take', label: `Release notes for ${LONG_URL}` }),
      fixtureRun('BB', { ticket: 'DEMO-0043', label: null }),
      ...Array.from({ length: 6 }, (_, i) => fixtureRun(`C${i}`, { label: `Shell ${i + 3}` })),
      fixtureRun('DD', { state: 'exited', replay_until: '2099-01-01T00:00:00Z', exit: ENDED }),
    ],
  }
}
const AGENT_GRANT = 'gr_01M4Y46B485AXD4CBEJZ15Z1TV'
const ROUTING_CHOICES: RoutingChoices = {
  harnesses: [
    { id: 'claude-code', label: 'Claude', models: [{ id: 'fable', label: 'Fable' }, { id: 'opus', label: 'Opus' }, { id: 'sonnet', label: 'Sonnet' }, { id: 'haiku', label: 'Haiku' }] },
    { id: 'codex', label: 'Codex', models: [{ id: 'gpt-5.4', label: 'GPT-5.4' }, { id: 'gpt-5-codex', label: 'GPT-5 Codex' }, { id: 'gpt-5', label: 'GPT-5' }, { id: 'o3', label: 'o3' }] },
  ],
}
const agentPreview = (scenario: LayoutScenario): AgentPreview => {
  const base: AgentPreview = {
    spec: 'f'.repeat(64), ticket: 'DEMO-0043', display: `claude --model opus 'Run orch status, then work on ticket DEMO-0043. Follow AGENTS.orch.md.' ${LONG_WORD}`, display_hidden: false,
    cwd: { kind: 'worktree', repo: 'billing', ticket: 'DEMO-0043' }, cwd_path: `/Users/demo/${'deeply-nested-folder/'.repeat(6)}billing.worktrees/0a1b2c3d-DEMO-0043`, cwd_path_hidden: false,
    will_create_worktree: true, login_shell: true, env: ['TERM', 'COLORTERM', 'ORCH_GRANT', 'ORCH_SESSION', 'ORCH_TICKET', 'ORCH_HARNESS', 'CLAUDE_CODE_SUBAGENT_MODEL'],
    grant: { id: AGENT_GRANT, until: new Date(nowMs() + 3 * 86_400_000).toISOString(), scope: 'workable' }, blocked: null,
    warnings: ['the program is found and started through your login shell; your shell profile runs first and can change what runs', `ORCH_WORKTREES leaves out ${LONG_URL}: its path contains ':'`],
  }
  if (scenario === 'agent-model') return { ...base, display: `claude --model sonnet 'Run orch status, then work on ticket DEMO-0043.'`, model: 'sonnet', model_source: 'addon', model_by: 'model-routing' }
  if (scenario === 'agent-blocked') return { ...base, grant: null, blocked: { code: 'runner.disabled', message: `dashboard execution is off for this workspace on this machine ${LONG_WORD}`, detail: { missing: 'machine' } } }
  if (scenario === 'agent-grant') return { ...base, grant: null, blocked: { code: 'launch.no_grant', message: 'no usable runner grant', detail: { reason: 'expiring' } } }
  return base
}
/** What a shell with colours prints: ls, git status and diff, a failing test, all 16 ANSI colours (layout preview only). */
const ESC = '\x1b['
const COLOUR_SAMPLE = [
  `${ESC}32msev${ESC}0m@${ESC}36mdemo${ESC}0m ${ESC}1;34m~/code/billing${ESC}0m ${ESC}35m(feat/DEMO-0043)${ESC}0m $ ls --color`,
  `${ESC}1;34msrc${ESC}0m  ${ESC}1;34mtests${ESC}0m  README.md  ${ESC}1;32mrun.sh${ESC}0m  ${ESC}1;36mlatest -> v2${ESC}0m  ${ESC}31mbroken.tar.gz${ESC}0m`,
  `$ git status -sb`,
  `## feat/DEMO-0043...origin/feat/DEMO-0043 [ahead 2]`,
  ` ${ESC}31mM${ESC}0m src/tariffs.py`,
  `${ESC}32mA${ESC}0m  tests/test_tariffs.py`,
  `${ESC}31m??${ESC}0m notes.txt`,
  `$ git diff src/tariffs.py`,
  `${ESC}1mdiff --git a/src/tariffs.py b/src/tariffs.py${ESC}0m`,
  `${ESC}36m@@ -12,3 +12,4 @@ def load(path):${ESC}0m`,
  `${ESC}31m-    rows = read(path)${ESC}0m`,
  `${ESC}32m+    rows = read(path, strict=True)${ESC}0m`,
  `$ pytest -q`,
  `${ESC}32m..........${ESC}0m${ESC}31mF${ESC}0m${ESC}32m..${ESC}0m`,
  `${ESC}1;31mFAILED${ESC}0m tests/test_tariffs.py::test_strict - ${ESC}33mAssertionError${ESC}0m: expected 3, got 4`,
  `${ESC}2m(dim: 1 failed, 12 passed)${ESC}0m  ${ESC}4munderlined${ESC}0m  ${ESC}7minverse${ESC}0m`,
  Array.from({ length: 8 }, (_, i) => `${ESC}${30 + i}m ${['black', 'red', 'green', 'yellow', 'blue', 'magenta', 'cyan', 'white'][i].padEnd(7)}${ESC}0m`).join(''),
  Array.from({ length: 8 }, (_, i) => `${ESC}${90 + i}m ${['black', 'red', 'green', 'yellow', 'blue', 'magenta', 'cyan', 'white'][i].padEnd(7)}${ESC}0m`).join(''),
  '',
].join('\r\n')
/** A fixture socket: `ready` at once (never the lease), then a screenful of output. Nothing leaves this page. */
function fixtureConnect(_ticket: string, h: TermHandlers): TermConnection {
  const timer = setTimeout(() => {
    h.ready({ start: 0, offset: 0, cols: 100, rows: 30, writer: false, state: current === 'terminal-stop' ? 'exited' : 'running' })
    const text = current === 'terminal-colours' ? COLOUR_SAMPLE : Array.from({ length: 40 }, (_, i) => `demo $ echo line ${i} ${'-'.repeat(i % 90)}\r\n`).join('')
    h.output(0, new TextEncoder().encode(text))
    if (current === 'terminal-stop') h.exit(1, null)
  }, 50)
  return { sendInput() {}, resize() {}, close: () => clearTimeout(timer) }
}

/** The roster a scenario reads: the readers fixture. */
function rosterFor(scenario: LayoutScenario): RosterMember[] {
  if (scenario === 'settings-approvals-stuck') return readersRoster().slice(0, 1)
  return readersRoster()
}
const DAY = 86_400_000
const T0 = Date.parse('2026-10-09T11:30:00Z')
/** Every Drop error the page explains, for the `drop-errors` scenario and the tests. */
export const DROP_SAMPLE_ERRORS: DropError[] = [
  new DropError(413, { code: 'drop.too_large', message: 'drop.too_large', retryable: false }, { limit_bytes: 25 * 1024 * 1024 }),
  new DropError(429, { code: 'drop.quota', message: 'drop.quota', retryable: false }, { quota: 'drop_ws', limit: 512 * 1024 * 1024, used: 511 * 1024 * 1024 }),
  new DropError(429, { code: 'drop.quota', message: 'drop.quota', retryable: false }, { quota: 'drop_device_documents', limit: 64, used: 64 }),
  new DropError(429, { code: 'drop.quota', message: 'drop.quota', retryable: false }, { quota: `drop_new_${LONG_WORD}`, limit: 1, used: 1 }),
  new DropError(429, { code: 'drop.version_cap', message: 'drop.version_cap', retryable: false }),
  new DropError(409, { code: 'drop.version_conflict', message: 'drop.version_conflict', retryable: false }),
  new DropError(410, { code: 'drop.expired', message: 'drop.expired', retryable: false }),
  new DropError(410, { code: 'drop.pruned', message: 'drop.pruned', retryable: false }),
  new DropError(503, { code: 'drop.unavailable', message: 'drop.unavailable', retryable: false }),
  new DropError(403, { code: 'drop.not_visible', message: 'drop.not_visible', retryable: false }),
  new DropError(503, { code: 'drop.overloaded', message: 'drop.overloaded', retryable: true, retry_after_ms: 4200 }),
]
const dropOptions = (scenario: LayoutScenario): DropFixtureOptions => {
  const doc = (over: Parameters<typeof dropItem>[0] = {}) => dropItem({ kind: 'document', versions: over?.version ?? 1, expires_ms: null, ...over })
  const mixed = () => [
    dropItem({ name: 'quarterly-report.pdf' }),
    dropItem({ name: 'build-log.txt', mime: 'text/plain', author: AGENT }),
    dropItem({ name: 'scan-from-phone.jpg', mime: 'image/jpeg', claimed: false }),
    doc({ object_id: 'demo-doc-notes', name: 'meeting-notes.md', mime: 'text/markdown', version: 37, versions: 37 }),
    dropItem({ name: 'old-export.zip', mime: 'application/zip', state: 'pruned' }),
    dropItem({ name: 'expired-photo.heic', mime: 'image/heic', state: 'expired', expires_ms: T0 - DAY, tombstone_until_ms: T0 + 29 * DAY }),
    dropItem({ name: 'deleted-contract.pdf', state: 'retired', tombstone_until_ms: T0 + 30 * DAY }),
    ...Array.from({ length: 4 }, (_, i) => dropItem({ name: `invoice-${i + 1}.pdf` })),
  ]
  const sent = () => [dropItem({ name: 'handover.pdf', author: ME }), dropItem({ name: 'diagram.svg', mime: 'image/svg+xml', author: ME })]
  switch (scenario) {
    case 'drop-unavailable':
      return { served: false }
    case 'drop-list':
      return { received: mixed(), sent: sent(), pageSize: 8 }
    case 'drop-long-names':
      return {
        received: [
          dropItem({ name: `${LONG_WORD}.pdf`, mime: `application/vnd.${LONG_WORD}` }),
          dropItem({ name: '<img src=x onerror=alert(1)>.html', mime: 'text/html' }),
          dropItem({ name: 'invoice\u202Efdp.exe', mime: 'application/octet-stream', author: AGENT }),
          dropItem({ name: 'line one\nline two\u0000.txt', mime: 'text/plain' }),
          dropItem({ name: null, name_hidden: true }),
          dropItem({ name: `${'a very long file name with many words '.repeat(5)}.docx`, author: { ...PHONE, person_name: `Severin ${LONG_WORD}`, device: `d_${LONG_WORD.toLowerCase()}` } }),
          dropItem({ name: `${LONG_URL}`, claimed: false }),
        ],
      }
    case 'drop-phone': {
      const share = (over: Parameters<typeof dropItem>[0]) =>
        dropItem({ shared_from: PHONE_SHARE, claimed_ms: T0 - DAY, shared_until_ms: T0 + 6 * DAY, can_retire: 'self', ...over })
      return {
        received: [
          share({ name: 'boarding-pass.pdf' }),
          share({ name: 'photo-without-confirmation.jpg', mime: 'image/jpeg', gestureless: true }),
          share({ name: `${LONG_URL}.png`, mime: 'image/png', shared_from: { ...PHONE_SHARE, label: `iPhone ${LONG_WORD}` }, can_retire: 'handoff' }),
          share({ name: 'old-scan.pdf', state: 'expired', expires_ms: T0 - DAY, tombstone_until_ms: T0 + 29 * DAY, can_retire: null }),
          share({ name: 'removed-by-owner.pdf', state: 'retired', tombstone_until_ms: T0 + 30 * DAY, can_retire: null }),
          dropItem({ name: 'awaiting.jpg', claimed: false, unclaimed_reason: 'awaiting_claim', can_retire: null }),
          dropItem({ name: 'revoked-phone.jpg', claimed: false, unclaimed_reason: 'author_revoked', can_retire: null }),
          dropItem({ name: 'needs-terminal.jpg', claimed: false, unclaimed_reason: 'needs_handoff', can_retire: 'handoff' }),
          dropItem({ name: 'from-the-future.jpg', claimed: false, unclaimed_reason: 'brand_new_reason' }),
          dropItem({ name: 'plain-file.pdf' }),
        ],
        pendingOthers: 3,
        pageSize: 20,
      }
    }
    case 'drop-upload-progress':
      return { received: mixed().slice(0, 3), stallAfterChunks: 2 }
    case 'drop-errors':
      return { received: mixed().slice(0, 4), readError: { '*': DROP_SAMPLE_ERRORS[1] }, sendError: DROP_SAMPLE_ERRORS[1] }
    case 'drop-versions':
      return {
        received: [doc({ object_id: 'demo-doc-full', name: 'shared-plan.md', mime: 'text/markdown', version: 1024, versions: 1024 }), doc({ object_id: 'demo-doc-notes', name: 'meeting-notes.md', version: 37, versions: 37 })],
        versions: {
          'demo-doc-full': Array.from({ length: 1024 }, (_, i) => dropVersion(1024 - i, { state: i < 8 ? 'live' : 'pruned', author: i % 5 === 0 ? AGENT : PHONE })),
          'demo-doc-notes': Array.from({ length: 37 }, (_, i) => dropVersion(37 - i)),
        },
        pageSize: 12,
      }
    default:
      return {}
  }
}
const dropStart = (scenario: LayoutScenario): DropPreviewStart | undefined => {
  if (scenario === 'drop-versions') return { openDoc: 'demo-doc-full' }
  if (scenario === 'drop-upload-progress') return { upload: new File([new Uint8Array(3 * 1024 * 1024)], `quarterly-figures-${LONG_WORD}.xlsx`, { type: 'application/vnd.ms-excel' }) }
  return undefined
}
let undoDrop: (() => void) | null = null
let dropOverride: DropFixtureOptions | null = null
let dropFixture: DropFixture | null = null
/** Tests: options merged into the next drop scenario, and the fixture it installed. */
export const dropPreviewForTests = {
  set: (o: DropFixtureOptions | null) => void (dropOverride = o),
  fixture: () => dropFixture,
}

let current: LayoutScenario = 'quick-normal'
let installed = false
const originals = new Map<string, unknown>()

/** Test cleanup: undo every mock api swap before another test uses the shared client. */
export function resetLayoutPreview() {
  idleResumed = false
  const a = api as unknown as Record<string, unknown>
  const r = runsApi as unknown as Record<string, unknown>
  for (const [name, fn] of originals) {
    if (name === 'runs.connect') termTransport.connect = fn as typeof termTransport.connect
    else if (name.startsWith('runs.')) r[name.slice(5)] = fn
    else if (name === 'shares.list') (sharesApi as unknown as Record<string, unknown>).list = fn
    else if (name === 'routing.choices') (routingApi as unknown as Record<string, unknown>).choices = fn
    else if (name.startsWith('agents.')) (agentsApi as unknown as Record<string, unknown>)[name.slice(7)] = fn
    else a[name] = fn
  }
  originals.clear()
  undoDrop?.()
  undoDrop = null
  dropOverride = null
  dropFixture = null
  installed = false
  current = 'quick-normal'
}

/** Replaces the mock api's methods that these pages call. Done once; the answers follow `current`. */
function install() {
  if (installed) return
  installed = true
  const a = api as unknown as Record<string, (...args: never[]) => unknown>
  const original = { getIdentity: a.getIdentity, getTicket: a.getTicket, clickStatus: a.clickStatus, switchClick: a.switchClick, custodySeal: a.custodySeal }
  const set = (name: string, fn: (...args: never[]) => unknown) => {
    originals.set(name, a[name])
    a[name] = fn
  }
  set('getQuick', async (_ws: string, p: { status?: string[] } = {}) => {
    const done = (p.status ?? []).includes('done')
    const rows = quickRows(current)
    return { available: true, rows: done ? [quickRow(900, { status: 'done', title: 'Drop the old logo' })] : rows, next_page: current === 'quick-many' ? 'page-2' : null, total: done ? 1 : rows.length }
  })
  set('getRecords', async () => records(current))
  set('getRecordsCurrent', async () => ({ ...records(current), remote: { ...records(current).remote!, current_url: LONG_URL + (current === 'records-push-mismatch' ? '/changed' : ''), current_url_hidden: false, current_private: true, matches_last: current !== 'records-push-mismatch' } }))
  // M7a: the real-host dock's routes and its socket answer from fixtures.
  const r = runsApi as unknown as Record<string, unknown>
  for (const name of ['list', 'preview', 'open', 'stop', 'ticket']) originals.set(`runs.${name}`, r[name])
  originals.set('runs.connect', termTransport.connect)
  r.list = async () => runList(current)
  r.preview = async () => ({
    spec: 'f'.repeat(64), display: `$SHELL -l -c 'exec "$0" "$@"' ${LONG_WORD}`, display_hidden: false, cwd_path: `/Users/demo/${'deeply-nested-folder/'.repeat(6)}billing`,
    cwd_path_hidden: false, notice: 'acts as you, not as an agent', as_person: true, login_shell: true, env: ['PATH', 'HOME', 'LANG', 'TERM', 'ORCH_TICKET', 'SHELL', 'USER', 'LOGNAME'],
    blocked: null, warnings: [`The working copy ${LONG_URL} has uncommitted changes.`],
  })
  r.ticket = async () => ({ ticket: 'layout-preview', expires_in_ms: 30000 })
  r.stop = async () => ({ state: 'exited', cleanup: 'incomplete' })
  termTransport.connect = fixtureConnect
  // M7b: the Start agent dialog's routes answer from fixtures.
  const ag = agentsApi as unknown as Record<string, unknown>
  for (const name of ['runnerReview', 'runnerSwitch', 'preview', 'runner', 'start', 'depositStatus', 'forget', 'riskNotices', 'acknowledge']) originals.set(`agents.${name}`, ag[name])
  ag.preview = async () => agentPreview(current)
  ag.runnerReview = async (_ws: string, b: { on: boolean; terminals?: boolean }) => ({
    view: { op: b.on ? 'runner.on' : 'runner.off', on: b.on, terminals: b.on && !!b.terminals, lines: b.on
      ? ['Turn on dashboard execution for Demo.', 'Agents may run commands on this computer as you when you start them.', 'Agents and terminals you start here work as you on this computer. They can run programs and open your files, including secret keys, and so can anything that takes over this dashboard. Only continue on a computer you trust.', 'That includes reading your SSH keys and your orch keys. The dashboard can then start agents, create worktrees and open terminals for this workspace, as you.']
      : ['Turn off dashboard execution for Demo.', 'Open terminals close within a second; agents already running keep running; the dashboard starts no new ones.'] },
    view_digest: 'sha256:' + 'a'.repeat(64),
  })
  ag.runnerSwitch = async (_ws: string, b: { on: boolean; terminals?: boolean }) => ({ enabled: b.on, terminals: b.on && !!b.terminals })
  ag.riskNotices = async () => ({ acknowledged: current === 'agent-dialog-acked' ? { start_agent: true, login_shell: true } : {} })
  ag.acknowledge = async () => ({ acknowledged: { start_agent: true } })
  originals.set('shares.list', sharesApi.list)
  ;(sharesApi as unknown as Record<string, unknown>).list = async () => ({ available: true, configured: current !== 'addons-publish-unset', server: current === 'addons-publish-unset' ? null : 'https://share.example.org', share: current === 'addons-publish-unset' ? null : 'https://view.example.org', workspace: 'w'.repeat(32), owner: 'p_owner', live: true, shares: [], staged: [] })
  originals.set('routing.choices', routingApi.choices)
  ;(routingApi as unknown as Record<string, unknown>).choices = async () => ROUTING_CHOICES
  ag.runner = async () => ({ runner: { connected: true, enabled: current !== 'agent-blocked', terminals: false, grant: current === 'agent-dialog' || current === 'agent-dialog-acked' || current === 'new-sequence-start' ? { id: AGENT_GRANT, until: new Date(nowMs() + 3 * 86_400_000).toISOString(), scope: 'workable' } : current === 'agent-pill' ? { id: AGENT_GRANT, until: new Date(nowMs() + 6 * 3_600_000).toISOString(), scope: 'workable' } : null } })
  ag.start = async () => { throw new ApiError(409, { code: 'run.limit', message: `8 agent runs at most ${LONG_WORD}`, retryable: true }) }
  ag.depositStatus = async () => ({ deposit: 'unknown' })
  ag.forget = async () => ({ forgotten: true })
  // "Make a normal ticket" reads the ticket fresh: answer with the quick row's labels and status.
  set('getTicket', async (...args: never[]) => {
    const key = args[0] as string
    const row = quickRows(current).find((r) => r.key === key)
    const base = (await (original.getTicket as (...a: never[]) => Promise<object>)(...args).catch(() => ({ key }))) as object
    return row ? { ...base, key, labels: row.labels, status: row.status } : base
  })
  set('recordsCommit', async (_ws: string, push: boolean) => {
    if (current === 'records-push-failed' && push) throw new ApiError(502, { code: 'records.push_failed', message: `push to ${LONG_URL} was refused`, retryable: true, commit: 'c'.repeat(40) })
    return { ok: true, commit: 'd'.repeat(40), pushed: push, url: push ? LONG_URL : null }
  })
  set('recordsAutoOff', async () => ({ ok: true }))
  set('recordsInit', async () => ({ ok: true }))
  set('recordsMark', async () => ({ ok: true }))
  set('recordsPushInitial', async (_ws: string, url: string) => ({ ok: true, commit: 'd'.repeat(40), pushed: true, url }))
  set('getIdentity', async (...args: never[]) => ({ ...((await (original.getIdentity as (...a: never[]) => Promise<object>)(...args)) as object), runner: { connected: true, enabled: current === 'runner-on', terminals: true } }))
  // The signing card: the same card on every read; a switch answers with "result unknown" (the card's own reload follows).
  // Only the signing-* scenarios answer from the fixture; every other scenario keeps the api's own method.
  set('getHostInfo', async () => {
    if (current === 'dev-status-failed') throw new ApiError(502, { code: 'host.unavailable', message: 'fixture: no answer', retryable: true })
    return hostInfoFor(current)
  })
  set('clickStatus', async (...args: never[]) => (current.startsWith('signing-') ? signingCard(current) : current === 'readers-solo' ? { custody: 'convenience', enabled: true, available: true, device: 'd_00112233445566778899aabbccddeeff' } : original.clickStatus(...args)))
  // The sealed new passphrase (C12): the fixture's workspace id is not a real one (the seal binds a 32-hex id), so the
  // preview shows the refusal a remote dashboard gets; the real seal is covered by SigningCard.test.tsx.
  set('custodySeal', async (...args: never[]) => {
    if (!current.startsWith('signing-')) return (original.custodySeal as (...a: never[]) => Promise<object>)(...args)
    throw new ApiError(409, { code: 'human.click_refused', message: 'fixture', retryable: false })
  })
  // #1198 C13: Settings → Devices shows "This computer's key"; the review answers from a fixture (the seal itself is
  // covered by ReplaceKey.test.tsx: the fixture's workspace id is not a real one).
  set('rotateSeal', async (ws: string) => ({
    request_id: 'hr_01M5PVRB46WS3X8H5X2RQK04S9',
    view_digest: `sha256:${'a'.repeat(64)}`,
    expires_in: 300,
    view: {
      op: 'device.rotate', workspace: { id: ws, name: 'billing', prefix: 'DEMO' },
      old: `d_${'1'.repeat(32)}`, new: `d_${'2'.repeat(32)}`, confirm: { kind: 'click' },
      lines: ['Replace the signing key of this computer for billing.', 'The old key stops signing everywhere, also while a hub is down. The new key is signed by your recovery code for this workspace only; nothing is re-shared with a hub.', `old device: d_${'1'.repeat(32)}`, `new device: d_${'2'.repeat(32)}`, ROTATE_LINE],
      rotate_line: ROTATE_LINE, seal: { v: 1, alg: 'P256-HKDF-SHA256-A256GCM', pk: `B${'Q'.repeat(86)}`, purposes: ['device.recovery_code', 'custody.passphrase'] },
    },
  }))
  set('switchClick', async (...args: never[]) => {
    if (!current.startsWith('signing-')) return original.switchClick(...args)
    if (current === 'signing-idle-off') {
      idleResumed = true
      return signingCard(current)
    }
    throw new TypeError('fixture: the connection was lost')
  })
  set('getAddOns', async () => ({
    available: true,
    add_ons: [
      { id: 'dashboard', title: 'Dashboard', desc: "This workspace's dashboard (locked on).", state: 'on', locked: 'The dashboard is always on: it is what shows this workspace.', blocked: null },
      { id: 'start-agent', title: 'Start agents', desc: 'Start an agent from the dashboard. It runs on this computer, as you.', state: 'on', locked: null, blocked: 'Stop first: an agent of yours is running right now.' },
      { id: 'terminals', title: 'Terminals', desc: 'Open a terminal on this computer from the dashboard. It runs as you.', state: 'off', locked: null, blocked: 'Needs dashboard execution first. Turn it on here, then switch this on.', runner_switch: true },
      { id: 'worktrees', title: 'Work copies', desc: 'Each ticket gets its own copy of your project folder, so agents do not overwrite each other.', state: 'off', locked: null, blocked: null },
      { id: 'publish', title: 'Publish', desc: 'Share files of a ticket with a link.', state: current.startsWith('addons-publish') ? 'on' : 'off', locked: null, blocked: null },
      { id: 'quick', title: 'Quick tasks', desc: 'Small one-off tasks. Always available; there is nothing to switch on.', state: 'on', locked: 'Always available. There is nothing to switch.', blocked: null },
    ],
  }))
  set('getHostSettings', async () => ({
    available: true,
    grant_hours: current.startsWith('new-sequence-start') ? 168 : 8,
    claim_ttl_min: 120,
    lease_ttl_min: 60,
    defaults: ['claim_ttl_min', 'lease_ttl_min'],
    gates: current.startsWith('settings-approvals') ? settingsGates(current) : {},
    repos: current === 'settings-general' ? [] : [{ name: 'code', default_branch: 'main', remote: LONG_URL }],
    repos_more: false,
    settings_at: null,
    policy_at: null,
  }))
  // H12d-2: `/members` and `/readers` in the real shape. The pair scenarios read the pair fixture's members.
  set('getRoster', async () => rosterFor(current))
  set('listHostGrants', async (_ws: string, active: boolean) => accessGrants(current).filter((g) => !active || (!g.revoked && Date.parse(g.expires_at) > Date.now())))
  set('getReaders', async () => current === 'readers-solo' ? { available: true, required_reader: 1, readers: [{ device: 'd_00112233445566778899aabbccddeeff', person: 'p_me', reader: 0, level: 0 }] } : readersOf(rosterFor(current), current === 'readers-devices' || current === 'settings-members' ? 1 : 0))
}

/**
 * The Wiki view (the declarative nav page of the wiki add-on) over a faked server: no wiki folder (`wiki-empty`), a few
 * pages (`wiki-created`, with the first open), or a long list (`wiki-many`). The node is the package's own view.
 */
function WikiPreview({ scenario }: { scenario: string }) {
  const [ready] = useState(() => {
    const real = window.fetch.bind(window)
    window.fetch = async (input, init) => {
      const url = new URL(typeof input === 'string' ? input : input instanceof URL ? input.href : input.url, window.location.href)
      const read = url.pathname.split('/view/')[1]
      if (!read) return real(input, init)
      const pages = scenario === 'wiki-many' ? WIKI_PREVIEW_PAGES.concat(Array.from({ length: 140 }, (_, i) => ({ id: `note-${i}`, title: `Meeting notes ${i + 1}`, markdown: `# Meeting notes ${i + 1}\n\nShort note.` }))) : WIKI_PREVIEW_PAGES
      const json = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } })
      if (read === 'wiki.tree')
        return scenario === 'wiki-empty'
          ? json({ state: 'no_folder', message: 'No wiki folder in this project yet.', nodes: [], total: 0, cut: false, offer: { label: 'Create the wiki folder', action: 'create' } })
          : json({ state: 'ok', message: null, nodes: pages.map((p) => ({ id: p.id, label: p.title, hint: p.id, ...('badge' in p ? { badge: p.badge } : {}) })), total: pages.length, cut: false })
      if (read === 'wiki.page') {
        const p = pages.find((x) => x.id === url.searchParams.get('id'))
        return p ? json({ id: p.id, title: p.title, markdown: p.markdown, updated: '2026-10-20T09:30:00Z', by: 'Mara' }) : json({ error: { code: 'not_found', message: 'x', retryable: false } })
      }
      const q = (url.searchParams.get('q') ?? '').toLowerCase()
      const hits = pages.filter((p) => p.title.toLowerCase().includes(q) || p.markdown.toLowerCase().includes(q))
      return json({ results: hits.map((p) => ({ id: p.id, label: p.title, snippet: p.markdown.split('\n').find((l) => l.toLowerCase().includes(q) && !l.startsWith('#')) ?? p.title })), total: hits.length, cut: false })
    }
    return true
  })
  return ready ? (
    <div className="w-full space-y-4">
      <h1 className="text-xl font-semibold tracking-tight">Wiki</h1>
      <AddonNode node={WIKI_PREVIEW_NODE} addon="wiki" />
    </div>
  ) : null
}

export function LayoutPreview({ name }: { name: string }) {
  const qc = useQueryClient()
  const { workspace } = useWorkspace()
  const [scenario] = useState<LayoutScenario | null>(() => {
    if (!(LAYOUT_SCENARIOS as readonly string[]).includes(name)) return null
    install()
    current = name as LayoutScenario
    if (current.startsWith('drop-')) {
      undoDrop?.()
      dropFixture = createDropFixture({ ...dropOptions(current), ...dropOverride })
      undoDrop = installDropFixture(dropFixture)
    }
    // Nothing of an earlier scenario stays in the cache.
    for (const key of ['quick', 'records', 'identity', 'runs', 'runner', 'drop', 'members', 'readers', 'click-status', 'add-ons']) qc.removeQueries({ queryKey: [key] })
    // The topbar is already mounted and watches this one: it is refetched, not removed.
    void qc.invalidateQueries({ queryKey: ['host-info'] })
    return current
  })
  if (!scenario) return <p role="alert">Unknown layout scenario {name}.</p>
  if (!workspace) return null
  if (scenario === 'quick-off') {
    // The host serves quick tasks, the add-on is off: the page says so (features `quick` without `quick_on`).
    setFeatures({ features: ['today', 'tickets', 'board', 'ticket', 'quick'] })
    return <QuickPage />
  }
  if (scenario.startsWith('wiki-')) {
    setFeatures({ features: ['today', 'tickets', 'board', 'ticket', 'wiki', 'addons'] })
    return <WikiPreview scenario={scenario} />
  }
  if (scenario.startsWith('quick-')) return <QuickPage />
  if (scenario.startsWith('terminals-page')) return <TerminalsPage />
  if (scenario.startsWith('terminal-')) return <TerminalPreview scenario={scenario} />
  if (scenario === 'new-sequence') return <NewSequencePreview />
  if (scenario === 'new-sequence-start' || scenario === 'new-sequence-start-nogrant') return <NewSequencePreview start />
  if (scenario === 'edit-ticket') return <EditTicketPreview />
  if (scenario === 'paste-sign') return <PasteSignPreview />
  if (scenario.startsWith('sign-b-')) return <SignBPreview scenario={scenario} ws={workspace.id} />
  if (scenario === 'setup-words') return <div className="setup max-w-xl p-4"><ConfirmWords words={['apple', 'river', 'cloud']} /></div>
  if (scenario === 'agent-pill' || scenario === 'agent-pill-stopped' || scenario === 'agent-pill-off')
    return <div className="p-4"><AgentAccess ws={workspace.id} previewStopped={scenario === 'agent-pill-stopped'} /></div>
  if (scenario.startsWith('agent-'))
    return (
      <StartAgentDialog ws={workspace.id} ticket="DEMO-0043" open choose onClose={() => {}} />
    )
  if (scenario.startsWith('drop-'))
    return (
      <div className="min-w-0 space-y-6">
        <DropView workspace={workspace} preview={dropStart(scenario)} />
        {scenario === 'drop-errors' && (
          <section aria-label="Every Drop error (layout preview)" data-testid="drop-error-gallery" className="max-w-4xl space-y-3">
            <h2 className="text-base font-semibold">Every Drop error (layout preview)</h2>
            {DROP_SAMPLE_ERRORS.map((e, i) => (
              <DropErrorLine key={i} detail={dropError(e, { max_bytes: 25 * 1024 * 1024, version_cap: 1024 })} />
            ))}
          </section>
        )}
      </div>
    )
  if (scenario === 'dev-refused')
    return (
      <section aria-label="Every dev host refusal (layout preview)" data-testid="dev-refused-gallery" className="max-w-4xl space-y-3">
        <h2 className="text-base font-semibold">Every dev host refusal (layout preview)</h2>
        {devRefusedSamples().map((sample) => (
          <div key={sample.title} data-testid="dev-refused-sample" className="space-y-1">
            <p className="font-mono text-[12px] text-text-faint">{sample.title}</p>
            <ClickProblemBox problem={clickProblem(sample.error)} ws={workspace.id} />
          </div>
        ))}
      </section>
    )
  if (scenario === 'convenience-refusals')
    return (
      <section aria-label="Every click-to-sign refusal (layout preview)" data-testid="refusal-gallery" className="max-w-4xl space-y-3">
        <h2 className="text-base font-semibold">Every click-to-sign refusal (layout preview)</h2>
        {refusalSamples().map((sample) => (
          <div key={sample.title} data-testid="refusal-sample" className="space-y-1">
            <p className="font-mono text-[12px] text-text-faint">{sample.title}</p>
            <ClickProblemBox problem={clickProblem(sample.error)} ws={workspace.id} />
          </div>
        ))}
        <div data-testid="refusal-sample" className="space-y-1">
          <p className="font-mono text-[12px] text-text-faint">signer status · readers_old</p>
          <p data-testid="signer-readers-old" className="rounded-md border border-warning/40 px-3 py-2 text-[13px] text-text-muted">{signerProblem('readers_old')}</p>
        </div>
      </section>
    )
  if (scenario === 'readers-devices' || scenario === 'readers-solo')
    return (
      <SettingsFrame current="devices" addon={undefined} ws={workspace.id} tabs={HOST_TABS}>
        <HostDevices workspace={workspace} />
      </SettingsFrame>
    )
  if (scenario === 'settings-general') return <SettingsFrame current="general" addon={undefined} ws={workspace.id} tabs={HOST_TABS}><HostGeneral workspace={{ ...workspace, click: signingCard('signing-off'), icon_known: true }} /></SettingsFrame>
  if (scenario === 'settings-members') return <SettingsFrame current="members" addon={undefined} ws={workspace.id} tabs={HOST_TABS}><HostMembers workspace={workspace} /></SettingsFrame>
  if (scenario.startsWith('settings-approvals')) return <SettingsFrame current="gates" addon={undefined} ws={workspace.id} tabs={HOST_TABS}><HostGates workspace={workspace} /></SettingsFrame>
  if (scenario.startsWith('settings-access')) return <SettingsFrame current="grants" addon={undefined} ws={workspace.id} tabs={HOST_TABS}><HostGrants workspace={workspace} /></SettingsFrame>
  if (scenario.startsWith('records-'))
    return (
      <SettingsFrame current="records" addon={undefined} ws={workspace.id} tabs={HOST_TABS}>
        <RecordsBlock workspace={workspace} />
      </SettingsFrame>
    )
  return (
    <SettingsFrame current="general" addon={undefined} ws={workspace.id} tabs={HOST_TABS}>
      <HostGeneral workspace={scenario.startsWith('signing-') ? signingWorkspace(scenario, workspace) : scenario === 'dev-badge-long' ? { ...workspace, name: SIGNING_LONG_NAME, prefix: SIGNING_LONG_PREFIX } : workspace} />
    </SettingsFrame>
  )
}

/** The real-host dock at its default bottom size, under a stand-in page area (the guard checks it at 720 px too). */
function TerminalPreview({ scenario }: { scenario: LayoutScenario }) {
  const view = { width: typeof window === 'undefined' ? 1440 : window.innerWidth, height: typeof window === 'undefined' ? 900 : window.innerHeight }
  return (
    <div className="flex h-[640px] min-w-0 flex-col">
      <p className="p-3 text-xs text-text-muted">Real-host terminal dock ({scenario}): fixture runs and a fixture socket.</p>
      <div className="flex-1" />
      <RealDock prefs={{ open: true, side: 'bottom', bottom: 360, right: 480, harness: 'shell' } as never} side="bottom" size={360} view={view} area={view.width}
        rightFits={false} setPrefs={() => {}} focus={{ current: null }} memory={new Map()} collapse={() => {}} autoPreview={scenario === 'terminal-preview'} />
    </div>
  )
}

/** The real ticket page in hand-off mode (a real host): Edit opens the form, Review and sign the one review with fixture answers. */
function EditTicketPreview() {
  useEffect(() => {
    setFeatures({ features: ['actions'] })
    return () => setFeatures(null)
  }, [])
  const [on, setOn] = useState(false)
  useEffect(() => setOn(true), [])
  return on ? <TicketPage ticketKey="DEMO-0043" /> : null
}

/** The real New ticket form in hand-off mode (a real host): Create opens the one review with fixture answers. */
function NewSequencePreview({ start = false }: { start?: boolean }) {
  useEffect(() => {
    setFeatures({ features: start ? ['actions', 'agent_start', 'model_routing'] : ['actions'] })
    return () => setFeatures(null)
  }, [])
  const ready = useRef(false)
  const [on, setOn] = useState(false)
  useEffect(() => {
    ready.current = true
    setOn(true)
  }, [])
  return on ? <NewTicketPage /> : null
}

/** R3 part B: the real section editor (paste a picture into it) and the real sign panel with a View that names the pasted
 * pictures: once as the signer would build it, once as a host that swapped the file would. Nothing here signs. */
function PasteSignPreview() {
  const [text, setText] = useState('The export button does nothing when I click it.')
  const pics = pastedIn([text])
  const view = (images: { name: string; sha256: string; bytes: number; alt: string }[]): ClickViewAnswer => ({
    view_digest: 'sha256:' + 'a'.repeat(64),
    view: {
      risk: CLICK_RISK,
      op: 'new',
      workspace: { id: 'f'.repeat(32), name: 'Acme energy data', prefix: 'DEMO' },
      fingerprint: 'abcdef012345',
      lines: ['Create DEMO-0044'],
      checks: [],
      confirm: { kind: 'click' },
      expires_at: new Date(Date.now() + 600_000).toISOString().replace(/\.\d+Z$/, 'Z'),
      auth: 'convenience',
      device: PLACEHOLDER_DEVICE,
      artifacts: 'checked',
      headline: 'Create DEMO-0044',
      content: {
        headline: 'Create DEMO-0044 (key per host; signed id …0000000A): Export does nothing',
        title: 'New ticket Export does nothing',
        subline: 'Includes its text (1 section)',
        ticket: { key: 'DEMO-0044', title: 'Export does nothing', uid_tail: '0000000A', new: true, key_note: 'key per host, not proven' },
        sections: [{ id: 'context', title: 'Context', text }],
        images,
        notes: [`Adds ${images.length} screenshot${images.length === 1 ? '' : 's'}. The dashboard shows each one from the file you pasted and checks its digest against the one signed.`],
      },
    },
  })
  const swapped = pics.map((p) => ({ ...p, sha256: `sha256:${'0'.repeat(64)}` }))
  return (
    <div className="mx-auto max-w-2xl space-y-8 p-4">
      <Section type="bug" name="context" value={text} onChange={setText} />
      <section aria-label="What you sign" className="space-y-2">
        <h2 className="text-[13px] font-medium">The sign dialog</h2>
        <ClickSignPanel answer={view(pics)} problem={null} signing={false} onSign={() => {}} onCancel={() => {}} />
      </section>
      <section aria-label="If the host swapped the file" className="space-y-2">
        <h2 className="text-[13px] font-medium">If the host had swapped the file</h2>
        <ClickSignPanel answer={view(swapped)} problem={null} signing={false} onSign={() => {}} onCancel={() => {}} />
      </section>
    </div>
  )
}

const SIGN_B_PERSON = 'p_2e954b7df6ad0c419ab05d2136b7e842'
/** R3 variant B: the real panel with a fixture View, in the width of a dialog. */
function SignBPreview({ scenario, ws }: { scenario: string; ws: string }) {
  const kind = scenario.slice('sign-b-'.length)
  const fields = [{ field: 'type', before: null, after: 'feature' }, { field: 'owner', before: null, after: SIGN_B_PERSON }, { field: 'estimate', before: null, after: '3 points (size M)' }]
  const content: import('@/api/signing').SignContent = kind === 'extend'
    ? { headline: 'Extend agent access', title: 'Extend agent access', fields_changed: [{ field: 'who', before: null, after: 'Agents working for you (Claude Code)' }, { field: 'owner', before: null, after: SIGN_B_PERSON }, { field: 'computer', before: null, after: 'Sevi’s MacBook' }, { field: 'until', before: 'Thu 22 Oct, 16:34', after: 'Thu 22 Oct, 16:34 (replaced)' }] }
    : { headline: kind === 'new' || kind === 'new-access' ? 'Create ticket' : kind === 'requirements' ? 'Approve requirements' : 'Approve plan', title: kind.startsWith('new') ? 'Create ticket' : kind === 'requirements' ? 'Approve requirements' : 'Approve plan', ticket: { key: 'TESTOR-0007', title: 'Export report as PDF', uid_tail: '9f3c1a', key_note: '', new: kind.startsWith('new') },
      sections: [{ id: 'requirements', title: 'Requirements', text: 'Export button on the report page\nPDF has the company logo\nWorks for reports up to 200 rows' }, ...(kind === 'plan' ? [{ id: 'plan', title: 'Plan you are approving', text: '1. Add an Export button.\n2. Generate a PDF with the logo.\n3. Check a short and a 200-row report.' }] : [])], fields_changed: fields }
  const action = (kind === 'new' ? { action: 'new_sequence', title: 'Export report as PDF', type: 'feature', sections: {} }
    : kind === 'new-access' ? { action: 'new_sequence', title: 'Export report as PDF', type: 'feature', sections: {}, agent: { hours: 168 } }
    : kind === 'requirements' ? { action: 'approve', key: 'TESTOR-0007', gate: 'requirements' }
    : kind === 'plan' ? { action: 'approve', key: 'TESTOR-0007', gate: 'plan' }
    : { action: 'runner_grant', hours: 72, scope: 'workable', extend: 'gr_old' }) as import('@/api/click').ClickAction
  const answer: import('@/api/click').ClickViewAnswer = { view_digest: 'sha256:' + 'a'.repeat(64), view: { risk: CLICK_RISK, op: 'x', workspace: { id: ws, name: 'Sevi’s workspace', prefix: 'TESTOR' }, fingerprint: '7d8b2e954b7d', lines: ['x'], checks: [], confirm: { kind: 'click' }, expires_at: new Date(nowMs() + 600_000).toISOString(), auth: 'convenience', device: 'Sevi’s MacBook · local signing key', artifacts: null, content } }
  return <div className="mx-auto max-w-lg space-y-4 rounded-lg border border-border bg-surface p-4"><h2 className="text-base font-semibold">{content.title}</h2><ClickSignPanel ws={ws} answer={answer} problem={null} signing={false} onSign={() => {}} onCancel={() => {}} story={storyOf(action)} /></div>
}

