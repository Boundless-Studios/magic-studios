import { execFileSync } from 'node:child_process'
import { expect, test } from '@playwright/test'

import { subjectPng } from './subjectImage'

// FABLE-TABLE deep-link + delivery slice: a character tagged in a target game flows into the
// studio (Part A) and its release flows back out to that target automatically (Part B), with no
// manual binding step. Exercised here against the in-repo Example Engine reference adapter.
test('deep-links into a bound project and delivers its release to the fake target importer', async ({ page }, testInfo) => {
  const entityId = `char-e2e-${testInfo.workerIndex}`

  // Part A: the deep link creates a project already bound to the target's character, with the
  // requested slot's preset motion added, and opens Motions with it selected.
  await page.goto(
    `/new?target=example&entity_kind=character&entity_id=${entityId}&name=Ember%20Knight&slot=idle`,
  )
  await expect(page).toHaveURL(/\/projects\/[^/]+\/motions/, { timeout: 15_000 })
  const projectId = new URL(page.url()).pathname.split('/')[2]
  await expect(page.getByText('Bound to Example Engine character Ember Knight')).toBeVisible()
  await expect(page.getByRole('option', { name: /Idle/, selected: true })).toBeVisible()

  // A second deep link for the same subject must open the same project rather than duplicate it.
  await page.goto(
    `/new?target=example&entity_kind=character&entity_id=${entityId}&name=Ember%20Knight`,
  )
  await expect(page).toHaveURL(new RegExp(`/projects/${projectId}/motions`), { timeout: 15_000 })

  // Generate and approve the subject so the deep-linked Idle motion can be generated.
  await page.goto(`/projects/${projectId}/subject`)
  const jpegReference = testInfo.outputPath('subject-reference-target.jpg')
  execFileSync('ffmpeg', ['-y', '-i', subjectPng, '-pix_fmt', 'yuvj420p', jpegReference], { stdio: 'ignore' })
  await page.getByLabel('Choose subject references').setInputFiles(jpegReference)
  await page.getByLabel(/Guidance/).fill('Preserve the warm palette and readable silhouette.')
  await page.getByRole('button', { name: 'Generate subject' }).click()
  await expect(page.getByRole('heading', { name: 'Ready for approval' })).toBeVisible({ timeout: 10_000 })
  await page.getByRole('button', { name: 'Approve subject' }).click()
  await expect(page.getByRole('heading', { name: 'Build the animation set' })).toBeVisible()

  // The deep-linked Idle motion is already present -- generate and select it.
  await expect(page.getByRole('option', { name: /Idle/ })).toBeVisible()
  await page.getByRole('option', { name: /Idle/ }).click()
  await page.getByRole('button', { name: 'Generate Idle' }).click()
  await expect(page.getByRole('listitem', { name: /Take/ })).toBeVisible({ timeout: 10_000 })
  await page.getByRole('listitem', { name: /Take/ }).getByRole('button', { name: 'Use animation' }).click()

  await page.getByRole('link', { name: 'Release & export', exact: true }).click()
  await page.getByRole('button', { name: 'Run quality checks' }).click()
  await expect(page.getByText('No set findings.', { exact: true })).toBeVisible()

  // Part B: releasing exports the target's profile and delivers it to the (fake, local) importer
  // automatically, and the studio surfaces the delivery outcome.
  await page.getByRole('link', { name: 'Release & export' }).click()
  await page.getByRole('button', { name: 'Approve animation set' }).click()
  await page.getByLabel('Release name').fill('target-autobind-release')
  const releaseResponsePromise = page.waitForResponse((response) =>
    response.url().endsWith('/releases') && response.request().method() === 'POST'
  )
  await page.getByRole('button', { name: 'Create immutable release' }).click()
  const release = await (await releaseResponsePromise).json() as {
    release_id: string
    deliveries: { target: string; status: string }[]
  }
  await expect(page.getByLabel('Export profile')).toHaveValue('example')
  expect(release.deliveries.at(-1)?.status).toBe('delivered')
  await expect(page.getByText(/Delivered to Example Engine: \d+ motions, \d+ bindings/)).toBeVisible()

  // Ground truth: the fake importer actually received this release's target package.
  const deliveries = await page.request.get('/local-target-deliveries/example').then((response) => response.json()) as
    Array<{ release_id: string; package: { strips: Array<{ motion_id: string; slot: string | null }> }; bindings: Array<{ entity_id: string; slot: string }> }>
  const delivery = deliveries.find((item) => item.release_id === release.release_id)
  expect(delivery).toBeDefined()
  expect(delivery!.package.strips.map((strip) => strip.motion_id)).toEqual(['idle'])
  expect(delivery!.package.strips[0].slot).toBe('idle')
  expect(delivery!.bindings).toEqual([{ entity_kind: 'character', entity_id: entityId, slot: 'idle', overrides: expect.any(Object) }])
})
