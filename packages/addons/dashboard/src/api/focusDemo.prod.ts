// Production stand-in for mocks/addons/focus/demo.ts (the mock host's demo run): a real host never calls any of it.
const demoOnly = (): never => { throw new Error('Focus demo data is only available on the mock host.') }
export const DEMO_FOCUS_TICKET = ''
export const isDemoRun = (_run: string): boolean => false
export const demoTranscript = demoOnly
export const demoDelegations = demoOnly
export const demoDiff = demoOnly
export const demoRunList = demoOnly
export const demoLiveOn = (_ticket?: string): boolean => false
