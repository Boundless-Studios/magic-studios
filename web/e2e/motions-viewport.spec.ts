import { expect, test, type Page } from '@playwright/test'

import { subjectPng } from './subjectImage'

const VIEWPORTS = [
  { name: '1440x900', width: 1440, height: 900 },
  { name: '1280x800', width: 1280, height: 800 },
  { name: '900x900', width: 900, height: 900 },
]

/** The hard acceptance from the operator's verdict on the Motions page: no page scroll at
 * these viewports, in any of the states below. Inner regions (rail, timeline, candidate
 * strip, advanced panel) may still scroll -- only `document.scrollingElement` must not. */
async function expectNoPageScroll(page: Page, state: string) {
  if ((page.viewportSize()?.width ?? 1440) <= 960) return // Stacked layout intentionally scrolls the page.
  const { scrollHeight, innerHeight } = await page.evaluate(() => ({
    scrollHeight: document.scrollingElement!.scrollHeight,
    innerHeight: window.innerHeight,
  }))
  expect(scrollHeight, `${state}: document.scrollingElement.scrollHeight (${scrollHeight}) should not exceed window.innerHeight (${innerHeight})`).toBeLessThanOrEqual(innerHeight + 1)
}

async function createApprovedProject(page: Page, name: string): Promise<string> {
  // Seed legacy unassigned state; artwork-first creation has its own browser journey.
  const created = await page.request.post('/studio/projects', { data: { name } })
  expect(created.ok()).toBe(true)
  const project = await created.json() as { project_id: string }

  await page.goto(`/projects/${project.project_id}/subject`)
  await page.getByLabel('Choose subject references').setInputFiles(subjectPng)
  await page.getByLabel(/Guidance/).fill('Preserve the warm palette and readable silhouette.')
  await page.getByRole('button', { name: 'Generate subject' }).click()
  await expect(page.getByRole('heading', { name: 'Ready for approval' })).toBeVisible({ timeout: 10_000 })
  await page.getByRole('button', { name: 'Approve subject' }).click()
  await expect(page.getByRole('heading', { name: 'Build the animation set' })).toBeVisible()
  return project.project_id
}

