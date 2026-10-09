import { ApiError } from '@/api/types'

/** Awaits a request that must be refused (4xx) and returns the refusal; fails the test if it succeeded. */
export async function refused(p: Promise<unknown>): Promise<{ status: number; code: string; message: string; hint?: string }> {
  try {
    await p
  } catch (e) {
    if (e instanceof ApiError) return { status: e.status, code: e.code, message: e.message, hint: e.hint }
    throw e
  }
  throw new Error('expected the request to be refused, but it succeeded')
}
