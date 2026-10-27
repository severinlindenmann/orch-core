// Tells the person when something new needs them (a question, an approval or a check): a toast, and the count in the
// tab title. What was already waiting when the page opened is not announced again.
import { useQuery } from '@tanstack/react-query'
import { useNavigate } from '@tanstack/react-router'
import { useEffect, useRef } from 'react'
import { toast } from 'sonner'
import { queries } from '@/api/queries'
import type { NeedsYouItem } from '@/api/types'
import { useAttention } from '../attention'
import { useWorkspace } from '../workspace'

const KINDS: string[] = ['question', 'approval', 'verdict']

type Item = Pick<NeedsYouItem, 'kind' | 'ticket' | 'ref' | 'hash' | 'title'>

/** One id per thing waiting: a new version of the same step is a new id. */
export function itemId(item: Item): string {
  return `${item.kind}:${item.ticket}:${item.ref ?? ''}:${item.hash ?? ''}`
}

/** The plain words of the toast. */
export function toastText(item: Item): string {
  if (item.kind === 'question') return `A question about ${item.title}`
  if (item.kind === 'approval') return `Ready for your approval: ${item.title}`
  return `Ready for your check: ${item.title}`
}

/** The tab title: the count in front when something waits, never twice. */
export function titleFor(base: string, total: number): string {
  const plain = base.replace(/^\(\d+\)\s*/, '')
  return total > 0 ? `(${total}) ${plain}` : plain
}

export function NeedsYouNotifier() {
  const { workspace } = useWorkspace()
  const ws = workspace?.id
  const navigate = useNavigate()
  const today = useQuery({ ...queries.today(ws ?? ''), enabled: !!ws })
  const attention = useAttention(ws)
  const total = attention.needsYou.total
  const seen = useRef(new Set<string>())
  const seededFor = useRef<string | undefined | null>(null)
  const base = useRef(titleFor(document.title, 0))

  useEffect(() => {
    const data = today.data
    if (!data) return
    const items = data.needs_you.filter((i) => KINDS.includes(i.kind))
    if (seededFor.current !== ws) {
      // First read for this workspace: only remember what is already waiting.
      seededFor.current = ws
      seen.current = new Set(items.map(itemId))
      return
    }
    for (const item of items) {
      const id = itemId(item)
      if (seen.current.has(id)) continue
      seen.current.add(id)
      toast('Needs you', {
        description: toastText(item),
        duration: 10000,
        action: {
          label: 'Open',
          onClick: () =>
            void navigate({
              to: '/ticket/$key',
              params: { key: item.ticket },
              ...(item.kind === 'question' ? { search: { tab: 'questions' } as never } : {}),
            }),
        },
      })
    }
  }, [today.data, ws, navigate])

  useEffect(() => {
    const plain = base.current
    document.title = titleFor(plain, total)
    return () => {
      document.title = plain
    }
  }, [total])

  return null
}
