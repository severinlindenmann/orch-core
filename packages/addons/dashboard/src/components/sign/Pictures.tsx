import { useMemo } from 'react'
import { checkPictures } from '@/api/pasteImage'
import type { SignContent } from '@/api/signing'
import { Inline, Raw } from './visible'

type Images = SignContent['images']

const short = (d: string) => `${d.slice(7, 15)}…${d.slice(-6)}`

/** True when every picture in the review is the file this tab holds (same sha256 and size). Sign is off otherwise. */
export function usePicturesOk(images: Images): boolean {
  return useMemo(() => checkPictures(images).every((p) => p.ok), [images])
}

/**
 * R3 part B (WYSIWYS): each picture is shown from the bytes THIS browser holds, never from what the host sends, and only
 * when their sha256 equals the digest in the signed event. A host that swapped the file shows a mismatch and Sign stays off.
 */
export function Pictures({ images }: { images: Images }) {
  const checks = useMemo(() => checkPictures(images), [images])
  if (!checks.length) return null
  return (
    <section data-testid="sign-pictures">
      <h3 className="mb-2 text-[13px] font-medium text-text">{checks.length === 1 ? 'Screenshot' : 'Screenshots'}</h3>
      <ul className="space-y-3">
        {checks.map((p) => (
          <li key={p.name} className="space-y-1">
            {p.ok && p.url ? (
              <img src={p.url} alt={p.alt} className="max-h-56 max-w-full rounded-md border border-border object-contain" />
            ) : (
              <div className="rounded-md border border-danger px-3 py-6 text-center text-[12px] text-danger">The picture is not shown because it does not match.</div>
            )}
            <p className="text-[12px] text-text-muted">
              <Inline>{p.alt}</Inline>
              {' · '}
              <span className="font-mono">sha256 <Raw>{short(p.sha256)}</Raw></span>{' '}
              {p.ok ? (
                <span data-testid="picture-ok" className="text-success">✓ matches what you sign</span>
              ) : (
                <span data-testid="picture-bad" role="alert" className="text-danger">✗ does not match what would be signed. Do not sign this.</span>
              )}
            </p>
          </li>
        ))}
      </ul>
    </section>
  )
}
