import { Suspense } from 'react'
import { lazyWithPreload } from '@/lib/lazyPreload'
import { Skeleton } from '@/components/ui/skeleton'

// react-markdown and its plugins load on first use, so they stay out of the main bundle.
export const SafeMarkdownChunk = lazyWithPreload(() => import('./SafeMarkdownView').then((m) => ({ default: m.SafeMarkdownView })))

/** Markdown from an addon (sanitized; see SafeMarkdownView). */
export function SafeMarkdown({ text, toc = false }: { text: string; toc?: boolean }) {
  return (
    <Suspense fallback={<Skeleton className="h-12 w-full" />}>
      <SafeMarkdownChunk text={text} toc={toc} />
    </Suspense>
  )
}
