import { useCallback } from 'react'
import { useRouter } from '@tanstack/react-router'
import { toast } from 'sonner'
import { shareableLink, toPublicPath } from './urls'

/** Copies text; falls back to a hidden text area where the Clipboard API is missing or refused. */
export async function copyText(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text)
      return true
    }
  } catch {
    /* refused (no focus, permissions): try the old way */
  }
  try {
    const area = Object.assign(document.createElement('textarea'), { value: text })
    area.setAttribute('readonly', '')
    area.style.cssText = 'position:fixed;opacity:0;pointer-events:none'
    document.body.append(area)
    area.select()
    const ok = document.execCommand?.('copy') ?? false
    area.remove()
    return ok
  } catch {
    return false
  }
}

/**
 * The permanent link of the current page: its path with the workspace (a ticket's key names its own), and the
 * view state in the search params. Never a dialog, a token or a title.
 */
export function useCurrentLink() {
  const router = useRouter()
  return useCallback(() => {
    const l = router.state.location
    const path = toPublicPath(l.pathname, router.options.context.urls.prefix)
    return shareableLink(`${path}${l.searchStr}${l.hash ? `#${l.hash}` : ''}`)
  }, [router])
}

/** "Copy link": copies the current page's link and says so (or shows the link when the copy failed). */
export function useCopyLink() {
  const current = useCurrentLink()
  return useCallback(
    async (what = 'Link') => {
      const link = current()
      if (await copyText(link)) toast.success(`${what} copied`, { description: link })
      else toast.error('Could not copy the link', { description: link, duration: Infinity })
    },
    [current],
  )
}
