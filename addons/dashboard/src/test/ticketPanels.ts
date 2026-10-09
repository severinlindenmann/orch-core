import { screen, within } from '@testing-library/react'
import type { UserEvent } from '@testing-library/user-event'

const T = { timeout: 5000 }

/** The ticket page's rail (at ≥ 1280 px; tests set `window.innerWidth` before rendering). */
export const findRail = () => screen.findByRole('complementary', { name: 'Ticket details' }, T)

/**
 * Opens one collapsed addon panel in the ticket rail by its title and returns the panel. Panels are collapsed by
 * default (at most two open), so a test opens the one it reads.
 */
export async function openTicketPanel(user: UserEvent, title: string | RegExp): Promise<HTMLElement> {
  const rail = await findRail()
  const name = typeof title === 'string' ? new RegExp(`${title.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}$`) : title
  const header = await within(rail).findByRole('button', { name, expanded: false }, T).catch(() => within(rail).getByRole('button', { name, expanded: true }))
  if (header.getAttribute('aria-expanded') === 'false') await user.click(header)
  return header.closest('section') as HTMLElement
}
