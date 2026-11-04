// Production stand-in for mocks/addons/focus/live.ts (the mock host's live focus demo): a real host never shows it.
export const LIVE_TICKET = ''
export const resetLive = (_now?: number): void => {}
export const liveAct = (_ticket: string, _action: unknown, _now?: number): void => {}
export const thumb = (_i: number, _ready: boolean): string => ''
export const liveSnapshot = (_ticket: string, _now?: number): null => null
export const liveWorkLog = (_ticket: string): null => null
