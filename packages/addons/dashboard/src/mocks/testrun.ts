// Static demo checklist: browser-local notes and images, never a host request.
import type { StepNotes } from '@/api/testrun'
import { makeZip } from '@/app/feedback/model'

type Run = { notes: Record<string, StepNotes>; images: Record<string, { type: string; data: string }> }
const key = 'orch.demo.checklist.'
export async function demoTestrun(method: string, path: string, body?: unknown): Promise<Response> {
  const [, , , run, section, id, sub] = path.split('/').map(decodeURIComponent)
  const state: Run = JSON.parse(localStorage.getItem(key + run) ?? '{"notes":{},"images":{}}')
  const json = (value: unknown) => Response.json(value)
  const save = () => localStorage.setItem(key + run, JSON.stringify(state))
  const bytes = (data: string) => Uint8Array.from(atob(data), (c) => c.charCodeAt(0))
  if (section === 'previous') return json({ runs: [] })
  if (section === 'screenshots.zip') {
    return new Response(makeZip(Object.entries(state.images).map(([name, image]) => ({ name, data: bytes(image.data) }))))
  }
  if (section === 'images') {
    if (method === 'DELETE') {
      delete state.images[id]
      for (const note of Object.values(state.notes)) note.images = note.images.filter((name) => name !== id)
      save()
      return json({ ok: true })
    }
    const image = state.images[id]
    return image ? new Response(bytes(image.data), { headers: { 'content-type': image.type } }) : new Response(null, { status: 404 })
  }
  if (section === 'steps') {
    const note = state.notes[id] ?? { status: 'todo', note: '', images: [] }
    if (sub === 'images') {
      const image = body as { type: string; data: string }
      const name = `${crypto.randomUUID()}.${image.type.split('/')[1]}`
      state.images[name] = image
      note.images.push(name)
      state.notes[id] = note
      save()
      return json({ name, path: 'Browser storage (demo)' })
    }
    state.notes[id] = { ...note, ...body as Partial<StepNotes>, updated: new Date().toISOString() }
    save()
    return json({ ok: true })
  }
  return json({ path: 'Browser storage (demo)', images_path: 'Browser storage (demo)', notes: state.notes })
}
