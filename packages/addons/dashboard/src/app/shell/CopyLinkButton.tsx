import { Link2 } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { useCopyLink } from '../copyLink'

/** A small "Copy link" button: copies the permanent link of the page as it is shown (tab, filters, workspace). */
export function CopyLinkButton({ label = 'Copy link to this page', what = 'Link', className }: { label?: string; what?: string; className?: string }) {
  const copy = useCopyLink()
  return (
    <Button type="button" variant="ghost" size="icon-xs" onClick={() => void copy(what)} aria-label={label} title={label} className={className}>
      <Link2 />
    </Button>
  )
}
