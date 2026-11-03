import { fireEvent, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { HttpResponse, http } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, expect, it } from 'vitest'
import type { ProjectResponse, Turnaround, ViewDraft } from '../../api/types'
import { projectFixture, queryWrapper } from '../../test/studioTestUtils'
import { useProject } from '../../api/queries'
import { TurnaroundPanel } from './TurnaroundPanel'

const TURN = `turnaround-${'a'.repeat(32)}`
const DRAFT = `draft-${'b'.repeat(32)}`
const sourceReference = { revision_id: 'rev-s', key: 'sources/s.png', sha256: 'a'.repeat(64), width: 512, height: 512 }
const project = (views = [{ view_id: 'S', heading: 'S' as const, name: 'Front', reference: sourceReference }], layout: 'isometric' | 'side_view' = 'isometric'): ProjectResponse =>
  ({ ...projectFixture, manifest: { ...projectFixture.manifest, sprite_layout: layout, views } })
const standard = { worker_id: 'desk', available: true, reason: null, profile: { profile: 'h3-24gb-standard' }, features: { last_frame: true } }
const cards: Record<string, string[]> = { N: ['Back to you.', "Axe in the hand on the picture's right."], NE: ['Facing back-right.', 'Axe in the near hand.'], E: ['Right profile.', 'Axe toward you.'], SE: ['Facing front-right.', 'Axe in the near hand.'], SW: ['Facing front-left.', 'Axe in the far hand.'], W: ['Left profile.', 'Axe in the far hand.'], NW: ['Facing back-left.', 'Axe in the far hand.'] }
const reason = (h: string) => h === 'S' ? 'the source, where the loop starts and ends' : h === 'E' || h === 'W' ? 'the body is narrowest here, a profile' : h === 'N' ? 'the body is widest here, between the two profiles' : 'evenly spaced between its neighbours'
const settled: Turnaround = {
  turnaround_id: TURN, source_view_id: 'S', source_heading: 'S', source_superseded: false, source_backdrop: 'transparent',
  equipment: [], prompt: 'p', job_id: 'j', status: 'completed', settled: true, frame_count: 64, cards_version: 'placement_cards.v1',
  placement_cards: cards, synthesized_parts: { N: ['the back of the head, hair and outfit, and equipment on the back'] }, pick_set: null,
  suggested_picks: Object.fromEntries(['S', 'SE', 'E', 'NE', 'N', 'NW', 'W', 'SW'].map((h, i) => [h, { frame: i * 8, reason: reason(h) }])),
}
const frames = { frames: Array.from({ length: 32 }, (_, i) => ({ index: i, image: `data:image/png;base64,frame${i}` })) }
const draft: ViewDraft = {
  draft_id: DRAFT, turnaround_id: TURN, heading: 'N', pick_revision: 1, frame_index: 32,
  image: { key: 'k', sha256: 'h', width: 1024, height: 1024 }, image_path: '/studio/projects/project-1/files?key=k',
  placement_card: cards.N, synthesized_parts: ['the back of the head, hair and outfit, and equipment on the back'],
  check: null, checked: false, discarded: false, bound_view_ids: [], bindable: false, suggested_frame: 32, picks_version: 'turnaround-picks-v2',
}

const server = setupServer()
beforeAll(() => server.listen()); afterEach(() => server.resetHandlers()); afterAll(() => server.close())
function serve({ workers = [standard], takes = [] as Turnaround[], detail = settled, drafts = [] as ViewDraft[] } = {}) {
  server.use(
    http.get('/studio/generation-config', () => HttpResponse.json({ workers })),
    http.get('/studio/projects/project-1/turnarounds', () => HttpResponse.json({ turnarounds: takes })),
    http.get(`/studio/projects/project-1/turnarounds/${TURN}`, () => HttpResponse.json(detail)),
    http.get(`/studio/projects/project-1/turnarounds/${TURN}/frames`, () => HttpResponse.json(frames)),
    http.get('/studio/projects/project-1/view-drafts', () => HttpResponse.json({ drafts })),
  )
}

