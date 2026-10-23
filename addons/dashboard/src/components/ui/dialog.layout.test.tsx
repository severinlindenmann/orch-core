import { render, screen } from '@testing-library/react'
import { expect, it } from 'vitest'
import { Dialog, DialogContent, DialogDescription, DialogTitle } from './dialog'

// Run 8 (7.4): a wide preview inside a dialog (a long line in a text file) stretched the dialog's single grid column and
// gave it a sideways scrollbar with its buttons cut off. jsdom cannot lay out, so this pins the class that prevents it:
// the one column may shrink to the dialog's width (verified in Chrome: scrollWidth == clientWidth with a 1700 px line).
it('the dialog body is one column that may shrink below its content', () => {
  render(
    <Dialog open>
      <DialogContent>
        <DialogTitle>t</DialogTitle>
        <DialogDescription>d</DialogDescription>
      </DialogContent>
    </Dialog>,
  )
  expect(screen.getByRole('dialog').className).toContain('grid-cols-[minmax(0,1fr)]')
})
