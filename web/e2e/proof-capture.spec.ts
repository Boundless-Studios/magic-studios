import { execFileSync } from 'node:child_process'
import { mkdirSync } from 'node:fs'
import * as path from 'node:path'

import { test, expect, type Page } from '@playwright/test'

import { subjectPng } from './subjectImage'

// Session-only proof capture: walks the restored capabilities on the local stack and
// photographs each one. Not committed; the HTML report is built from OUT afterwards.
const OUT = process.env.PROOF_OUT || '/tmp/studio-proof'
mkdirSync(OUT, { recursive: true })
let shot = 0
async function snap(page: Page, name: string) {
  shot += 1
  await page.waitForTimeout(400)
  await page.screenshot({ path: path.join(OUT, `${String(shot).padStart(2, '0')}-${name}.jpg`), fullPage: false, type: 'jpeg', quality: 62 })
}

test('proof capture of the parity build', async ({ page }, testInfo) => {
  test.setTimeout(480_000)
  await page.setViewportSize({ width: 1440, height: 900 })
  // Historical parity proof uses a legacy unassigned project.
  const creation = await page.request.post('/studio/projects', { data: { name: 'Ember Knight' } })
  expect(creation.ok()).toBe(true)
  const project = await creation.json() as { project_id: string }

  await page.goto(`/projects/${project.project_id}/subject`)
  const jpeg = testInfo.outputPath('subject-reference.jpg')
  execFileSync('ffmpeg', ['-y', '-i', subjectPng, '-pix_fmt', 'yuvj420p', jpeg], { stdio: 'ignore' })
  await page.getByLabel('Choose subject references').setInputFiles(jpeg)
  await page.getByLabel(/Direction/).fill('Preserve the warm palette and readable silhouette.')
  await page.getByRole('button', { name: 'Generate subject' }).click()
  await expect(page.getByRole('heading', { name: 'Ready for approval' })).toBeVisible({ timeout: 15_000 })
  await snap(page, 'subject-generated-and-measured')
  await page.getByRole('button', { name: 'Approve subject' }).click()
  await expect(page.getByRole('heading', { name: 'Build the animation set' })).toBeVisible()

  await page.getByRole('button', { name: 'Add motion' }).click()
  await expect(page.getByRole('button', { name: 'Add Walk' })).toBeVisible()
  await snap(page, 'preset-library-grid')
  await page.reload()
  await page.getByRole('button', { name: 'Add the recommended set' }).click()
  await expect(page.getByRole('option', { name: /Hurt To generate/ })).toBeVisible()
  await snap(page, 'recommended-set-added')

  await snap(page, 'set-first-rail-ready-to-generate')
  await page.getByRole('button', { name: 'Generate 6 motions' }).click()
  await expect(page.getByRole('button', { name: /GPU status, [1-6] running/ })).toBeVisible({ timeout: 10_000 })
  await page.getByRole('button', { name: /GPU status, [1-6] running/ }).click()
  await expect(page.getByRole('heading', { name: 'GPU status' })).toBeVisible()
  await snap(page, 'activity-panel-batch-running-with-cancel')
  await page.getByRole('button', { name: 'Close activity' }).click()
  for (const name of ['Idle', 'Walk', 'Attack', 'Guard', 'Cast', 'Hurt']) {
    await expect(page.getByRole('option', { name: new RegExp(`${name} (Review|Selected)`) })).toBeVisible({ timeout: 40_000 })
  }
  await snap(page, 'six-motions-generated-from-shared-pose')

  await page.getByRole('option', { name: /Idle Review/ }).click()
  const card = page.getByRole('listitem', { name: /Take/ }).first()
  await card.getByRole('button', { name: 'Download' }).click()
  await expect(card.getByRole('button', { name: 'GIF' })).toBeVisible()
  await snap(page, 'candidate-download-menu-png-webm-gif')
  await page.keyboard.press('Escape')
  // Every required motion needs a pinned candidate before the set can be approved.
  for (const name of ['Walk', 'Attack', 'Guard', 'Cast', 'Hurt']) {
    await page.getByRole('option', { name: new RegExp(`${name} Review`) }).click()
    await page.getByRole('listitem', { name: /Take/ }).first().getByRole('button', { name: 'Use animation' }).click()
    await expect(page.getByRole('option', { name: new RegExp(`${name} Selected`) })).toBeVisible()
  }
  await page.getByRole('option', { name: /Idle Review/ }).click()
  await card.getByRole('button', { name: 'Use animation' }).click()
  await card.getByRole('button', { name: 'Edit frames' }).click()
  await expect(page.getByRole('region', { name: 'Idle frame editor' })).toBeVisible()
  await page.getByRole('button', { name: 'Exclude frame 2' }).click()
  await page.getByText('More frame controls', { exact: true }).click()
  await page.getByLabel('Current frame', { exact: true }).fill('3')
  await page.getByRole('button', { name: 'Move frame 4 earlier' }).click()
  await page.getByLabel('Frames per second').fill('12')
  await page.getByLabel('Flip horizontally').check()
  await page.getByLabel('Current frame', { exact: true }).fill('0')
  await page.getByLabel('Hold for frame 1').fill('3')
  await page.getByLabel('Frame count').selectOption('16')
  await page.getByLabel('Cell size').selectOption('512')
  await snap(page, 'frame-editor-reorder-flip-hold-fps-packing-draft')
  // Resampling one motion alone would (correctly) fail set coherence at signoff:
  // canvas mismatch and texture limit. Keep the source geometry for the walkthrough.
  await page.getByLabel('Frame count').selectOption('')
  await page.getByRole('button', { name: 'Save revision' }).click()
  await expect(page.getByText(/Saved · /)).toBeVisible({ timeout: 15_000 })
  await snap(page, 'frame-revision-saved')

  await page.getByRole('button', { name: 'Expand' }).click()
  await page.getByRole('button', { name: 'Advanced generation settings' }).click()
  await expect(page.getByLabel('Steps')).toBeVisible()
  await snap(page, 'advanced-generation-settings-references-style-script')

  await page.getByRole('link', { name: 'Quality' }).click()
  await page.getByRole('button', { name: 'Run quality checks' }).click()
  await expect(page.getByText('No items in this group.').first()).toBeVisible({ timeout: 15_000 })
  await snap(page, 'quality-inbox-after-checks')

  await page.getByRole('link', { name: 'Release & export' }).click()
  await snap(page, 'release-before-approval')
  const approveResponse = page.waitForResponse((r) => r.url().endsWith('/set/approve') && r.request().method() === 'POST')
  await page.getByRole('button', { name: 'Approve animation set' }).click()
  const approved = await approveResponse
  console.log('APPROVE', approved.status(), (await approved.text()).slice(0, 400))
  await snap(page, 'release-after-approval')
  await page.getByLabel('Release name').fill('first-playable')
  const createRelease = page.getByRole('button', { name: 'Create immutable release' })
  await expect(createRelease).not.toHaveAttribute('aria-disabled', 'true', { timeout: 15_000 })
  await createRelease.click()
  const profile = page.getByLabel(/profile/i)
  if (await profile.count()) await profile.first().selectOption('gaia')
  await page.getByRole('button', { name: 'Request export' }).click()
  await expect(page.getByRole('link', { name: /Download/ })).toBeVisible({ timeout: 20_000 })
  await snap(page, 'release-and-gaia-export')

  await page.getByRole('link', { name: 'History' }).click()
  await page.waitForURL(/\/history$/)
  await page.waitForTimeout(1500)
  await snap(page, 'history-revisions')
  const operationsTab = page.getByRole('tab', { name: /Operations/ })
  if (await operationsTab.count()) { await operationsTab.click(); await snap(page, 'history-operations-with-settings') }
})