it.each([
  ['no front or three-quarter reference', project([{ view_id: 'E', heading: 'E' as never, name: 'Right', reference: sourceReference }]), [standard], 'Needs a front or three-quarter (S, SE or SW) reference'],
  ['a side-view project', project([], 'side_view'), [standard], 'Turnarounds currently need a front or three-quarter source'],
  ['no eligible machine', project(), [{ ...standard, features: { last_frame: false } }], 'No machine supports turnarounds yet'],
  ['only a LightX machine', project(), [{ ...standard, profile: { profile: 'h3-16gb-lightx8' } }], 'No machine supports turnarounds yet'],
])('explains why it cannot start with %s', async (_, value, workers, reason) => {
  serve({ workers })
  render(<TurnaroundPanel project={value} onClose={() => {}} />, { wrapper: queryWrapper() })
  expect(await screen.findByText((text) => text.includes(reason))).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Generate' })).not.toBeInTheDocument()
})

it('starts a turnaround from the front reference with one-sided equipment', async () => {
  serve()
  let body: Record<string, unknown> = {}
  server.use(http.post('/studio/projects/project-1/turnarounds', async ({ request }) => { body = await request.json() as Record<string, unknown>; return HttpResponse.json({ turnaround_id: TURN, operation: { job_id: 'j' } }) }))
  const user = userEvent.setup()
  render(<TurnaroundPanel project={project()} onClose={() => {}} />, { wrapper: queryWrapper() })
  await user.click(await screen.findByRole('button', { name: 'Add equipment' }))
  await user.type(screen.getByLabelText('Item 1 name'), 'axe')
  expect(screen.getByText("axe: the character's right hand")).toBeInTheDocument()
  // Hand items need a side: "neither" is offered only for back and other.
  expect(within(screen.getByLabelText('Item 1 picture side')).queryByText('neither (centred)')).not.toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'Generate' }))
  await expect.poll(() => body.source_revision_id).toBe('rev-s')
  expect(body).toEqual(expect.objectContaining({ source_view_id: 'S', equipment: [{ item: 'axe', attachment: 'hand', source_picture_side: 'left' }] }))
})

it('prefills suggested frames, shows each placement card and asks before saving an out-of-region pick', async () => {
  serve({ takes: [settled] })
  const saves: Record<string, unknown>[] = []
  server.use(http.put(`/studio/projects/project-1/turnarounds/${TURN}/picks`, async ({ request }) => {
    const body = await request.json() as { picks: Record<string, { confirm_out_of_region?: boolean }> }
    saves.push(body)
    return body.picks.N.confirm_out_of_region ? HttpResponse.json({ pick_revision: 1, picks: body.picks, findings: [], saved_by: 'u', saved_at: 'now' })
      : HttpResponse.json({ detail: { message: 'N: frame 60 is outside its region', problems: [{ heading: 'N', kind: 'out_of_region', distance: 9, message: 'N: frame 60 is 9 frames outside its region; confirm it if that is the facing you mean' }] } }, { status: 422 })
  }))
  const user = userEvent.setup()
  render(<TurnaroundPanel project={project()} onClose={() => {}} />, { wrapper: queryWrapper() })
  const north = await screen.findByRole('figure', { name: 'Back (N)' })
  expect(within(north).getByLabelText('N frame')).toHaveValue('32')
  expect(within(north).getByText("Back to you. Axe in the hand on the picture's right.")).toBeInTheDocument()
  expect(within(north).getByText('Suggested frame 32: the body is widest here, between the two profiles.')).toBeInTheDocument()
  // The source is an anchor, never a slot.
  expect(screen.queryByRole('figure', { name: 'Front (S)' })).not.toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'Save picks' }))
  const confirm = await within(north).findByLabelText(/9 frames outside its region/)
  await user.click(confirm)
  await user.click(screen.getByRole('button', { name: 'Save picks' }))
  await expect.poll(() => saves.length).toBe(2)
  expect((saves[1].picks as Record<string, unknown>).N).toEqual(expect.objectContaining({ confirm_out_of_region: true }))
})

