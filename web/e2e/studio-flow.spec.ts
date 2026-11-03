import { execFileSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import { writeFile } from 'node:fs/promises'
import { expect, test, type Page } from '@playwright/test'

import { subjectPng } from './subjectImage'

const API_CREATE_CEILING_MS = 750
// Approve now shows a green confirmation state on the Chosen card and deliberately waits ~800ms
// before handing off to Motions (operator, 2026-09-05), so the operator sees the confirmation
// before the page changes -- this budget carries that intentional delay plus rendering overhead.
const CACHED_NAVIGATION_CEILING_MS = 2000
// The desktop runner label is shared with other repos' CI, so wall-clock budgets there measure
// the box's load, not the studio. Locally they stay tight.
const CI_SLACK = process.env.CI ? 9 : 1
const GENERATION_CEILING_MS = 10_000 * CI_SLACK
const READY_TIMEOUT_MS = 10_000 * CI_SLACK

/** These existing journeys exercise legacy unassigned projects. New human creation is
 * covered by art-first.spec.ts; seed legacy state through its supported HTTP boundary. */
async function createLegacyProject(page: Page, name: string) {
  const response = await page.request.post('/studio/projects', { data: { name } })
  expect(response.ok()).toBe(true)
  return response
}

test('runs the real local subject-to-export pipeline', async ({ page }, testInfo) => {
  const apiStarted = performance.now()
  const createResponse = Promise.resolve(await createLegacyProject(page, 'Ember Knight'))
  const created = await createResponse
  expect(created.ok()).toBe(true)
  const project = await created.json() as { project_id: string }
  const apiCreateMs = performance.now() - apiStarted

  await page.goto(`/projects/${project.project_id}/subject`)
  const jpegReference = testInfo.outputPath('subject-reference.jpg')
  execFileSync('ffmpeg', ['-y', '-i', subjectPng, '-pix_fmt', 'yuvj420p', jpegReference], { stdio: 'ignore' })
  await page.getByLabel('Choose subject references').setInputFiles(jpegReference)
  const referencePreview = page.getByRole('img', { name: /Reference 1:/ })
  await expect(referencePreview).toBeVisible()
  expect(await referencePreview.evaluate((image) => getComputedStyle(image).objectFit)).toBe('contain')
  expect((await referencePreview.locator('..').boundingBox())?.width).toBeLessThanOrEqual(280)
  await page.getByLabel(/Guidance/).fill('Preserve the warm palette and readable silhouette.')
  await page.getByRole('button', { name: 'Generate subject' }).click()
  // The button is its own progress indicator while the job is in flight (BOU-3208 operator
  // feedback), and the just-uploaded reference stays visible rather than being cleared on submit.
  // The same generation also shows as a live placeholder card in the Subject takes gallery
  // (studio-ux-flow.md section 3), which now carries the same "Generating…" text too -- `.first()`
  // keeps this an equivalent check to the single-match original rather than a stricter one, since
  // which of the two elements a fast local run catches first is not itself under test here.
  // A fast local job may settle before the browser observes its transient spinner.
  await expect(page.getByText('Generating…').first().or(page.getByRole('heading', { name: 'Ready for approval' })).first()).toBeVisible()
  await expect(referencePreview).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Ready for approval' })).toBeVisible({ timeout: READY_TIMEOUT_MS })
  // BOU-3208: the page used to keep showing "generating" (Regenerate disabled) forever once the
  // job settled, because it polled the plain operations listing and never refetched the project
  // on settlement -- only a reload cleared it. Assert the live page itself, with no reload, both
  // stops showing "generating" and re-enables the button, and that the submitted direction is
  // still in the field (it used to clear on submit).
  await expect(page.getByRole('button', { name: /Regenerate subject/ })).toBeEnabled()
  await expect(referencePreview).toBeVisible()
  await expect(page.getByLabel(/Guidance/)).toHaveValue('Preserve the warm palette and readable silhouette.')
  // Playwright traces only the first retry, and the first attempt is the one that runs with
  // every worker busy, so trace this one step there and keep it when the budget is missed.
  const traceBudget = testInfo.retry === 0
  if (traceBudget) await page.context().tracing.start({ snapshots: true })
  await page.evaluate(() => performance.mark('cached-navigation-start'))
  await page.getByRole('button', { name: 'Approve subject' }).click()
  await expect(page.getByRole('heading', { name: 'Build the animation set' })).toBeVisible()
  const cachedNavigationMs = await page.evaluate(() => { performance.mark('cached-navigation-ready'); return performance.measure('cached-navigation', 'cached-navigation-start', 'cached-navigation-ready').duration })
  // Recorded in the report on passing runs too, so CI shows how close it runs to the budget.
  testInfo.annotations.push({ type: 'cachedNavigationMs', description: cachedNavigationMs.toFixed(0) })
  if (traceBudget) {
    const overBudget = cachedNavigationMs >= CACHED_NAVIGATION_CEILING_MS
    const tracePath = testInfo.outputPath('cached-navigation-trace.zip')
    await page.context().tracing.stop(overBudget ? { path: tracePath } : undefined)
    if (overBudget) await testInfo.attach('cached-navigation-trace', { path: tracePath, contentType: 'application/zip' })
  }
  expect(cachedNavigationMs).toBeLessThan(CACHED_NAVIGATION_CEILING_MS)

  // "Add motion" opens the preset grid (T5); each card's own button adds that preset.
  await page.getByRole('button', { name: 'Add motion' }).click()
  await page.getByRole('button', { name: 'Add Idle' }).click()
  const idleStarted = performance.now()
  await page.getByRole('button', { name: 'Generate Idle' }).click()
  await expect(page.getByRole('option', { name: 'Idle Generating' })).toBeVisible()
  // The Activity panel stays closed for this whole wait: settlement must not depend on it being open.
  await expect(page.getByRole('dialog', { name: 'GPU status' })).toHaveCount(0)
  await expect(page.getByRole('listitem', { name: /Take/ })).toBeVisible({ timeout: READY_TIMEOUT_MS })
  await expect(page.getByRole('dialog', { name: 'GPU status' })).toHaveCount(0)
  const idleGenerationMs = performance.now() - idleStarted
  expect(idleGenerationMs).toBeLessThan(GENERATION_CEILING_MS)

  // T20: the candidate card's Download menu resolves a signed read URL per format.
  const candidateCard = page.getByRole('listitem', { name: /Take/ })
  await candidateCard.getByRole('button', { name: 'Download' }).click()
  const sheetDownloadResponse = page.waitForResponse((response) => response.url().includes('/sheet.png') && response.request().method() === 'GET')
  // Downloads are attachment navigations now, not popups: Playwright reports them as `download`.
  const [sheetDownload] = await Promise.all([page.waitForEvent('download'), candidateCard.getByRole('button', { name: 'PNG' }).click()])
  expect((await sheetDownloadResponse).ok()).toBe(true)
  expect(sheetDownload.suggestedFilename()).toMatch(/take-1\.png$/)

  await candidateCard.getByRole('button', { name: 'Download' }).click()
  const [gifDownload] = await Promise.all([page.waitForEvent('download'), candidateCard.getByRole('button', { name: 'GIF' }).click()])
  expect(gifDownload.suggestedFilename()).toMatch(/take-1\.gif$/)

  await page.getByRole('listitem', { name: /Take/ }).getByRole('button', { name: 'Use animation' }).click()
  await page.getByRole('listitem', { name: /Take/ }).getByRole('button', { name: 'Edit frames' }).click()
  await expect(page.getByRole('region', { name: 'Idle frame editor' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Exclude frame 4' })).toBeVisible()
  await page.getByRole('button', { name: 'Exclude frame 2' }).click()
  await page.getByText('More frame controls', { exact: true }).click()
  await page.getByLabel('Current frame', { exact: true }).fill('3')
  await page.getByRole('button', { name: 'Move frame 4 earlier' }).click()
  await page.getByLabel('Frames per second').fill('12')
  await page.getByLabel('Flip horizontally').check()
  await expect(page.locator('.frame-editor__stage')).toHaveCSS('transform', 'matrix(-1, 0, 0, 1, 0, 0)')
  await page.getByLabel('Current frame', { exact: true }).fill('0')
  await page.getByLabel('Hold for frame 1').fill('3')
  await expect(page.getByLabel('Hold for frame 1')).toHaveValue('3')
  await expect(page.getByText('Draft · 3 of 4 frames')).toBeVisible()

  // T19: the Packing row stages a resample and the draft duration readout reflects it,
  // then clearing it back to "Keep source" restores the reorder/hold-only draft duration.
  await page.getByLabel('Frame count').selectOption('16')
  await page.getByLabel('Cell size').selectOption('512')
  await expect(page.getByLabel('Cell size')).toHaveValue('512')
  await page.getByLabel('Frame count').selectOption('')
  await expect(page.getByLabel('Cell size')).toHaveValue('')

  await page.getByRole('link', { name: 'Subject', exact: true }).click()
  await page.getByRole('link', { name: 'Motions' }).click()
  await page.reload()
  // The draft itself survives navigation and reload (localStorage); reopening Frames on the
  // same take restores it -- Frames is closed by default until "Edit frames" is clicked again.
  await page.getByRole('listitem', { name: /Take/ }).getByRole('button', { name: 'Edit frames' }).click()
  await expect(page.getByText('Draft · 3 of 4 frames')).toBeVisible()
  const revisionResponse = page.waitForResponse((response) => response.url().endsWith('/motions/idle/revisions') && response.request().method() === 'POST')
  await page.getByRole('button', { name: 'Save revision' }).click()
  expect((await revisionResponse).ok()).toBe(true)
  await expect(page.getByText('Saved · 3 frames')).toBeVisible()

  // "Custom…" reveals the free-text form beside the preset grid (T5).
  await page.getByRole('button', { name: 'Add motion' }).click()
  await page.getByRole('button', { name: 'Custom…' }).click()
  await page.getByLabel('Motion name').fill('Victory')
  await page.getByLabel('What happens', { exact: true }).fill('A confident upward celebration with a clean return.')
  await page.getByRole('button', { name: 'Create motion' }).click()
  const victoryStarted = performance.now()
  await page.getByRole('button', { name: 'Generate Victory' }).click()
  await expect(page.getByRole('option', { name: 'Victory Generating' })).toBeVisible()
  await page.getByRole('button', { name: /GPU status, 1 running/ }).click()
  await expect(page.getByRole('heading', { name: 'GPU status' })).toBeVisible()
  await expect(page.getByText('Victory').last()).toBeVisible()
  await expect(page.getByText('Running').last()).toBeVisible()
  await expect(page.locator('.route-state')).toHaveCount(0)
  await page.getByRole('link', { name: 'Subject', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Create the character to animate' })).toBeVisible()
  await page.getByRole('button', { name: /GPU status/ }).click()
  await expect(page.getByText('Victory').last()).toBeVisible()
  await page.getByRole('button', { name: 'Close activity' }).click()
  await page.getByRole('link', { name: 'Release & export' }).click()
  await page.getByRole('button', { name: /GPU status/ }).click()
  await expect(page.getByText('Victory').last()).toBeVisible()
  await page.getByRole('button', { name: 'Close activity' }).click()
  await page.getByRole('link', { name: 'Motions' }).click()
  await expect(page.getByRole('option', { name: /Victory Review/ })).toBeVisible({ timeout: READY_TIMEOUT_MS })
  const victoryGenerationMs = performance.now() - victoryStarted
  expect(victoryGenerationMs).toBeLessThan(GENERATION_CEILING_MS)
  await page.getByRole('option', { name: /Victory Review/ }).click()
  await page.getByRole('listitem', { name: /Take/ }).getByRole('button', { name: 'Use animation' }).click()

  // Quality is folded into Release (studio-ux-flow.md section 5); the sidebar is three steps.
  await page.getByRole('link', { name: 'Release & export' }).click()
  await expect(page.getByRole('heading', { name: 'Approve the set and ship it' })).toBeVisible()
  const qualityResponse = page.waitForResponse((response) => response.url().endsWith('/quality/set') && response.request().method() === 'POST')
  await page.getByRole('button', { name: 'Run quality checks' }).click()
  expect((await qualityResponse).ok()).toBe(true)
  await expect(page.getByText('No set findings.')).toBeVisible()

  await page.getByRole('link', { name: 'Release & export' }).click()
  await page.getByRole('button', { name: 'Approve animation set' }).click()
  await page.getByLabel('Release name').fill('first-playable')
  const releaseResponsePromise = page.waitForResponse((response) => response.url().endsWith('/releases') && response.request().method() === 'POST')
  await page.getByRole('button', { name: 'Create immutable release' }).click()
  const release = await (await releaseResponsePromise).json() as { release_id: string; release_digest: string; source_revision: number }
  const exportResponsePromise = page.waitForResponse((response) => response.url().endsWith('/exports') && response.request().method() === 'POST')
  await page.getByRole('button', { name: 'Request export' }).click()
  const exportResponse = await exportResponsePromise
  expect(exportResponse.ok(), await exportResponse.text()).toBe(true)
  const exported = await exportResponse.json() as { export_id: string; sha256: string }
  const downloadTarget = await page.request.get(`/studio/exports/${exported.export_id}/download`)
  expect(downloadTarget.ok(), await downloadTarget.text()).toBe(true)
  const downloadPromise = page.waitForEvent('download')
  await page.getByRole('button', { name: 'Download PNG + JSON package' }).click()
  const archivePath = await (await downloadPromise).path()
  expect(archivePath).not.toBeNull()
  const archiveNames = execFileSync('unzip', ['-Z1', archivePath!], { encoding: 'utf8' }).trim().split('\n')
  expect(execFileSync('unzip', ['-t', archivePath!], { encoding: 'utf8' })).toContain('No errors detected')
  const manifest = JSON.parse(execFileSync('unzip', ['-p', archivePath!, 'manifest.json'], { encoding: 'utf8' })) as { format: { name: string; version: string }; release_digest: string; motions: Array<{ motion_id: string; sheet: string; ordered_frames: number[]; duration_ms: number; loop: string; direction: string | null; event_markers: unknown[]; frame_geometry: { frame_count: number; frame_width: number; frame_height: number; sheet_width: number; sheet_height: number } }> }
  expect(manifest.format).toEqual({ name: 'png-json', version: '2' })
  expect(manifest.release_digest).toBe(release.release_digest)
  expect(manifest.motions.map((motion) => motion.motion_id)).toEqual(['idle', 'victory'])
  for (const motion of manifest.motions) {
    expect(archiveNames).toContain(motion.sheet)
    expect(motion.ordered_frames).toEqual(motion.motion_id === 'idle' ? [0, 3, 2] : [...Array(motion.frame_geometry.frame_count).keys()])
    expect(motion.duration_ms).toBeGreaterThan(0)
    expect(motion.loop).toBe('loop')
    expect(motion.frame_geometry.frame_width).toBeGreaterThan(0)
    expect(motion.frame_geometry.frame_height).toBeGreaterThan(0)
    expect(motion.frame_geometry.sheet_width).toBeGreaterThan(0)
    expect(motion.frame_geometry.sheet_height).toBeGreaterThan(0)
    expect(Array.isArray(motion.event_markers)).toBe(true)
  }
  expect(manifest.motions.find((motion) => motion.motion_id === 'victory')?.direction).toContain('confident upward')
  const archiveUrl = `/local-assets/exports/studio/${project.project_id}/${exported.export_id}.zip`
  const archiveBytes = await page.request.get(archiveUrl).then((response) => response.body())
  expect(createHash('sha256').update(archiveBytes).digest('hex')).toBe(exported.sha256)
  const repeated = await page.request.post(`/studio/releases/${release.release_id}/exports`, { data: { profile: 'png-json', expected_revision: release.source_revision } })
  expect(repeated.ok()).toBe(true)
  const repeatedExport = await repeated.json() as { export_id: string; release_id: string; profile_name: string; profile_version: string; sha256: string }
  expect(repeatedExport).toMatchObject({ export_id: exported.export_id, release_id: release.release_id, profile_name: 'png-json', profile_version: '2', sha256: exported.sha256 })
  const repeatedTarget = await page.request.get(`/studio/exports/${repeatedExport.export_id}/download`)
  expect(repeatedTarget.ok()).toBe(true)
  const repeatedUrl = (await repeatedTarget.json() as { upload_url: string }).upload_url
  const repeatedBytes = await page.request.get(repeatedUrl).then((response) => response.body())
  expect(Buffer.compare(archiveBytes, repeatedBytes)).toBe(0)

  // T22: choosing the Example Engine profile exports example/package.json alongside the same
  // manifest.json/motions PNGs the PNG + JSON profile produces, from the same release.
  await page.getByLabel('Export profile').selectOption('example')
  const targetExportResponsePromise = page.waitForResponse((response) => response.url().endsWith('/exports') && response.request().method() === 'POST')
  await page.getByRole('button', { name: 'Request export' }).click()
  const targetExportResponse = await targetExportResponsePromise
  expect(targetExportResponse.ok(), await targetExportResponse.text()).toBe(true)
  const targetExported = await targetExportResponse.json() as { export_id: string; profile_name: string }
  expect(targetExported.profile_name).toBe('example')
  const targetDownloadTarget = await page.request.get(`/studio/exports/${targetExported.export_id}/download`)
  expect(targetDownloadTarget.ok(), await targetDownloadTarget.text()).toBe(true)
  const targetDownloadPromise = page.waitForEvent('download')
  await page.getByRole('button', { name: 'Download Example Engine package' }).click()
  const targetArchivePath = await (await targetDownloadPromise).path()
  expect(targetArchivePath).not.toBeNull()
  const targetArchiveNames = execFileSync('unzip', ['-Z1', targetArchivePath!], { encoding: 'utf8' }).trim().split('\n')
  expect(targetArchiveNames).toEqual(expect.arrayContaining(['manifest.json', 'example/package.json']))
  const targetPackage = JSON.parse(execFileSync('unzip', ['-p', targetArchivePath!, 'example/package.json'], { encoding: 'utf8' })) as { format: { name: string; version: string }; schema_version: number; source_package_id: string; strips: Array<{ motion_id: string; slot: string | null; animation_type: string; sheet: string; columns: number; rows: number; loop_mode: string; flip: boolean }> }
  expect(targetPackage.format).toEqual({ name: 'example', version: '1' })
  expect(targetPackage.schema_version).toBe(1)
  expect(targetPackage.strips.map((strip) => strip.motion_id)).toEqual(['idle', 'victory'])
  for (const strip of targetPackage.strips) {
    expect(targetArchiveNames).toContain(strip.sheet)
    expect(strip.rows).toBe(1)
    expect(strip.columns).toBeGreaterThan(0)
  }
  // idle keeps the Example Engine slot it maps to; a motion that lands outside the
  // adapter's declared slots (e.g. a custom "victory") has none.
  expect(targetPackage.strips.find((strip) => strip.motion_id === 'idle')?.slot).toBe('idle')

  expect(apiCreateMs).toBeLessThan(API_CREATE_CEILING_MS)
  const responsivenessBudgets = JSON.stringify({
      ceilings_ms: {
        api_create: API_CREATE_CEILING_MS,
        cached_navigation: CACHED_NAVIGATION_CEILING_MS,
        deterministic_generation: GENERATION_CEILING_MS,
      },
      measured_ms: {
        api_create: apiCreateMs,
        cached_navigation: cachedNavigationMs,
        idle_generation: idleGenerationMs,
        victory_generation: victoryGenerationMs,
      },
      frame_draft_survived_navigation_and_reload: true,
    }, null, 2)
  const responsivenessBudgetPath = testInfo.outputPath('responsiveness-budgets.json')
  await writeFile(responsivenessBudgetPath, responsivenessBudgets)
  await testInfo.attach('responsiveness-budgets.json', {
    path: responsivenessBudgetPath,
    contentType: 'application/json',
  })
})

// T5/T6: the preset library and set generation -- adds and generates all six studio presets.
test('adds the recommended set and generates it as one batch', async ({ page }, testInfo) => {
  const createResponse = Promise.resolve(await createLegacyProject(page, 'Set Test'))
  const project = await (await createResponse).json() as { project_id: string }

  await page.goto(`/projects/${project.project_id}/subject`)
  const jpegReference = testInfo.outputPath('subject-reference-set.jpg')
  execFileSync('ffmpeg', ['-y', '-i', subjectPng, '-pix_fmt', 'yuvj420p', jpegReference], { stdio: 'ignore' })
  await page.getByLabel('Choose subject references').setInputFiles(jpegReference)
  await page.getByLabel(/Guidance/).fill('Preserve the warm palette and readable silhouette.')
  await page.getByRole('button', { name: 'Generate subject' }).click()
  await expect(page.getByRole('heading', { name: 'Ready for approval' })).toBeVisible({ timeout: READY_TIMEOUT_MS })
  await page.getByRole('button', { name: 'Approve subject' }).click()
  await expect(page.getByRole('heading', { name: 'Build the animation set' })).toBeVisible()

  // The empty-state CTA adds all six recommended presets in one batch request (T6).
  await page.getByRole('button', { name: 'Add the recommended set' }).click()
  await expect(page.getByRole('option', { name: /Idle To generate/ })).toBeVisible()
  await expect(page.getByRole('option', { name: /Walk To generate/ })).toBeVisible()
  await expect(page.getByRole('option', { name: /Attack To generate/ })).toBeVisible()
  await expect(page.getByRole('option', { name: /Guard To generate/ })).toBeVisible()
  await expect(page.getByRole('option', { name: /Cast To generate/ })).toBeVisible()
  await expect(page.getByRole('option', { name: /Hurt To generate/ })).toBeVisible()

  // Set-first generation (studio-ux-flow.md section 4): every added motion is pre-checked, and
  // "Generate N motions" confirms through one sheet that asks what each motion does -- the
  // opening pose that binds the set together is prepared automatically, with no compose/approve
  // step in the UI.
  await expect(page.getByRole('checkbox', { name: 'Include Idle in the next batch' })).toBeChecked()
  await expect(page.getByRole('checkbox', { name: 'Include Hurt in the next batch' })).toBeChecked()
  await page.getByRole('button', { name: 'Generate 6 motions' }).click()
  const batchSheet = page.getByRole('dialog', { name: 'What happens in each motion' })
  await expect(batchSheet.getByRole('textbox', { name: 'What happens in Attack' })).toHaveValue('swings a weapon')
  await batchSheet.getByRole('textbox', { name: 'What happens in Attack' }).fill('draws and fires the bow')
  await batchSheet.getByRole('button', { name: 'Generate' }).click()

  // All six settle without the Activity panel ever opening (parity with T1's settlement fix).
  await expect(page.getByRole('dialog', { name: 'GPU status' })).toHaveCount(0)
  await expect(page.getByRole('option', { name: /Idle (Review|Selected)/ })).toBeVisible({ timeout: 30_000 })
  await expect(page.getByRole('option', { name: /Walk (Review|Selected)/ })).toBeVisible({ timeout: 30_000 })
  await expect(page.getByRole('option', { name: /Attack (Review|Selected)/ })).toBeVisible({ timeout: 30_000 })
  await expect(page.getByRole('option', { name: /Guard (Review|Selected)/ })).toBeVisible({ timeout: 30_000 })
  await expect(page.getByRole('option', { name: /Cast (Review|Selected)/ })).toBeVisible({ timeout: 30_000 })
  await expect(page.getByRole('option', { name: /Hurt (Review|Selected)/ })).toBeVisible({ timeout: 30_000 })
  await expect(page.getByRole('dialog', { name: 'GPU status' })).toHaveCount(0)
})

// studio-ux-flow.md section 4: a lone custom motion needs no composition gate at all any more --
// the opening pose is an internal detail the studio prepares automatically, so a single custom
// motion is ready for "Generate 1 motion" the moment it is added.
test('a solo custom motion is ready to generate immediately, with no shared-pose step', async ({ page }, testInfo) => {
  const createResponse = Promise.resolve(await createLegacyProject(page, 'Solo Custom'))
  const project = await (await createResponse).json() as { project_id: string }

  await page.goto(`/projects/${project.project_id}/subject`)
  const jpegReference = testInfo.outputPath('subject-reference-solo.jpg')
  execFileSync('ffmpeg', ['-y', '-i', subjectPng, '-pix_fmt', 'yuvj420p', jpegReference], { stdio: 'ignore' })
  await page.getByLabel('Choose subject references').setInputFiles(jpegReference)
  await page.getByLabel(/Guidance/).fill('Preserve the warm palette and readable silhouette.')
  await page.getByRole('button', { name: 'Generate subject' }).click()
  await expect(page.getByRole('heading', { name: 'Ready for approval' })).toBeVisible({ timeout: READY_TIMEOUT_MS })
  await page.getByRole('button', { name: 'Approve subject' }).click()
  await expect(page.getByRole('heading', { name: 'Build the animation set' })).toBeVisible()

  await page.getByRole('button', { name: 'Add motion' }).click()
  await page.getByRole('button', { name: 'Custom…' }).click()
  await page.getByLabel('Motion name').fill('Taunt')
  await page.getByLabel('What happens', { exact: true }).fill('A quick, confident taunt with a clean return to idle.')
  await page.getByRole('button', { name: 'Create motion' }).click()

  // No compose/approve step anywhere on the page -- pre-checked and ready to go.
  await expect(page.getByRole('button', { name: /Compose opening frame/ })).toHaveCount(0)
  await expect(page.getByRole('checkbox', { name: 'Include Taunt in the next batch' })).toBeChecked()
  await expect(page.getByRole('button', { name: 'Generate 1 motion' })).toBeVisible()
})

// T18: the local deterministic simulator's candidates already carry alpha, so the frame
// editor's "Remove green backdrop" button stays hidden -- and the derive-alpha route,
// called directly, refuses a source that already has alpha rather than silently no-opping.
test('a fully alpha candidate hides the alpha fix and the route rejects re-deriving it', async ({ page }, testInfo) => {
  const createResponse = Promise.resolve(await createLegacyProject(page, 'Alpha Check'))
  const project = await (await createResponse).json() as { project_id: string }

  await page.goto(`/projects/${project.project_id}/subject`)
  const jpegReference = testInfo.outputPath('subject-reference-alpha.jpg')
  execFileSync('ffmpeg', ['-y', '-i', subjectPng, '-pix_fmt', 'yuvj420p', jpegReference], { stdio: 'ignore' })
  await page.getByLabel('Choose subject references').setInputFiles(jpegReference)
  await page.getByLabel(/Guidance/).fill('Preserve the warm palette and readable silhouette.')
  await page.getByRole('button', { name: 'Generate subject' }).click()
  await expect(page.getByRole('heading', { name: 'Ready for approval' })).toBeVisible({ timeout: READY_TIMEOUT_MS })
  await page.getByRole('button', { name: 'Approve subject' }).click()
  await expect(page.getByRole('heading', { name: 'Build the animation set' })).toBeVisible()

  await page.getByRole('button', { name: 'Add motion' }).click()
  await page.getByRole('button', { name: 'Add Idle' }).click()
  await page.getByRole('button', { name: 'Generate Idle' }).click()
  await expect(page.getByRole('listitem', { name: /Take/ })).toBeVisible({ timeout: READY_TIMEOUT_MS })
  await page.getByRole('listitem', { name: /Take/ }).getByRole('button', { name: 'Use animation' }).click()
  await page.getByRole('listitem', { name: /Take/ }).getByRole('button', { name: 'Edit frames' }).click()
  await expect(page.getByRole('region', { name: 'Idle frame editor' })).toBeVisible()
  await expect(page.getByRole('button', { name: /Remove green backdrop/ })).toHaveCount(0)

  const projectState = await (await page.request.get(`/studio/projects/${project.project_id}`, {
    headers: { 'X-Studio-Actor-Id': 'local-e2e-user' },
  })).json() as { revision: number; manifest: { motions: { candidates: { candidate_id: string }[] }[] } }
  const candidateId = projectState.manifest.motions[0].candidates[0].candidate_id
  const deriveResponse = await page.request.post(
    `/studio/projects/${project.project_id}/motions/idle/candidates/${candidateId}/derive-alpha`,
    { headers: { 'X-Studio-Actor-Id': 'local-e2e-user' }, data: { expected_revision: projectState.revision } },
  )
  expect(deriveResponse.status()).toBe(422)
  expect((await deriveResponse.json()).detail.code).toBe('motion_already_has_alpha')
})

// T21: ingest a PNG strip produced elsewhere (Ludo, AutoSprite, an artist) as a
// candidate, without ever generating one for this motion first.
test('uploads an existing PNG as a candidate through the ingest form', async ({ page }) => {
  const createResponse = Promise.resolve(await createLegacyProject(page, 'Ingest Check'))
  const project = await (await createResponse).json() as { project_id: string }

  await page.goto(`/projects/${project.project_id}/subject`)
  await page.getByLabel('Choose subject references').setInputFiles(subjectPng)
  await page.getByLabel(/Guidance/).fill('Preserve the warm palette and readable silhouette.')
  await page.getByRole('button', { name: 'Generate subject' }).click()
  await expect(page.getByRole('heading', { name: 'Ready for approval' })).toBeVisible({ timeout: READY_TIMEOUT_MS })
  await page.getByRole('button', { name: 'Approve subject' }).click()
  await expect(page.getByRole('heading', { name: 'Build the animation set' })).toBeVisible()

  await page.getByRole('button', { name: 'Add motion' }).click()
  await page.getByRole('button', { name: 'Add Idle' }).click()
  await page.getByRole('button', { name: 'or upload a clip you already have' }).click()

  const uploadInput = page.getByLabel('Upload existing animation')
  await uploadInput.setInputFiles(subjectPng)
  await expect(page.getByLabel('Uploaded frame count')).toBeVisible()
  await page.getByLabel('Uploaded frame count').fill('1')
  const settleResponse = page.waitForResponse((response) => response.url().endsWith('/candidates/settle-upload') && response.request().method() === 'POST')
  await page.getByRole('button', { name: 'Add as candidate' }).click()
  expect((await settleResponse).ok()).toBe(true)
  await expect(page.getByRole('listitem', { name: /Take/ })).toBeVisible({ timeout: READY_TIMEOUT_MS })
})

test('lists a created project on the project browser after a reload', async ({ page }) => {
  const createResponse = Promise.resolve(await createLegacyProject(page, 'Home Screen Check'))
  await createResponse

  await page.goto('/projects')

  await expect(page.getByRole('heading', { name: 'Your projects' })).toBeVisible()
  const link = page.getByRole('link', { name: /Home Screen Check/ })
  await expect(link).toBeVisible()
  await expect(link).toHaveAttribute('href', /\/subject$/)
})