for (const viewport of VIEWPORTS) {
  test.describe(`Motions workspace fits the viewport at ${viewport.name}`, () => {
    test.use({ viewport: { width: viewport.width, height: viewport.height } })

    test(`no page scroll through the motions lifecycle at ${viewport.name}`, async ({ page }, testInfo) => {
      testInfo.setTimeout(120_000)

      // (a) empty project right after subject approval: the recommended-set card.
      await createApprovedProject(page, `Viewport ${viewport.name} ${testInfo.testId}`)
      await expect(page.getByText('Idle, Walk, Attack, Guard, Cast, Hurt')).toBeVisible()
      await expectNoPageScroll(page, 'empty project (recommended-set card)')

      // (b) six motions added, all pre-checked and ready for one batch (no compose/approve step).
      await page.getByRole('button', { name: 'Add the recommended set' }).click()
      await expect(page.getByRole('option', { name: /Idle To generate/ })).toBeVisible()
      await expect(page.getByRole('option', { name: /Hurt To generate/ })).toBeVisible()
      await expect(page.getByRole('button', { name: 'Generate 6 motions' })).toBeVisible()
      await expectNoPageScroll(page, 'six motions added, ready to generate')

      // (c) batch generating.
      await page.getByRole('button', { name: 'Generate 6 motions' }).click()
      await page.getByRole('dialog', { name: 'What happens in each motion' }).getByRole('button', { name: 'Generate', exact: true }).click()
      await expect(page.getByRole('option', { name: /Idle (Queued|Generating)/ })).toBeVisible({ timeout: 10_000 })
      await expectNoPageScroll(page, 'batch generating')

      // (d) candidates ready, Idle selected with the frame editor open on a 4-frame candidate.
      await expect(page.getByRole('option', { name: /Idle (Review|Selected)/ })).toBeVisible({ timeout: 30_000 })
      await page.getByRole('option', { name: /Idle/ }).click()
      const candidateGroup = page.getByRole('listitem', { name: /Take/ }).first()
      await expect(candidateGroup).toBeVisible({ timeout: 10_000 })
      const selectButton = candidateGroup.getByRole('button', { name: 'Use animation' })
      if (await selectButton.count()) await selectButton.click()
      await candidateGroup.getByRole('button', { name: 'Edit frames' }).click()
      await expect(page.getByRole('region', { name: 'Idle frame editor' })).toBeVisible()
      await expect(page.getByRole('button', { name: 'Exclude frame 1' })).toBeVisible()

      // (1) The stage must actually draw the frame: a real background-image, and a rendered
      // size big enough to be a usable preview (not a solid-color box or a collapsed 0x0 one).
      const stageBox = await page.locator('.frame-editor__stage').boundingBox()
      const stageBackground = await page.locator('.frame-editor__stage').evaluate((el) => getComputedStyle(el).backgroundImage)
      expect(stageBackground, 'stage should draw a real sprite frame, not an empty background').toMatch(/^url\(/)
      expect(stageBox?.width ?? 0, 'stage should render at a usable width').toBeGreaterThanOrEqual(200)
      expect(stageBox?.height ?? 0, 'stage should render at a usable height').toBeGreaterThanOrEqual(200)

      // (2) The stage must letterbox the sprite, never crop it: its own box must fit entirely
      // inside the stage-frame container on both axes (a crop would make it taller/wider than
      // its container, since the container clips with overflow:hidden).
      const stageFrameBox = await page.locator('.frame-editor__stage-frame').boundingBox()
      if (stageBox && stageFrameBox) {
        expect(stageBox.height, 'stage must not exceed its container height (no crop)').toBeLessThanOrEqual(stageFrameBox.height + 1)
        expect(stageBox.width, 'stage must not exceed its container width (no crop)').toBeLessThanOrEqual(stageFrameBox.width + 1)
      }

      // The rail's own thumbnail for the now-generated Idle motion must show a real frame too
      // (a background-image), not the solid-color placeholder tile.
      const railThumbBackground = await page.getByRole('option', { name: /Idle/ }).locator('.motion-rail__thumb-frame').evaluate((el) => getComputedStyle(el).backgroundImage)
      expect(railThumbBackground, 'rail thumbnail should show a real sprite frame, not a blank tile').toMatch(/^url\(/)

      await page.getByRole('list', { name: 'Animation frames' }).scrollIntoViewIfNeeded()
      if (viewport.width <= 960) {
        const timeline = await page.locator('.frame-timeline').boundingBox()
        const settings = await page.locator('.frame-editor__settings').boundingBox()
        const actions = await page.locator('.frame-editor__actions').boundingBox()
        expect(timeline!.y + timeline!.height).toBeLessThanOrEqual(settings!.y)
        expect(settings!.y + settings!.height).toBeLessThanOrEqual(actions!.y)
      }
      // Dragging selects a range; clicking a selected thumbnail removes only that frame.
      const second = await page.getByRole('button', { name: 'Exclude frame 2' }).boundingBox()
      const fourth = await page.getByRole('button', { name: 'Exclude frame 4' }).boundingBox()
      if (!second || !fourth) throw new Error('Frame thumbnails missing')
      await page.mouse.move(second.x + second.width / 2, second.y + second.height / 2)
      await page.mouse.down()
      await page.mouse.move(fourth.x + fourth.width / 2, fourth.y + fourth.height / 2, { steps: 10 })
      await page.mouse.up()
      await expect(page.getByRole('button', { name: 'Include frame 1' })).toHaveAttribute('aria-pressed', 'false')
      await expect(page.getByRole('button', { name: 'Exclude frame 4' })).toHaveAttribute('aria-pressed', 'true')
      await page.getByRole('button', { name: 'Exclude frame 3' }).click()
      await expect(page.getByText('Draft · 2 of 4 frames')).toBeVisible()
      await expect(page.getByLabel('Hold for frame 3')).toBeHidden()
      await page.getByRole('button', { name: 'Discard draft' }).click()
      await page.screenshot({ path: testInfo.outputPath(`editor-${viewport.name}.png`) })

      await expectNoPageScroll(page, 'candidates ready, Idle frame editor open')

      // (e) Advanced generation settings expanded (the Generate zone itself is collapsed by
      // default once a motion has takes).
      await page.getByRole('button', { name: 'Expand' }).click()
      await page.getByRole('button', { name: 'Advanced generation settings' }).click()
      await expect(page.getByLabel('Preset')).toBeVisible()
      await expectNoPageScroll(page, 'Advanced generation settings expanded')
    })
  })
}