it('checks a draft against its card, then binds it through a reviewed setup change', async () => {
  const picked = { ...settled, pick_set: { pick_revision: 1, picks: {}, findings: [], saved_by: 'u', saved_at: 'now' } }
  let checked: Record<string, unknown> = {}; let applied = false; let previewed: { desired_setup?: { views: { heading: string; reference: unknown }[] } } = {}
  let current: ViewDraft = draft
  serve({ takes: [picked], detail: picked })
  server.use(
    http.get('/studio/projects/project-1/view-drafts', () => HttpResponse.json({ drafts: [current] })),
    http.post(`/studio/projects/project-1/view-drafts/${DRAFT}/check`, async ({ request }) => { checked = await request.json() as Record<string, unknown>; current = { ...draft, checked: true, bindable: true }; return HttpResponse.json(current) }),
    http.get('/studio/projects/project-1/setup', () => HttpResponse.json({ revision: 4, setup: { schema_version: 1, sprite_layout: 'isometric', identity_reference: null, views: [{ view_id: 'S', heading: 'S', name: 'Front', reference: { revision_id: 'rev-s' }, motions: [] }] } })),
    http.post('/studio/projects/project-1/setup/preview', async ({ request }) => { previewed = await request.json() as typeof previewed; return HttpResponse.json({ plan_token: 't'.repeat(64), expected_revision: 4, desired_setup: previewed.desired_setup, impacts: [{ impact_id: 'add_view:x:', kind: 'add_view', view_id: 'x', motion_id: null, name: 'Back', candidate_count: 0, revision_count: 0 }], blocked_operations: [] }) }),
    http.post('/studio/projects/project-1/setup/apply', () => { applied = true; return HttpResponse.json(project()) }),
  )
  const user = userEvent.setup()
  const { rerender } = render(<TurnaroundPanel project={project()} onClose={() => {}} />, { wrapper: queryWrapper() })
  const tile = await screen.findByRole('article', { name: 'N draft' })
  // The standing pass is not offered yet: one note, not a disabled button per direction.
  expect(screen.getByText('Standing versions: coming later.')).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: /standing/ })).not.toBeInTheDocument()
  expect(within(tile).getByText(/Synthesized:/)).toBeInTheDocument()
  await user.click(within(tile).getByLabelText(/Facing is correct: Back to you\./))
  await user.click(within(tile).getByLabelText(/Equipment is where the card says: Axe in the hand on the picture's right\./))
  await user.click(within(tile).getByLabelText(/Same face and outfit/))
  await user.click(within(tile).getByRole('button', { name: 'Save check' }))
  await expect.poll(() => checked.identity).toBe(true)
  expect(checked).toEqual({ expected_revision: 4, facing: true, equipment: true, identity: true })
  rerender(<TurnaroundPanel project={{ ...project(), revision: 5 }} onClose={() => {}} />)
  await user.click(await screen.findByRole('button', { name: 'Use as N reference' }))
  await user.click(await screen.findByRole('button', { name: 'Confirm' }))
  await expect.poll(() => applied).toBe(true)
  const north = previewed.desired_setup?.views.find((view) => view.heading === 'N')
  expect(north?.reference).toEqual({ revision_id: DRAFT })
})

it('says why a stale draft can no longer be used and offers no binding', async () => {
  const picked = { ...settled, pick_set: { pick_revision: 2, picks: {}, findings: [], saved_by: 'u', saved_at: 'now' } }
  const stale: ViewDraft = { ...draft, checked: true, bindable: false, stale_reason: 'the saved picks changed since this draft was made; make it again from them' }
  serve({ takes: [picked], detail: picked, drafts: [stale] })
  render(<TurnaroundPanel project={project()} onClose={() => {}} />, { wrapper: queryWrapper() })
  const tile = await screen.findByRole('article', { name: 'N draft' })
  expect(within(tile).getByRole('note')).toHaveTextContent('can no longer be used: the saved picks changed')
  expect(within(tile).queryByRole('button', { name: 'Use as N reference' })).not.toBeInTheDocument()
})

