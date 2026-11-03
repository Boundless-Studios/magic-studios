import { expect, test, type Page, type Route, type TestInfo } from '@playwright/test'

// A real browser on a real onboarded project. The local studio has no GPU pool, so the
// turnaround, its picks, the drafts and the setup change are fixture responses: this proves
// the human flow and what the panel sends, not generation. The cards, reasons and suggested
// frames are what the server computed for the ranger turntable proof (H3 seed 42, a 4 s closed
// loop, kept in the private art-archive-2026-11-02 release, not in Git). The pictures are a
// drawn stand-in, a numbered figure turning once, not the proof's frames. The ranger's source
// is near-frontal, so it is a true S source (the orc's faces SE: the three-quarter case).
const TURN = `turnaround-${'a'.repeat(32)}`
const DRAFT = `draft-${'b'.repeat(32)}`
const COUNT = 96
const SUGGESTED = { S: 0, SE: 18, E: 35, NE: 47, N: 59, NW: 70, W: 80, SW: 93 }
const REASON: Record<string, string> = {
  S: 'the source, where the loop starts and ends', E: 'the body is narrowest here, a profile', W: 'the body is narrowest here, a profile',
  N: 'the body is widest here, between the two profiles',
}
const CARDS: Record<string, string[]> = {
  SE: ['Facing front-right.', 'Bow in the near hand.', 'Quiver on the back, behind the body.'],
  E: ['Right profile.', 'Bow toward you.', 'Quiver on the back, seen from the side.'],
  NE: ['Facing back-right.', 'Bow in the near hand.', 'Quiver on the back, in view.'],
  N: ['Back to you.', "Bow in the hand on the picture's right.", 'Quiver on the back, in view.'],
  NW: ['Facing back-left.', 'Bow in the far hand.', 'Quiver on the back, in view.'],
  W: ['Left profile.', 'Bow in the far hand.', 'Quiver on the back, seen from the side.'],
  SW: ['Facing front-left.', 'Bow in the far hand.', 'Quiver on the back, behind the body.'],
}
const BACK = 'the back of the head, hair and outfit, and equipment on the back'
const SYNTHESIZED = { SE: ["the character's right side"], E: ["the character's right side"], NE: [BACK, "the character's right side"], N: [BACK], NW: [BACK, "the character's left side"], W: ["the character's left side"], SW: ["the character's left side"] }

/** A drawn turntable: a figure that turns once on chroma green, each frame numbered so no two
 * match, plus cut-outs on a transparent backdrop of the source and the W pick. */
async function rangerTurntable(page: Page) {
  return page.evaluate(({ count, west }) => {
    const canvas = document.createElement('canvas')
    const context = canvas.getContext('2d')!
    const draw = (index: number, size: number, backdrop: boolean) => {
      canvas.width = canvas.height = size
      context.setTransform(size / 512, 0, 0, size / 512, 0, 0)
      if (backdrop) { context.fillStyle = '#00b140'; context.fillRect(0, 0, 512, 512) }
      // The torso is widest facing you or away and narrowest in profile, as a real turn measures.
      const angle = (2 * Math.PI * index) / (count - 1)
      const half = Math.hypot(70 * Math.cos(angle), 28 * Math.sin(angle))
      context.fillStyle = '#d6aa8c'
      context.beginPath(); context.arc(256, 100, 30, 0, 2 * Math.PI); context.fill()
      context.fillStyle = '#963c28'; context.fillRect(256 - half, 130, 2 * half, 190)
      context.fillStyle = '#3c325a'; context.fillRect(256 - (half * 2) / 3, 320, (half * 4) / 3, 130)
      if (backdrop) { context.fillStyle = '#0f172a'; context.font = 'bold 32px sans-serif'; context.fillText(String(index), 20, 490) }
    }
    const frames = Array.from({ length: count }, (_, index) => { draw(index, 256, true); return canvas.toDataURL('image/jpeg', 0.85) })
    draw(0, 512, false)
    const source = canvas.toDataURL('image/png')
    draw(west, 512, false)
    return { frames, source, draft: canvas.toDataURL('image/png') }
  }, { count: COUNT, west: SUGGESTED.W })
}

