import type { ReactNode } from 'react'

/** Core's list of the args a confirm sends (one line per arg, from `argLines`), above the addon's region. */
export function SentArgs({ lines }: { lines: ReactNode[] }) {
  if (!lines.length) return null
  return (
    <dl className="grid grid-cols-[88px_1fr] gap-x-3 rounded-md border border-border bg-bg p-3 text-[13px]">
      <dt className="text-text-muted">Sends</dt>
      <dd>
        <ul className="list-disc space-y-0.5 pl-4">
          {lines.map((l, i) => (
            <li key={i}>{l}</li>
          ))}
        </ul>
      </dd>
    </dl>
  )
}
