// The guide on a real host: fixed pages from the web bundle (api/guideContent.ts), no host call. The mock serves the same
// pages through its guide addon. `?page=` picks the page; anything unknown shows the first.
import { Link, useSearch } from '@tanstack/react-router'
import { GUIDE_PAGES } from '@/api/guideContent'
import { SafeMarkdown } from '@/addon-ui/SafeMarkdown'
import { cn } from '@/lib/utils'
import { usePageHeader } from '@/app/shell/ShellUi'

export function GuidePage() {
  const { page } = useSearch({ strict: false }) as { page?: string }
  const cur = GUIDE_PAGES.find((p) => p.slug === page) ?? GUIDE_PAGES[0]
  usePageHeader('Guide')
  return (
    <div className="grid w-full gap-6 md:grid-cols-[14rem_minmax(0,1fr)]">
      <nav aria-label="Guide pages">
        <ul className="space-y-1">
          {GUIDE_PAGES.map((p) => (
            <li key={p.slug}>
              <Link
                to="/guide"
                search={{ page: p.slug }}
                aria-current={p.slug === cur.slug ? 'page' : undefined}
                className={cn('block rounded-md px-3 py-1.5 text-[13px] hover:bg-surface-2', p.slug === cur.slug ? 'bg-surface-2 font-medium text-text' : 'text-text-muted')}
              >
                {p.title}
              </Link>
            </li>
          ))}
        </ul>
      </nav>
      <article className="min-w-0 max-w-2xl">
        <SafeMarkdown text={cur.markdown} />
      </article>
    </div>
  )
}
