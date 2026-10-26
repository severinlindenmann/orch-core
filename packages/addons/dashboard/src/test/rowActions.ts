import { screen, within } from '@testing-library/react'
import type userEvent from '@testing-library/user-event'

/** Opens the row's "More actions for …" menu and returns the menu item with this name (row actions beyond the first live there). */
export async function moreAction(user: ReturnType<typeof userEvent.setup>, row: HTMLElement, name: string | RegExp) {
  await user.click(within(row).getByRole('button', { name: /^More actions for / }))
  return screen.findByRole('menuitem', { name })
}
