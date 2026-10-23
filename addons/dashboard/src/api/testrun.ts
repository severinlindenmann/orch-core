// The tester's checklist store (server: testrun.py). Not part of the workspace `api`: it needs no workspace, so it works
// before `orch setup`. Same-origin, bearer header only, like the other calls.
import { getToken } from './bootstrap'

export const STATUSES = ['todo', 'pass', 'fail_blocking', 'fail_non_blocking', 'skipped'] as const
export type StepStatus = (typeof STATUSES)[number]

export interface StepNotes {
  status: StepStatus
  note: string
  images: string[]
  updated?: string
  passed_at?: string
  hash?: string
  /** The person reopened a step carried over from an earlier run. */
  retest?: boolean
}

/** One earlier run, read only: the notes as they were, pictures only counted. */
export interface PreviousRun {
  run: string
  steps: Record<string, { status: StepStatus; note: string; images: number; updated: string | null; passed_at: string | null; hash: string | null }>
}

export interface TestrunState {
  /** Where the notes are kept on this computer. */
  path: string
  images_path: string
  notes: Record<string, StepNotes>
}

export const IMAGE_TYPES = ['image/png', 'image/jpeg', 'image/webp'] as const
export const IMAGE_MAX = 5 * 1024 * 1024

/** Whether sending the same save again can help: a network error (no status), a timeout, a rate limit or a server error. */
export const retryable = (e: unknown): boolean => !(e instanceof TestrunError) || e.status === 408 || e.status === 429 || e.status >= 500

export class TestrunError extends Error {
  constructor(public status: number, public code: string) {
    super(code)
  }
}

/** A keepalive request may carry at most 64 KB in the browser: bigger bodies use a normal fetch. */
export const KEEPALIVE_MAX = 60_000

async function call(method: string, path: string, body?: unknown, keepalive = false): Promise<Response> {
  const token = getToken()
  const headers: Record<string, string> = {}
  if (token) headers.Authorization = `Bearer ${token}`
  if (body !== undefined) headers['content-type'] = 'application/json'
  const payload = body === undefined ? undefined : JSON.stringify(body)
  const small = payload === undefined || new TextEncoder().encode(payload).length <= KEEPALIVE_MAX
  const res = await fetch(path, { method, headers, credentials: 'omit', redirect: 'error', cache: 'no-store', keepalive: keepalive && small, body: payload })
  if (!res.ok) {
    let code = 'error'
    try {
      code = ((await res.json()) as { error?: { code?: string } }).error?.code ?? code
    } catch {
      /* not JSON */
    }
    throw new TestrunError(res.status, code)
  }
  return res
}

const base = (run: string) => `/api/testrun/${encodeURIComponent(run)}`

export async function loadRun(run: string): Promise<TestrunState> {
  return (await (await call('GET', base(run))).json()) as TestrunState
}

/** The notes of earlier runs (lower run numbers), read only. */
export async function loadPrevious(run: string): Promise<PreviousRun[]> {
  return ((await (await call('GET', `${base(run)}/previous`)).json()) as { runs: PreviousRun[] }).runs
}

/** Saves only the fields that changed; the server merges them into the step it holds (an absent field stays as it is). */
export async function saveStep(run: string, step: string, patch: { status?: StepStatus; note?: string; hash?: string; retest?: boolean }): Promise<void> {
  await call('PUT', `${base(run)}/steps/${encodeURIComponent(step)}`, patch, true)
}

function toBase64(file: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(String(reader.result).split(',', 2)[1] ?? '')
    reader.onerror = () => reject(reader.error)
    reader.readAsDataURL(file)
  })
}

/** Why a file cannot be added, or null when it can (the server checks again). */
export function imageProblem(file: { type: string; size: number }): string | null {
  if (!(IMAGE_TYPES as readonly string[]).includes(file.type)) return 'Only PNG, JPEG or WebP pictures can be added.'
  if (file.size > IMAGE_MAX) return 'That picture is larger than 5 MB.'
  if (file.size === 0) return 'That picture is empty.'
  return null
}

export async function addImage(run: string, step: string, file: Blob): Promise<{ name: string; path: string }> {
  const data = await toBase64(file)
  return (await (await call('POST', `${base(run)}/steps/${encodeURIComponent(step)}/images`, { type: file.type, data })).json()) as { name: string; path: string }
}

export async function removeImage(run: string, name: string): Promise<void> {
  await call('DELETE', `${base(run)}/images/${encodeURIComponent(name)}`)
}

/** The picture as a data: URL (the page's CSP allows `data:` images, and the request needs the bearer header). */
export async function imageUrl(run: string, name: string): Promise<string> {
  const blob = await (await call('GET', `${base(run)}/images/${encodeURIComponent(name)}`)).blob()
  const data = await toBase64(blob)
  return `data:${blob.type};base64,${data}`
}

const two = (n: number) => String(n).padStart(2, '0')

/** `orch-<run>-screenshots-<YYYYMMDD-HHMM>.zip`, in the person's local time. */
export function zipName(run: string, now: Date = new Date()): string {
  return `orch-${run}-screenshots-${now.getFullYear()}${two(now.getMonth() + 1)}${two(now.getDate())}-${two(now.getHours())}${two(now.getMinutes())}.zip`
}

/** All pictures of the run as one zip (the request needs the bearer header, so the page saves the answer itself). */
export async function downloadScreenshots(run: string): Promise<void> {
  const blob = await (await call('GET', `${base(run)}/screenshots.zip`)).blob()
  const url = URL.createObjectURL(blob)
  try {
    const a = document.createElement('a')
    a.href = url
    a.download = zipName(run)
    a.rel = 'noopener'
    document.body.appendChild(a)
    a.click()
    a.remove()
  } finally {
    setTimeout(() => URL.revokeObjectURL(url), 10_000)
  }
}
