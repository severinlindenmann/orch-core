import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

/** Presses the first "i" (InfoDrawer) inside `scope` and returns the opened content. */
export async function openInfo(scope: HTMLElement = document.body): Promise<HTMLElement> {
  await userEvent.click(within(scope).getAllByRole('button', { name: 'More information' })[0])
  return screen.findByTestId('info-drawer-content')
}
