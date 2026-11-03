import { execFileSync } from 'node:child_process'
import { mkdir } from 'node:fs/promises'
import { expect, test } from '@playwright/test'

function plate(path: string, color: string) {
  execFileSync('ffmpeg', ['-v', 'error', '-y', '-f', 'lavfi', '-i', `color=c=${color}:s=1536x864`, '-frames:v', '1', path])
  return path
}

test('plans, generates, re-rolls, selects, downloads and chains a scene flight', async ({ page }, testInfo) => {
  test.setTimeout(120_000)
  const proof = testInfo.outputPath('proof')
  await mkdir(proof, { recursive: true })
  const existing = await page.request.post('/studio/projects', { data: { name: 'Flight flow existing project' } })
  expect(existing.ok()).toBeTruthy()
  await page.goto('/')
  const flightOption = page.getByRole('button', { name: 'Make a scene flight instead' })
  const newProject = page.getByRole('button', { name: 'New project', exact: true })
  await expect(newProject.or(flightOption)).toBeVisible()
  if (await newProject.isVisible()) await newProject.click()
  await flightOption.click()
  await page.getByLabel('Project name').fill('Harrowfen to Miretrace')
  await page.getByRole('button', { name: 'Create project' }).click()
  await expect(page.getByRole('heading', { name: 'Location images' })).toBeVisible()

  await page.getByLabel('Add location images').setInputFiles([
    plate(testInfo.outputPath('harrowfen.png'), '0x2f5a3a'), plate(testInfo.outputPath('miretrace.png'), '0xb8bcc4'),
  ])
  await expect(page.getByRole('img', { name: 'Location 2' })).toBeVisible()
  await page.getByRole('button', { name: 'Add a leg' }).click()
  await page.getByLabel('Origin place name').fill('Harrowfen')
  await page.getByLabel('Destination place name').fill('Miretrace')
  await page.getByLabel('Avoid').fill('fog banks')
  await page.getByRole('button', { name: 'Save legs' }).click()
  const takes = page.getByRole('region', { name: 'Harrowfen to Miretrace takes' })
  await expect(takes).toBeVisible()
  await page.screenshot({ path: `${proof}/01-planned.png`, fullPage: true })

  await takes.getByRole('button', { name: 'Generate this leg' }).click()
  await expect(takes.getByLabel('Harrowfen to Miretrace selected take')).toBeVisible({ timeout: 30_000 })
  await expect(takes.getByText(/First frame vs origin: \d/).first()).toBeVisible()
  await takes.getByRole('button', { name: 'Re-roll this leg' }).click()
  await expect(takes.getByRole('listitem')).toHaveCount(2)
  await expect(takes.getByRole('listitem').nth(1).getByRole('link', { name: 'Download' })).toBeVisible({ timeout: 30_000 })
  await takes.getByRole('listitem').first().getByRole('button', { name: 'Use this take' }).click()
  await expect(takes.getByRole('listitem').first().getByText('Selected', { exact: true })).toBeVisible()

  const download = await page.request.get(await takes.getByRole('listitem').first().getByRole('link', { name: 'Download' }).getAttribute('href') ?? '')
  expect(download.headers()['content-type']).toBe('video/mp4')
  expect((await download.body()).byteLength).toBeGreaterThan(1000)

  await page.getByRole('button', { name: 'Chain flight' }).click()
  await expect(page.getByRole('link', { name: 'Download chained flight' })).toBeVisible({ timeout: 30_000 })
  await page.screenshot({ path: `${proof}/02-chained.png`, fullPage: true })
})
