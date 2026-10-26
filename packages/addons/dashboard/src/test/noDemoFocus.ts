// A stand-in for mocks/addons/focus/demo.ts: no demo run, no children, no diff. Tests of the focus screens that bring their
// own runs mock the demo module with this (`vi.mock('@/mocks/addons/focus/demo', () => import('@/test/noDemoFocus'))`).
export const DEMO_FOCUS_TICKET = 'DEMO-0043'
export const DEMO_MAIN_RUN = 'demo-main-unused'
export const DEMO_CHILD_RUN = 'demo-child-unused'
export const isDemoRun = () => false
export const demoRunList = () => ({ runs: [], runner: null, truncated: false })
export const demoDelegations = () => ({ ticket: DEMO_FOCUS_TICKET, supported: true, sessions: [] })
export const demoTranscript = () => ({ run: DEMO_MAIN_RUN, harness: 'claude-code', available: false, reason: null, items: [], next: null, eof: true })
export const demoDiff = () => ({ ticket: DEMO_FOCUS_TICKET, repo: null, available: false, reason: 'No demo', path_label: null, branch: null, head: null, checked_at: null, truncated: false, files: [], unified: '' })
export const demoSources = () => ({ run: DEMO_MAIN_RUN, available: false, reason: null, sources: [] })