function LiveProject() {
  // The panel as the workspace renders it: from the project query, so a refresh reaches it.
  const live = useProject('project-1')
  return live.data ? <TurnaroundPanel project={live.data} onClose={() => {}} /> : null
}

it('uses the project revision that saving picks produced when it makes a draft', async () => {
  let revision = 4; const drafts: Record<string, unknown>[] = []
  const pickedTake = { ...settled, pick_set: { pick_revision: 1, picks: Object.fromEntries(Object.entries(settled.suggested_picks ?? {}).filter(([h]) => h !== 'S').map(([h, e]) => [h, { frame_index: e!.frame }])), findings: [], saved_by: 'u', saved_at: 'now' } }
  let current: Turnaround = settled
  serve({ takes: [settled] })
  server.use(
    http.get('/studio/projects/project-1', () => HttpResponse.json({ ...project(), revision })),
    http.get(`/studio/projects/project-1/turnarounds/${TURN}`, () => HttpResponse.json(current)),
    http.put(`/studio/projects/project-1/turnarounds/${TURN}/picks`, () => { revision = 5; current = pickedTake; return HttpResponse.json(pickedTake.pick_set) }),
    http.post('/studio/projects/project-1/view-drafts', async ({ request }) => { drafts.push(await request.json() as Record<string, unknown>); return HttpResponse.json({ draft_id: DRAFT, draft }) }),
  )
  const user = userEvent.setup()
  render(<LiveProject />, { wrapper: queryWrapper() })
  await user.click(await screen.findByRole('button', { name: 'Save picks' }))
  const row = (await screen.findByText('N · Back', { selector: '.turnaround-panel__draft-row > strong' })).parentElement!
  await user.click(within(row).getByRole('button', { name: 'Use this frame' }))
  await expect.poll(() => drafts.length).toBe(1)
  expect(drafts[0].expected_revision).toBe(5)
})

it('keeps polling a completed take until it settles, then shows the picks', async () => {
  let listed: Turnaround = { ...settled, status: 'completed', settled: false }
  serve({ takes: [listed] })
  server.use(http.get('/studio/projects/project-1/turnarounds', () => HttpResponse.json({ turnarounds: [listed] })))
  render(<TurnaroundPanel project={project()} onClose={() => {}} />, { wrapper: queryWrapper() })
  expect(await screen.findByText(/Turning your character/)).toBeInTheDocument()
  listed = settled
  expect(await screen.findByRole('figure', { name: 'Back (N)' }, { timeout: 5000 })).toBeInTheDocument()
}, 10_000)

it('asks for the picks to be saved before a draft is made from a moved frame', async () => {
  const picked = { ...settled, pick_set: { pick_revision: 1, picks: Object.fromEntries(Object.entries(settled.suggested_picks ?? {}).filter(([h]) => h !== 'S').map(([h, e]) => [h, { frame_index: e!.frame }])), findings: [], saved_by: 'u', saved_at: 'now' } }
  serve({ takes: [picked], detail: picked })
  render(<TurnaroundPanel project={project()} onClose={() => {}} />, { wrapper: queryWrapper() })
  const row = (await screen.findByText('N · Back', { selector: '.turnaround-panel__draft-row > strong' })).parentElement!
  expect(within(row).getByRole('button', { name: 'Use this frame' })).toBeEnabled()
  const north = screen.getByRole('figure', { name: 'Back (N)' })
  fireEvent.change(within(north).getByLabelText('N frame'), { target: { value: '33' } })
  expect(within(row).getByRole('button', { name: 'Use this frame' })).toBeDisabled()
  expect(screen.getByText(/Save your picks first/)).toBeInTheDocument()
})

