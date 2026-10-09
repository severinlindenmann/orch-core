/** The visible reason beside a control that a non-owner sees disabled. */
export function OwnerNote({ children = 'Only owners can change this.', id }: { children?: string; id?: string }) {
  return (
    <span id={id} className="text-[12px] text-text-faint">
      {children}
    </span>
  )
}