async function onboardRanger(page: Page, source: string) {
  await page.goto('/projects/new')
  await page.getByLabel('Choose character image').setInputFiles({ name: 'ranger.png', mimeType: 'image/png', buffer: Buffer.from(source.split(',')[1], 'base64') })
  await page.getByLabel('Character / project name').fill('Turnaround panel acceptance')
  await page.getByRole('button', { name: 'Continue', exact: true }).click()
  await page.getByRole('button', { name: /Four-direction/ }).click()
  await page.getByRole('button', { name: 'Continue', exact: true }).click()
  await page.getByRole('button', { name: /South · toward you/ }).click()
  await page.getByRole('button', { name: 'Continue', exact: true }).click()
  await page.getByRole('button', { name: 'Continue', exact: true }).click()
  // Review offers the other directions because the artwork faces S and a machine can turn it.
  await page.getByRole('button', { name: 'Generate the other directions' }).click()
  await expect(page).toHaveURL(/\/projects\/[^/]+\/motions$/)
  return page.url().split('/').at(-2)!
}

/** The whole panel as a person sees it, on a desktop and on a 375px phone. */
async function shoot(page: Page, info: TestInfo, name: string) {
  const desktop = page.viewportSize()!
  for (const [label, width] of [['desktop', desktop.width], ['375', 375]] as const) {
    await page.setViewportSize({ width, height: desktop.height })
    // The overlay scrolls, so grow the viewport to the panel's height (plus the overlay's padding).
    const height = await page.locator('.turnaround-panel').evaluate((element) => element.scrollHeight)
    await page.setViewportSize({ width, height: height + 64 })
    await page.screenshot({ path: info.outputPath(`${name}-${label}.png`) })
  }
  await page.setViewportSize(desktop)
}