it('lets the person go back to an earlier take', async () => {
  const later: Turnaround = { ...settled, turnaround_id: `turnaround-${'c'.repeat(32)}`, status: 'failed', settled: false }
  serve({ takes: [settled, later] })
  const user = userEvent.setup()
  render(<TurnaroundPanel project={project()} onClose={() => {}} />, { wrapper: queryWrapper() })
  expect(await screen.findByText(/The turnaround failed/)).toBeInTheDocument()
  await user.selectOptions(screen.getByLabelText('Turnaround take'), 'Take 1')
  expect(await screen.findByRole('figure', { name: 'Back (N)' })).toBeInTheDocument()
})

it('starts from a three-quarter (SE) reference when there is no front one', async () => {
  serve()
  let body: Record<string, unknown> = {}
  server.use(http.post('/studio/projects/project-1/turnarounds', async ({ request }) => { body = await request.json() as Record<string, unknown>; return HttpResponse.json({ turnaround_id: TURN, operation: { job_id: 'j' } }) }))
  const user = userEvent.setup()
  render(<TurnaroundPanel project={project([{ view_id: 'view-se', heading: 'SE' as never, name: 'Front right', reference: { ...sourceReference, revision_id: 'rev-se' } }])} onClose={() => {}} />, { wrapper: queryWrapper() })
  await user.click(await screen.findByRole('button', { name: 'Generate' }))
  await expect.poll(() => body.source_revision_id).toBe('rev-se')
  expect(body.source_view_id).toBe('view-se')
})

it('lays out the picks from an SE source with the source as the anchor, never a slot', async () => {
  const fromSe = { ...settled, source_view_id: 'view-se', source_heading: 'SE' as const }
  serve({ takes: [fromSe], detail: fromSe })
  render(<TurnaroundPanel project={project([{ view_id: 'view-se', heading: 'SE' as never, name: 'Front right', reference: sourceReference }])} onClose={() => {}} />, { wrapper: queryWrapper() })
  expect(await screen.findByRole('figure', { name: 'Front (S)' })).toBeInTheDocument()
  expect(screen.queryByRole('figure', { name: 'Front right (SE)' })).not.toBeInTheDocument()
})

it('asks again for confirmation once a confirmed pick is moved', async () => {
  const saved = Object.fromEntries(Object.entries(settled.suggested_picks ?? {}).filter(([h]) => h !== 'S').map(([h, e]) => [h, { frame_index: e!.frame }]))
  const confirmed = { ...settled, pick_set: { pick_revision: 1, picks: { ...saved, N: { frame_index: 60, confirm_out_of_region: true } }, findings: [{ heading: 'N' as const, kind: 'out_of_region' as const, distance: 9 }], saved_by: 'u', saved_at: 'now' } }
  serve({ takes: [confirmed], detail: confirmed })
  const bodies: { picks: Record<string, Record<string, unknown>> }[] = []
  server.use(http.put(`/studio/projects/project-1/turnarounds/${TURN}/picks`, async ({ request }) => { bodies.push(await request.json() as typeof bodies[number]); return HttpResponse.json({ ...confirmed.pick_set, pick_revision: 2 }) }))
  const user = userEvent.setup()
  render(<TurnaroundPanel project={project()} onClose={() => {}} />, { wrapper: queryWrapper() })
  const north = await screen.findByRole('figure', { name: 'Back (N)' })
  expect(within(north).getByLabelText('N frame')).toHaveValue('60')
  fireEvent.change(within(north).getByLabelText('N frame'), { target: { value: '61' } })
  await user.click(screen.getByRole('button', { name: 'Save picks' }))
  await expect.poll(() => bodies.length).toBe(1)
  // The confirmation was for frame 60; frame 61 is the server's to judge afresh.
  expect(bodies[0].picks.N).toEqual({ frame_index: 61 })
})

it('recognizes the front reference of a legacy compass project, whose S view has no heading field', async () => {
  serve()
  render(<TurnaroundPanel project={project([{ view_id: 'S', name: 'S', reference: sourceReference } as never])} onClose={() => {}} />, { wrapper: queryWrapper() })
  expect(await screen.findByRole('button', { name: 'Generate' })).toBeInTheDocument()
})
