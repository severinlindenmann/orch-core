export type SSEMessage = { event: string; data: string; id?: string }

/** Incremental UTF-8 SSE parser, including CRLF split across network chunks. */
export async function readSSE(body: ReadableStream<Uint8Array>, receive: (message: SSEMessage) => void, alive: () => void = () => {}) {
  const reader = body.getReader()
  const decoder = new TextDecoder()
  let buffer = '', event = '', data: string[] = [], id: string | undefined
  const line = (text: string) => {
    if (!text) {
      if (data.length) receive({ event: event || 'message', data: data.join('\n'), id })
      event = ''; data = []; id = undefined
      return
    }
    if (text.startsWith(':')) return
    const colon = text.indexOf(':')
    const field = colon < 0 ? text : text.slice(0, colon)
    const value = colon < 0 ? '' : text.slice(colon + 1).replace(/^ /, '')
    if (field === 'event') event = value
    if (field === 'data') data.push(value)
    if (field === 'id' && !value.includes('\0')) id = value
  }
  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) return
      alive()
      buffer += decoder.decode(value, { stream: true })
      if (buffer.length + data.join('').length > 1_048_576) throw new Error('Stream frame too large')
      let end: number
      while ((end = buffer.indexOf('\n')) >= 0) {
        line(buffer.slice(0, end).replace(/\r$/, '')); buffer = buffer.slice(end + 1)
      }
    }
  } finally { await reader.cancel().catch(() => {}); reader.releaseLock() }
}