test('a person turns the front artwork, picks frames, checks a draft and uses it as a direction', async ({ page }, info) => {
  info.setTimeout(180_000)
  await page.goto('/')
  const ranger = await rangerTurntable(page)
  const json = (route: Route, body: unknown) => route.fulfill({ json: body })
  await page.route('**/studio/generation-config', (route) => json(route, { workers: [{ worker_id: 'desk', available: true, reason: null, profile: { profile: 'h3-24gb-standard' }, features: { last_frame: true } }] }))
  const id = await onboardRanger(page, ranger.source)
  const base = `/studio/projects/${id}`
  const sent: Record<string, unknown> = {}
  let take: Record<string, unknown> = {
    turnaround_id: TURN, source_view_id: 'S', source_heading: 'S', source_superseded: false, source_backdrop: 'transparent', equipment: [], prompt: 'p', job_id: 'j',
    status: 'processing', settled: false, frame_count: COUNT, cards_version: 'placement_cards.v1', placement_cards: CARDS, synthesized_parts: SYNTHESIZED, pick_set: null,
  }
  const suggested = Object.fromEntries(Object.entries(SUGGESTED).map(([h, frame]) => [h, { frame, reason: REASON[h] ?? 'evenly spaced between its neighbours' }]))
  let draft: Record<string, unknown> | null = null
  await page.route(`**${base}/turnarounds`, async (route) => {
    if (route.request().method() === 'POST') { sent.start = route.request().postDataJSON(); return json(route, { turnaround_id: TURN, operation: { job_id: 'j' } }) }
    return json(route, { turnarounds: sent.start ? [take] : [] })
  })
  await page.route(`**${base}/turnarounds/${TURN}`, (route) => json(route, { ...take, suggested_picks: suggested }))
  await page.route(`**${base}/turnarounds/${TURN}/frames*`, (route) => {
    const query = new URL(route.request().url()).searchParams
    const start = Number(query.get('start') ?? 0)
    const indexes = Array.from({ length: Number(query.get('limit') ?? 32) }, (_, i) => start + i).filter((index) => index < COUNT)
    return json(route, { frames: indexes.map((index) => ({ index, image: ranger.frames[index] })) })
  })
  await page.route(`**${base}/turnarounds/${TURN}/picks`, (route) => {
    sent.picks = route.request().postDataJSON()
    take = { ...take, pick_set: { pick_revision: 1, picks: (sent.picks as { picks: unknown }).picks, findings: [], saved_by: 'u', saved_at: 'now' } }
    return json(route, take.pick_set)
  })
  await page.route(`**${base}/view-drafts`, async (route) => {
    if (route.request().method() === 'POST') {
      sent.draft = route.request().postDataJSON()
      draft = { draft_id: DRAFT, turnaround_id: TURN, heading: 'W', pick_revision: 1, frame_index: SUGGESTED.W, suggested_frame: SUGGESTED.W, picks_version: 'turnaround-picks-v2', stale_reason: null, image: { key: 'k', sha256: 'h', width: 1024, height: 1024 }, image_path: ranger.draft, placement_card: CARDS.W, synthesized_parts: SYNTHESIZED.W, check: null, checked: false, discarded: false, bound_view_ids: [], bindable: false }
      return json(route, { draft_id: DRAFT, draft })
    }
    return json(route, { drafts: draft ? [draft] : [] })
  })
  await page.route(`**${base}/view-drafts/${DRAFT}/check`, (route) => { sent.check = route.request().postDataJSON(); draft = { ...draft, checked: true, bindable: true }; return json(route, draft) })
  await page.route(`**${base}/setup/preview`, (route) => { sent.preview = route.request().postDataJSON(); return json(route, { plan_token: 't'.repeat(64), expected_revision: 1, desired_setup: (sent.preview as { desired_setup: unknown }).desired_setup, impacts: [{ impact_id: 'add_view:W:', kind: 'add_view', view_id: 'W', motion_id: null, name: 'Left', candidate_count: 0, revision_count: 0 }], blocked_operations: [] }) })
  await page.route(`**${base}/setup/apply`, async (route) => { sent.apply = route.request().postDataJSON(); draft = { ...draft, bound_view_ids: ['W'], bindable: false }; return json(route, await (await page.request.get(base, { headers: { 'X-Studio-Setup-Version': '1' } })).json()) })

  // Onboarding opened the panel; it is the same one the setup page's button opens.
  const panel = page.getByRole('dialog', { name: 'Generate other directions' })
  await expect(panel).toBeVisible()
  await panel.getByRole('button', { name: 'Add equipment' }).click()
  await panel.getByLabel('Item 1 name').fill('bow')
  await panel.getByRole('button', { name: 'Add equipment' }).click()
  await panel.getByLabel('Item 2 name').fill('quiver')
  await panel.getByLabel('Item 2 attachment').selectOption('back')
  await panel.getByLabel('Item 2 picture side').selectOption('unsided')
  await expect(panel.getByText("bow: the character's right hand")).toBeVisible()
  await expect(panel.getByText('quiver: back', { exact: true })).toBeVisible()
  await panel.getByRole('button', { name: 'Generate' }).click()
  await expect(panel.getByRole('status')).toContainText('Turning your character')
  await page.screenshot({ path: info.outputPath('turning.png'), fullPage: true })
  expect(sent.start).toEqual(expect.objectContaining({
    source_view_id: expect.any(String),
    equipment: [{ item: 'bow', attachment: 'hand', source_picture_side: 'left' }, { item: 'quiver', attachment: 'back', source_picture_side: 'unsided' }],
  }))

  // The take settles: each slot opens on the suggested frame, with its card and reason.
  take = { ...take, status: 'completed', settled: true }
  const west = panel.getByRole('figure', { name: 'Left (W)' })
  await expect(west).toBeVisible({ timeout: 15_000 })
  await expect(west.getByLabel('W frame')).toHaveValue('80')
  await expect(west.getByText('Left profile. Bow in the far hand. Quiver on the back, seen from the side.')).toBeVisible()
  await expect(west.getByText('Suggested frame 80: the body is narrowest here, a profile.')).toBeVisible()
  await expect(west.getByRole('img', { name: 'W frame 80' })).toHaveAttribute('src', ranger.frames[80])
  // The person moves SW back to the frame the turntable proof picked by eye, five frames before the suggestion.
  const southwest = panel.getByRole('figure', { name: /\(SW\)/ })
  await southwest.getByLabel('SW frame').fill('88')
  await panel.getByRole('button', { name: 'Save picks' }).click()
  await expect(panel.getByText('Saved pick set 1.')).toBeVisible()
  expect((sent.picks as { picks: Record<string, { frame_index: number }> }).picks.SW.frame_index).toBe(88)
  await shoot(page, info, 'picks')

  await panel.locator('.turnaround-panel__draft-row').filter({ hasText: 'W · Left' }).getByRole('button', { name: 'Use this frame', exact: true }).click()
  const tile = panel.getByRole('article', { name: 'W draft' })
  await expect(tile.getByText(/Synthesized:/)).toBeVisible()
  await shoot(page, info, 'drafts')
  await tile.getByLabel('Facing is correct: Left profile.').check()
  await tile.getByLabel('Equipment is where the card says: Bow in the far hand. Quiver on the back, seen from the side.').check()
  await tile.getByLabel(/Same face and outfit/).check()
  await tile.getByRole('button', { name: 'Save check' }).click()
  await tile.getByRole('button', { name: 'Use as W reference' }).click()
  await tile.getByRole('button', { name: 'Confirm' }).click()
  await expect(tile.getByText('Used as the W reference.')).toBeVisible()
  await shoot(page, info, 'bound')
  expect(sent.check).toEqual(expect.objectContaining({ facing: true, equipment: true, identity: true }))
  const views = (sent.preview as { desired_setup: { views: { heading: string; reference: unknown }[] } }).desired_setup.views
  expect(views.find((view) => view.heading === 'W')?.reference).toEqual({ revision_id: DRAFT })
})
