import { lazy, Suspense } from 'react'
import { Skeleton } from '@/components/ui/skeleton'

// react-markdown and its plugins load on first use, so they stay out of the main bundle.
const View = lazy(() => import('./SafeMarkdownView').then((m) => ({ default: m.SafeMarkdownView })))

/** Markdown from an addon (sanitized; see SafeMarkdownView). */
export function SafeMarkdown({ text }: { text: string }) {
  return (
    <Suspense fallback={<Skeleton className="h-12 w-full" />}>
      <View text={text} />
    </Suspense>
  )
}
