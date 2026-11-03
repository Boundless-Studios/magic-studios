import { expect, test } from '@playwright/test'

import { subjectPng } from './subjectImage'

test('uploads a starting reference in Story and keeps it as the first opening frame', async ({ page }, testInfo) => {
  await page.goto('/projects/new')
  await page.getByRole('button', { name: 'Make a movie instead' }).click()
  await page.getByLabel('Project name').fill('Image-led movie')
  await page.getByRole('button', { name: 'Create project' }).click()
  await page.getByLabel('Upload starting reference image').setInputFiles(subjectPng)
  await expect(page.getByAltText('Starting reference for the first shot')).toBeVisible()
  await page.getByLabel('Action & camera movement').fill('The character turns toward the light. Slow camera push in.')
  await page.getByRole('button', { name: 'Save draft', exact: true }).click()
  await expect(page.getByText('Movie saved.')).toBeVisible()
  await page.reload()
  await page.getByText('Reference images · upload or replace', { exact: true }).click()
  await expect(page.getByAltText('Starting reference for the first shot')).toBeVisible()
  await page.screenshot({ path: testInfo.outputPath('starting-reference.png'), fullPage: true })
  await expect(page.getByRole('button', { name: 'Approve opening frame' })).toHaveCount(0)
  await page.getByRole('button', { name: 'Generate this shot', exact: true }).click()
  await expect(page.getByLabel('Shot 1 preview', { exact: true })).toBeVisible({ timeout: 20_000 })
  const movieId = page.url().split('/movies/')[1].split('/')[0]
  const first = await (await page.request.get(`/studio/movies/${movieId}`)).json()
  expect(first.shots[0].selected_take_id).toBe(first.shots[0].takes[0].take_id)
  await page.getByLabel('Action & camera movement').fill('The camera pulls back slowly.')
  await page.getByRole('button', { name: 'Generate another take', exact: true }).click()
  await expect(page.getByRole('button', { name: 'Preview take 2', exact: true })).toBeVisible({ timeout: 20_000 })
  const refined = await (await page.request.get(`/studio/movies/${movieId}`)).json()
  expect(refined.shots[0].takes).toHaveLength(2)
  expect(refined.shots[0].selected_take_id).toBe(refined.shots[0].takes[1].take_id)
  expect(refined.shots[0].opening_asset_key).toBe(first.shots[0].opening_asset_key)
  expect(refined.shots[0].takes[1].opening_asset_key).toBe(first.shots[0].opening_asset_key)


})

test('saves an ordered image sequence as separate shot openings', async ({ page }) => {
  await page.goto('/projects/new')
  await page.getByRole('button', { name: 'Make a movie instead' }).click()
  await page.getByLabel('Project name').fill('Image sequence')
  await page.getByRole('button', { name: 'Create project' }).click()
  const { readFile } = await import('node:fs/promises')
  const buffer = await readFile(subjectPng)
  await page.getByLabel('Add image sequence').setInputFiles([
    { name: 'Arrival.png', mimeType: 'image/png', buffer },
    { name: 'Departure.png', mimeType: 'image/png', buffer },
  ])
  await expect(page.getByAltText('Arrival reference')).toBeVisible()
  await page.getByRole('button', { name: 'Arrange Departure, shot 2' }).focus()
  await page.keyboard.press('ArrowUp')
  await page.getByRole('button', { name: 'Arrange Departure, shot 1' }).click()
  await expect(page.getByAltText('Departure reference')).toBeVisible()
  await expect(page.getByLabel('Shot name').first()).toHaveValue('Departure')
  for (const name of ['Arrange Departure, shot 1', 'Arrange Arrival, shot 2']) {
    await page.getByRole('button', { name }).click()
    await page.getByLabel('Action & camera movement').fill('Slow camera push in.')
  }
  await page.getByRole('button', { name: 'Save draft', exact: true }).click()
  await expect(page.getByText('Movie saved.')).toBeVisible()
  await page.reload()
  await expect(page.getByLabel('Shot name').first()).toHaveValue('Departure')
  await expect(page.getByAltText('Departure reference')).toBeVisible()
  await page.getByRole('button', { name: 'Arrange Arrival, shot 2' }).click()
  await expect(page.getByAltText('Arrival reference')).toBeVisible()
  await page.getByRole('button', { name: 'Generate movie', exact: true }).click()
  await expect(page.getByRole('link', { name: 'Review movie →' })).toBeVisible({ timeout: 40_000 })
  await expect(page.getByLabel('Arrival preview', { exact: true })).toBeVisible()

})
