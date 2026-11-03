import { execFileSync } from 'node:child_process'
import { mkdir } from 'node:fs/promises'
import { expect, test } from '@playwright/test'

function tone(path: string, frequency: number) {
  execFileSync('ffmpeg', ['-v', 'error', '-y', '-f', 'lavfi', '-i', `sine=frequency=${frequency}:duration=1.5`, '-ar', '44100', '-c:a', 'pcm_s16le', path])
  return path
}

test('gathers, reviews, ships and releases an audio set', async ({ page }, testInfo) => {
  test.setTimeout(120_000)
  const proof = testInfo.outputPath('proof')
  await mkdir(proof, { recursive: true })
  const existing = await page.request.post('/studio/projects', { data: { name: 'Audio flow existing project' } })
  expect(existing.ok()).toBeTruthy()
  await page.goto('/')
  const audioOption = page.getByRole('button', { name: 'Gather audio instead' })
  const newProject = page.getByRole('button', { name: 'New project', exact: true })
  await expect(newProject.or(audioOption)).toBeVisible()
  if (await newProject.isVisible()) await newProject.click()
  await audioOption.click()
  await page.getByLabel('Project name').fill('Glass and doors')
  await page.getByRole('button', { name: 'Create project' }).click()
  await expect(page.getByRole('heading', { name: 'Cues' })).toBeVisible()
  // The local studio has no Freesound token, so search says so and uploads still work.
  await expect(page.getByText(/Free-sound search is not set up/)).toBeVisible()

  await page.getByRole('button', { name: 'Add a cue' }).click()
  await page.getByLabel('Cue 1 name').fill('Glass crack')
  await page.getByLabel('Cue 1 description').fill('glass crack, short, dry')
  await page.getByRole('button', { name: 'Save cues' }).click()
  const board = page.getByRole('region', { name: 'Glass crack candidates' })
  await expect(board).toBeVisible()

  await board.getByLabel('Upload a file for Glass crack').setInputFiles(tone(testInfo.outputPath('crack-a.wav'), 660))
  const first = board.getByRole('listitem', { name: 'Glass crack candidate 1' })
  await expect(first).toContainText('LUFS', { timeout: 30_000 })
  await expect(first).toContainText('Your upload')
  await board.getByLabel('Upload a file for Glass crack').setInputFiles(tone(testInfo.outputPath('crack-b.wav'), 990))
  const second = board.getByRole('listitem', { name: 'Glass crack candidate 2' })
  await expect(second).toBeVisible({ timeout: 30_000 })

  await second.getByLabel('Note for Glass crack candidate 2').fill('too bright')
  await second.getByRole('button', { name: 'Reject' }).click()
  await expect(second.getByText('Rejected', { exact: true })).toBeVisible()
  await expect(second.getByRole('checkbox', { name: /Ship/ })).toBeDisabled()
  await first.getByRole('button', { name: 'Keep' }).click()
  await expect(first.getByText('Kept', { exact: true })).toBeVisible()
  // The box is the saved selection: it turns on once the server accepts it.
  await first.getByRole('checkbox', { name: /Ship/ }).click()
  await expect(first.getByText('Ship as variant 1')).toBeVisible()
  await expect(first.getByRole('checkbox', { name: /Ship/ })).toBeChecked()
  await page.screenshot({ path: `${proof}/01-board.png`, fullPage: true })

  await page.getByLabel('Release name').fill('v1')
  await page.getByRole('button', { name: 'Release audio' }).click()
  const link = page.getByRole('link', { name: 'Download v1' })
  await expect(link).toBeVisible()
  const download = await page.request.get(await link.getAttribute('href') ?? '')
  expect(download.ok()).toBeTruthy()
  const zip = await download.body()
  expect(zip.subarray(0, 2).toString()).toBe('PK')
  for (const name of ['manifest.json', 'CREDITS.md', 'audio/', 'originals/']) expect(zip.includes(Buffer.from(name))).toBeTruthy()
  await page.screenshot({ path: `${proof}/02-released.png`, fullPage: true })

  await page.goto('/projects')
  await expect(page.getByRole('link', { name: /Glass and doors.*Audio · 1 cue · 1 release/ })).toBeVisible()
})
