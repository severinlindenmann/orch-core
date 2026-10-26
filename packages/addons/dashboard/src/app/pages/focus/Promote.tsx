import { useQuery } from '@tanstack/react-query'
import { focusApi, type ChatItem } from '@/api/focus'
import { useWorkspace } from '@/app/workspace'
import type { Draft } from '../new-ticket'
import { CreateFocusTicket } from './StartDialog'

const PAGES = 5
const clip = (text: string, n: number) => (text.length > n ? `${text.slice(0, n - 1)}…` : text)

/** A ticket draft from a scratch conversation: title = the first thing the person asked, requirements = that ask in
 * full, context = what the agent said last. All of it is editable before the ticket is signed. */
export function draftFromChat(items: ChatItem[]): Partial<Draft> {
  const text = (i: ChatItem) => (i.text ?? '').trim()
  const first = items.find((i) => i.role === 'user' && i.kind === 'text' && text(i))
  const said = items.filter((i) => i.role === 'assistant' && i.kind === 'text' && text(i)).slice(-3).map(text)
  const ask = first ? text(first) : ''
  const sections: Draft['sections'] = {}
  if (ask) sections.requirements = clip(ask, 4000)
  if (said.length) sections.context = clip(said.join('\n\n'), 4000)
  return { title: clip(ask.split('\n')[0] ?? '', 120), sections }
}

async function readAll(ws: string, run: string): Promise<ChatItem[]> {
  const items: ChatItem[] = []
  let after: string | null = null
  for (let page = 0; page < PAGES; page++) {
    const got = await focusApi.transcript(ws, run, { after, limit: 200 })
    items.push(...got.items)
    if (got.eof || !got.next) break
    after = got.next
  }
  return items
}

/** "Create ticket from this": the existing create form, prefilled from the scratch session. When the ticket exists
 * the session is linked to it (`run.link`) and Focus opens the ticket's session. */
export function Promote({ run, onClose }: { run: string; onClose: () => void }) {
  const { workspace } = useWorkspace()
  const ws = workspace?.id ?? ''
  const chat = useQuery({ queryKey: ['focus', 'promote', ws, run], queryFn: () => readAll(ws, run), enabled: !!ws, retry: false, staleTime: 0, gcTime: 0 })
  if (chat.isPending) return <p role="status" className="p-4 text-sm">Reading the conversation...</p>
  // A transcript that cannot be read still lets the person write the ticket by hand.
  return <CreateFocusTicket onClose={onClose} initial={chat.data ? draftFromChat(chat.data) : undefined} onTicket={(key) => focusApi.link(ws, run, key).then(() => undefined)} />
}
