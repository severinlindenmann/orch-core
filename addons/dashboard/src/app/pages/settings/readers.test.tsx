// Core H12d-2 in the dashboard: every click-to-sign refusal reason as a plain sentence, the devices it names shown by
// label (never by raw id), reader levels on Settings → Devices, untrusted labels as text, and the signer's readers_old.
import { cleanup, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { clickProblem } from '@/api/click'
import {
  DECL_INVALID_SENTENCE,
  READERS_REASONS,
  READERS_SENTENCE,
  SIGNER_PROBLEM_SENTENCE,
  UNAVAILABLE_REASONS,
  UNAVAILABLE_SENTENCE,
  refusalOf,
  refusalSentence,
  signerProblem,
} from '@/api/refusals'
import { ApiError } from '@/api/types'
import { DeviceName, nameDevices } from '@/components/sign/RefusalDevices'
import { resetLayoutPreview } from '@/dev/layoutPreview'
import { LAPTOP, MAC, OLD_PHONE, PHONE, readersRoster } from '@/dev/readersFixture'
import { renderApp } from '@/test/renderApp'
import { NEEDS_UPDATE, readerRows, requiredLine } from './host/Readers'

const T = { timeout: 4000 }
afterEach(() => {
  cleanup()
  resetLayoutPreview()
})

const err = (code: string, detail?: Record<string, unknown>, extra: Record<string, unknown> = {}) =>
  new ApiError(409, { code, message: `${code} from the host`, retryable: false, ...(detail ? { detail } : {}), ...extra } as never)

describe('every reason is a plain sentence', () => {
  it.each(UNAVAILABLE_REASONS)('sign.convenience_unavailable · %s', (reason) => {
    const p = clickProblem(err('sign.convenience_unavailable', { reason, devices: [LAPTOP] }))
    expect(p.body).toContain(UNAVAILABLE_SENTENCE[reason])
    expect(p.body).not.toContain(reason)
    expect(p.devices).toEqual([LAPTOP])
    expect(p.next).toBe('terminal')
  })

  it.each(READERS_REASONS)('sign.convenience_readers · %s', (reason) => {
    const p = clickProblem(err('sign.convenience_readers', { reason, devices: [OLD_PHONE] }))
    expect(p.body).toContain(READERS_SENTENCE[reason])
    expect(p.body).not.toContain('from the host') // a known reason is said in core's words, not the host's message
    expect(p.devices).toEqual([OLD_PHONE])
  })

  it('the sentences say what the spec says', () => {
    expect(UNAVAILABLE_SENTENCE.relay_paired).toBe('Click-to-sign is off because this workspace is paired with a phone through the relay.')
    expect(READERS_SENTENCE.reader_too_old).toBe('A device’s orch app is too old to show what click-to-sign changes: update it, or sign in the terminal.')
    for (const s of [...Object.values(UNAVAILABLE_SENTENCE), ...Object.values(READERS_SENTENCE), DECL_INVALID_SENTENCE]) expect(s).toMatch(/^[A-Z].*\.$/)
  })

  it('the click signer’s top-level reason counts too (its channel carries no detail)', () => {
    const p = clickProblem(err('sign.convenience_unavailable', undefined, { reason: 'phone_device' }))
    expect(p.body).toContain(UNAVAILABLE_SENTENCE.phone_device)
    expect(p.devices).toEqual([])
  })

  it('an unknown reason is never echoed: the code’s general sentence', () => {
    const p = clickProblem(err('sign.convenience_unavailable', { reason: 'relay_reader_old<b>' }))
    expect(p.body).not.toContain('relay_reader_old')
    expect(p.body).toContain('does not allow click-to-sign')
    expect(refusalOf(err('sign.convenience_readers', { reason: 'disabled' }))!.reason).toBeNull() // a reason of the other code
  })

  it('devices must be device ids, or none is taken', () => {
    for (const devices of [['Mara’s phone'], [LAPTOP, 'd_' + 'A'.repeat(32)], LAPTOP, [{ id: LAPTOP }], Array.from({ length: 65 }, () => LAPTOP)])
      expect(refusalOf(err('sign.convenience_unavailable', { reason: 'other_member', devices }))!.devices).toEqual([])
    expect(refusalOf(err('sign.convenience_unavailable', { devices: [LAPTOP, LAPTOP, PHONE] }))!.devices).toEqual([LAPTOP, PHONE])
  })

  it('reader.decl_invalid has its sentence; other codes are not refusals', () => {
    expect(refusalSentence(refusalOf(err('reader.decl_invalid'))!)).toBe(DECL_INVALID_SENTENCE)
    expect(clickProblem(err('reader.decl_invalid')).body).toBe(DECL_INVALID_SENTENCE)
    expect(refusalOf(err('human.click_refused', { reason: 'disabled' }))).toBeNull()
    expect(refusalOf(new Error('x'))).toBeNull()
  })

  it('readers_old has its own sentence and fix; an unnamed problem stays text', () => {
    expect(signerProblem('readers_old')).toBe(SIGNER_PROBLEM_SENTENCE.readers_old)
    expect(signerProblem('readers_old')).toMatch(/Settings → Devices/)
    expect(signerProblem('unfinished change to passphrase')).toBe('The signing helper says: unfinished change to passphrase.')
    expect(signerProblem(undefined)).toBeNull()
  })
})

describe('devices are named by label, never by id', () => {
  it('label and member from the roster; a device without a label by its member; an unknown one says so', () => {
    const named = nameDevices([PHONE, LAPTOP, 'd_' + '9'.repeat(32)], readersRoster())
    expect(named.map((d) => [d.label?.slice(0, 9) ?? null, d.member])).toEqual([['Severin’s', 'Severin'], [null, 'Mara'], [null, null]])
    render(
      <p>
        {named.map((d) => (
          <DeviceName key={d.id} device={d} />
        ))}
      </p>,
    )
    const text = document.body.textContent!
    expect(text).toContain('a device of Mara with no name')
    expect(text).toContain('a device this page does not know')
    for (const id of [PHONE, LAPTOP]) expect(text).not.toContain(id)
  })

  it('a label is text: markup is not drawn, an LF stays a line break inside isolated text', () => {
    render(<DeviceName device={{ id: PHONE, label: '<img src=x onerror=alert(1)>\nsecond line', member: 'Mara' }} />)
    const bdi = screen.getByTestId('refusal-device').querySelector('bdi')!
    expect(bdi.textContent).toBe('<img src=x onerror=alert(1)>\nsecond line')
    expect(document.querySelector('img')).toBeNull()
    expect(screen.getByTestId('refusal-device')).toHaveTextContent('(named by the device)')
  })
})

describe('reader levels', () => {
  it('rows below the required level come first and are marked', () => {
    const roster = readersRoster()
    const readers = roster.flatMap((m) => m.devices.map((d) => ({ device: d.id, person: m.person, reader: d.reader ?? 0, level: d.reader ?? 0 })))
    const rows = readerRows(readers, 1, roster)
    expect(rows.filter((r) => r.below).map((r) => r.reader.device)).toContain(LAPTOP)
    expect(rows.findIndex((r) => !r.below)).toBe(rows.filter((r) => r.below).length)
    expect(readerRows(readers, 0, roster).every((r) => !r.below)).toBe(true)
    expect(readerRows(readers, null, roster).every((r) => !r.below)).toBe(true)
    expect(requiredLine(1)).toMatch(/level 1/)
    expect(requiredLine(0)).toMatch(/No reader level is required/)
  })

  it('Settings → Devices shows each device’s level and required_reader, marks the ones below, labels as text', async () => {
    renderApp('/__layout/readers-devices')
    const rows = await screen.findAllByTestId('reader-row', {}, T)
    expect(rows.length).toBe(10)
    expect(screen.getByTestId('readers-required')).toHaveAttribute('data-required', '1')
    const below = rows.filter((r) => r.dataset.below === 'true')
    expect(below.length).toBe(4)
    for (const r of below) expect(r).toHaveTextContent(NEEDS_UPDATE)
    for (const r of rows.filter((x) => x.dataset.below === 'false')) expect(r).not.toHaveTextContent(NEEDS_UPDATE)
    expect(screen.getByTestId('readers-below')).toHaveTextContent('4 devices are below the required level')
    const lf = rows.find((r) => r.textContent!.includes('Work phone'))!
    expect(within(lf).getByTestId('reader-level')).toHaveTextContent('0')
    expect(lf.querySelector('bdi')!.textContent).toBe('Work phone\nsecond line')
    expect(screen.getByTestId('reader-levels').textContent).not.toContain(MAC)
    expect(screen.getByTestId('pair-unavailable')).toBeInTheDocument() // pairing stays behind its own gate
  })

  it('a host without reader/1 says so', async () => {
    const { setFeatures } = await import('@/api/features')
    setFeatures({ features: ['identity'] })
    try {
      renderApp('/__layout/readers-devices')
      expect(await screen.findByTestId('readers-unavailable', {}, T)).toBeInTheDocument()
      expect(screen.queryByTestId('reader-row')).toBeNull()
    } finally {
      setFeatures(null)
    }
  })
})

describe('the refusal gallery', () => {
  it('draws every reason, names devices by label, shows no raw device id and no unknown reason', async () => {
    renderApp('/__layout/convenience-refusals')
    const gallery = await screen.findByTestId('refusal-gallery', {}, T)
    const problems = within(gallery).getAllByTestId('click-problem')
    expect(problems.length).toBe(UNAVAILABLE_REASONS.length + READERS_REASONS.length + 2)
    for (const s of [...Object.values(UNAVAILABLE_SENTENCE), ...Object.values(READERS_SENTENCE), DECL_INVALID_SENTENCE]) expect(gallery.textContent).toContain(s)
    await within(gallery).findAllByText(/Work phone/, {}, T)
    const bodies = problems.map((p) => p.textContent).join('\n')
    for (const id of [OLD_PHONE, LAPTOP, PHONE]) expect(bodies).not.toContain(id)
    expect(bodies).not.toContain('relay_reader_old')
    expect(within(gallery).getByTestId('signer-readers-old')).toHaveTextContent(SIGNER_PROBLEM_SENTENCE.readers_old)
  })
})
