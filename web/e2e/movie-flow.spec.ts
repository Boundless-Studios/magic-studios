import { execFileSync } from 'node:child_process'
import { mkdir, readFile, stat } from 'node:fs/promises'
import { expect, test } from '@playwright/test'

import { subjectPng } from './subjectImage'

test('makes a movie with independent shots, a continuation, trims and narration', async ({ page }, testInfo) => {
  test.setTimeout(120_000)
  const proof = testInfo.outputPath('proof')
  await mkdir(proof, { recursive: true })
  // Returning accounts and other parallel flows may already own projects.
  const existing = await page.request.post('/studio/projects', { data: { name: 'Movie flow existing project' } })
  expect(existing.ok()).toBeTruthy()
  await page.goto('/')
  const movieOption = page.getByRole('button', { name: 'Make a movie instead' })
  const newProject = page.getByRole('button', { name: 'New project', exact: true })
  await expect(newProject.or(movieOption)).toBeVisible()
  if (await newProject.isVisible()) await newProject.click()
  await movieOption.click()
  await page.getByLabel('Project name').fill('The last light')
  await page.getByRole('button', { name: 'Create project' }).click()
  await expect(page.getByRole('heading', { name: 'Movie', exact: true })).toBeVisible()
  const movieId = page.url().split('/movies/')[1].split('/')[0]
  await page.getByLabel('Movie brief').fill('A traveller arrives at an abandoned city. A distant beacon answers. The traveller steps into the light.')
  await page.getByLabel('Visual direction').fill('Ash-grey stone, warm amber light, a restrained cinematic camera.')
  await page.getByLabel('Clips per batch').fill('2')
  for (const [name, action, opening] of [
    ['Arrival', 'The traveller takes one slow step toward the city.', 'A lone traveller stands outside an abandoned city at dusk.'],
    ['The beacon', 'The distant beacon slowly brightens.', 'A dark tower above a still sea of ash.'],
    ['Into the light', 'The traveller steps through the city gate.', 'Continue from the traveller outside the gate.'],
  ]) {
    await page.getByRole('button', { name: '+ Add shot' }).click()
    const fieldset = page.locator('.movie-shot-definition').last()
    await fieldset.getByLabel('Shot name').fill(name)
    await fieldset.getByLabel('Action & camera movement').fill(action)
    await fieldset.getByLabel('Opening frame direction').fill(opening)
  }
  await page.locator('.movie-shot-definition').last().getByLabel('Continuity').selectOption({ label: 'Continue from Arrival' })
  await page.getByRole('button', { name: 'Save draft' }).click()
  await expect(page.getByText('Movie saved.')).toBeVisible()
  await page.locator('#main-content').evaluate((element) => { element.scrollTop = 0 })
  await page.screenshot({ path: `${proof}/01-story.png`, fullPage: true })
  for (const name of ['Arrival','The beacon']) {
    await page.getByRole('button', { name: new RegExp(`^Arrange ${name},`) }).click()
    await page.getByText('Starting image & existing clips', { exact: true }).click()
    await page.getByLabel('Upload starting image').setInputFiles(subjectPng)
  }
  await expect(page.getByRole('button', { name: 'Generate movie', exact: true })).toBeInViewport()
  const dispatched = page.waitForResponse((response) => response.url().endsWith('/build') && response.request().method() === 'POST')
  await page.getByRole('button', { name: 'Generate movie', exact: true }).click()
  expect((await dispatched).ok()).toBeTruthy()
  await page.reload()
  await expect(page.getByRole('link', { name: 'Review movie →' })).toBeVisible({ timeout: 60_000 })
  const built = await (await page.request.get(`/studio/movies/${movieId}`)).json()
  expect(built.build.status).toBe('succeeded')
  expect(built.shots.map((shot: { takes: unknown[] }) => shot.takes.length)).toEqual([1, 1, 1])
  expect(built.cut).toHaveLength(3)
  expect(built.cut[0].transition_seconds).toBe(0.5)
  await page.getByRole('button', { name: /^Arrange Arrival,/ }).click()
  await expect(page.getByLabel('Arrival preview', { exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Approve opening frame' })).toHaveCount(0)
  const preview = page.getByRole('button', { name: 'Preview take 1' })
  await preview.click()
  const dialog = page.getByRole('dialog', { name: 'Arrival take preview' })
  await expect(dialog).toBeVisible()
  await expect.poll(() => dialog.locator('video').evaluate((video: HTMLVideoElement) => video.currentTime)).toBeGreaterThan(0)
  await page.screenshot({ path: `${proof}/02-take-preview.png`, fullPage: true })
  await page.keyboard.press('Escape')
  await expect(dialog).not.toBeVisible()
  await expect(preview).toBeFocused()
  await preview.click()
  await expect(dialog).toBeVisible()
  await page.getByRole('button', { name: 'Close preview' }).click()
  await page.locator('#main-content').evaluate((element) => { element.scrollTop = 0 })
  await page.screenshot({ path: `${proof}/02-shots.png`, fullPage: true })
  await page.getByRole('link', { name: 'Review', exact: true }).click()
  await expect(page.getByLabel('Assembled movie preview')).toBeVisible()
  await expect(page.getByRole('group', { name: 'Cut clip 1' }).getByLabel('Transition to next clip')).toHaveValue('0.5')
  await page.getByRole('group', { name: 'Cut clip 1' }).getByLabel('Trim start (seconds)').fill('.5')
  await expect(page.getByLabel('Movie preview, clips in order')).toBeVisible()
  await page.getByRole('button', { name: 'Save and assemble' }).click()
  await expect(page.getByLabel('Assembled movie preview')).toBeVisible({ timeout: 20_000 })
  const score = testInfo.outputPath('score.wav')
  execFileSync('ffmpeg',['-v','error','-f','lavfi','-i','sine=frequency=440:duration=2',score])
  await page.getByLabel('Add music or narration').setInputFiles(score)
  await expect(page.getByLabel('Volume (dB)')).toHaveValue('-7')
  await page.getByLabel('Start in movie (seconds)').fill('1')
  await page.getByRole('button', { name: 'Save soundtrack' }).click()
  await expect(page.getByRole('button', { name: 'Save soundtrack' })).toBeDisabled()
  await page.getByRole('button', { name: 'Assemble movie' }).click()
  await expect(page.getByLabel('Assembled movie preview')).toBeVisible({ timeout: 20_000 })
  await page.screenshot({ path: `${proof}/03-cut.png`, fullPage: true })
  await page.getByRole('link', { name: 'Release & export', exact: true }).click()
  await page.getByRole('checkbox', { name: 'I reviewed the assembled movie and its continuity.' }).check()
  await page.getByRole('button', { name: 'Release movie', exact: true }).click()
  await expect(page.getByRole('link', { name: 'Download MP4' })).toBeVisible()
  await page.screenshot({ path: `${proof}/04-release.png`, fullPage: true })
  const downloadEvent = page.waitForEvent('download')
  await page.getByRole('link', { name: 'Download MP4' }).click()
  const download = await downloadEvent
  expect(download.suggestedFilename()).toMatch(/^movie-[a-zA-Z0-9-]+\.mp4$/)
  const movieFile = testInfo.outputPath(download.suggestedFilename())
  await download.saveAs(movieFile)
  expect(await download.failure()).toBeNull()
  expect((await stat(movieFile)).size).toBeGreaterThan(1000)
  const media = JSON.parse(execFileSync('ffprobe', ['-v', 'error', '-show_streams', '-show_format', '-of', 'json', movieFile], { encoding: 'utf8' }))
  expect(media.streams).toEqual(expect.arrayContaining([expect.objectContaining({ codec_type: 'video', codec_name: 'h264', width: 768, height: 432 }), expect.objectContaining({ codec_type: 'audio', codec_name: 'aac' })]))
  // Each 4-second request rounds up to 107 H3 frames; the local 16-fps
  // fixture is 4.5s. Three clips minus two 0.5s joins and the 0.5s trim.
  expect(Number(media.format.duration)).toBeCloseTo(12, 0)
  await testInfo.attach('released-movie', { path: movieFile, contentType: 'video/mp4' })
  await page.goto(`/movies/${movieId}/shots`)
  await page.setViewportSize({ width: 390, height: 844 })
  await expect(page.getByRole('heading', { name: 'Movie', exact: true })).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  expect((await page.getByRole('link', { name: 'Movie', exact: true }).boundingBox())?.height).toBeGreaterThan(30)
  await page.screenshot({ path: `${proof}/05-mobile.png`, fullPage: true })
})

test('arranges the movie with a right-side draggable sequence and preserves continuity', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1600, height: 1000 })
  const created = await page.request.post('/studio/movies', { data: { name: 'Sequence drag proof' } })
  expect(created.ok()).toBeTruthy()
  let movie = await created.json()
  const root = `/studio/movies/${movie.movie_id}`
  const saved = await page.request.put(`${root}/story`, { data: { expected_revision: movie.revision, brief: 'A coast, a forest, then a continuation.', style: 'Cinematic', batch_size: 2, shots: ['Coast', 'Forest', 'Beyond'].map((name, index) => ({ shot_id: `shot-${index}`, name, prompt: 'Slow camera movement', opening_prompt: name, duration_seconds: 2, continuation_of: index === 2 ? 'shot-0' : null })) } })
  expect(saved.ok()).toBeTruthy(); movie = await saved.json()
  await page.goto(`/movies/${movie.movie_id}/story`)
  const sequence = page.getByRole('complementary', { name: 'Movie sequence' })
  const firstEditor = page.locator('.movie-shot-definition').first()
  await expect(sequence).toBeVisible()
  expect((await firstEditor.boundingBox())!.y).toBeLessThan(250)
  const railBox = (await sequence.boundingBox())!
  const editorBox = (await firstEditor.boundingBox())!
  expect(railBox.x).toBeGreaterThan(editorBox.x + editorBox.width)
  async function drag(name: string, onto: string) {
    const source = (await page.getByRole('button', { name }).boundingBox())!
    const target = (await page.getByRole('button', { name: onto }).boundingBox())!
    await page.mouse.move(source.x + source.width / 2, source.y + source.height / 2)
    await page.mouse.down()
    await page.mouse.move(target.x + target.width / 2, target.y + target.height / 2, { steps: 12 })
    await page.mouse.up()
  }
  await drag('Arrange Forest, shot 2', 'Arrange Coast, shot 1')
  await expect(page.getByRole('button', { name: 'Arrange Forest, shot 1' })).toBeVisible()
  await expect(firstEditor.getByLabel('Shot name')).toHaveValue('Coast')
  await page.getByRole('button', { name: 'Arrange Forest, shot 1' }).click()
  await expect(firstEditor.getByLabel('Shot name')).toHaveValue('Forest')
  await expect(page.locator('.movie-shot-definition')).toHaveCount(1)
  await expect(page.getByRole('button', { name: 'Generate movie', exact: true })).toBeInViewport()
  await expect(firstEditor.getByLabel('Action & camera movement')).toBeInViewport()
  await expect(firstEditor.getByLabel('Continuity')).toBeInViewport()
  await expect(page.getByLabel('Clips per batch')).toBeInViewport()
  await drag('Arrange Beyond, shot 3', 'Arrange Coast, shot 2')
  await expect(page.getByRole('alert')).toHaveText('Keep continuation shots after the shots they continue.')
  await expect(page.getByRole('button', { name: 'Arrange Beyond, shot 3' })).toBeVisible()
  await page.getByRole('button', { name: 'Save draft', exact: true }).click()
  await expect(page.getByText('Movie saved.')).toBeVisible()
  await page.reload()
  await expect(page.getByRole('button', { name: 'Arrange Forest, shot 1' })).toBeVisible()
  await page.screenshot({ path: testInfo.outputPath('movie-sequence-desktop.png'), fullPage: true })
  await page.setViewportSize({ width: 390, height: 844 })
  await expect(sequence).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  await page.screenshot({ path: testInfo.outputPath('movie-sequence-mobile.png'), fullPage: true })
})

test('arranges review clips by dragging and previews them in sequence order', async ({ page }, testInfo) => {
  test.setTimeout(120_000)
  await page.setViewportSize({ width: 1440, height: 900 })
  const created = await page.request.post('/studio/movies', { data: { name: 'Review sequence proof' } })
  expect(created.ok()).toBeTruthy()
  let movie = await created.json()
  const root = `/studio/movies/${movie.movie_id}`
  const saved = await page.request.put(`${root}/story`, { data: { expected_revision: movie.revision, brief: 'A coast, a forest, then a continuation.', style: 'Cinematic', batch_size: 3, compute_target: 'shared', shots: ['Coast', 'Forest', 'Beyond'].map((name, index) => ({ shot_id: `shot-${index}`, name, prompt: `${name} slow camera movement`, opening_prompt: name, duration_seconds: 2, continuation_of: index === 2 ? 'shot-0' : null })) } })
  expect(saved.ok()).toBeTruthy(); movie = await saved.json()
  const image = await readFile(subjectPng)
  for (const shot of ['shot-0', 'shot-1']) {
    const target = await (await page.request.post(`${root}/uploads`, { data: { content_type: 'image/png' } })).json()
    expect((await page.request.put(target.upload_url, { headers: { ...target.upload_headers, 'Content-Type': 'image/png' }, data: image })).ok()).toBeTruthy()
    const opened = await page.request.post(`${root}/shots/${shot}/opening`, { data: { expected_revision: target.revision, asset_key: target.asset_key } })
    expect(opened.ok()).toBeTruthy(); movie = await opened.json()
  }
  // Shot by shot, as from each shot's own generate button: no build, so no saved cut.
  const generated = await page.request.post(`${root}/generate`, { data: { expected_revision: movie.revision, shot_ids: ['shot-0', 'shot-1', 'shot-2'], request_id: crypto.randomUUID(), select_when_ready: true } })
  expect(generated.ok()).toBeTruthy()
  await expect.poll(async () => {
    const current = await (await page.request.post(`${root}/reconcile`, { data: {} })).json()
    return current.shots.filter((shot: { selected_take_id: string | null }) => shot.selected_take_id).length
  }, { timeout: 60_000 }).toBe(3)
  movie = await (await page.request.get(root)).json()
  expect(movie.cut).toEqual([])
  const takeAsset = (index: number) => movie.shots[index].takes.find((take: { take_id: string }) => take.take_id === movie.shots[index].selected_take_id).asset_key
  await page.goto(`/movies/${movie.movie_id}/review`)
  const sequence = page.getByRole('complementary', { name: 'Movie sequence' })
  await expect(sequence.getByRole('button', { name: /^Arrange / })).toHaveCount(3)
  await expect(page.getByText('These are your selected takes in story order.', { exact: false })).toBeVisible()
  const preview = page.getByLabel('Movie preview, clips in order')
  await expect(preview).toBeVisible()
  async function drag(name: string, onto: string) {
    const source = (await page.getByRole('button', { name }).boundingBox())!
    const target = (await page.getByRole('button', { name: onto }).boundingBox())!
    await page.mouse.move(source.x + source.width / 2, source.y + source.height / 2)
    await page.mouse.down()
    await page.mouse.move(target.x + target.width / 2, target.y + target.height / 2, { steps: 12 })
    await page.mouse.up()
  }
  await drag('Arrange Beyond, clip 3', 'Arrange Coast, clip 1')
  await expect(page.getByRole('alert')).toHaveText('Keep continuation clips after the clips they continue.')
  await expect(page.getByRole('button', { name: 'Arrange Beyond, clip 3' })).toBeVisible()
  await drag('Arrange Forest, clip 2', 'Arrange Coast, clip 1')
  await expect(page.getByRole('button', { name: 'Arrange Forest, clip 1' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Arrange Coast, clip 2' })).toBeVisible()
  // The preview plays the clips back to back in the new order.
  const played: string[] = []
  await page.getByRole('button', { name: 'Play all from the start' }).click()
  await expect.poll(async () => {
    const now = await page.getByRole('status').filter({ hasText: /^Clip \d of 3/ }).textContent()
    if (now && played.at(-1) !== now) played.push(now)
    return played.length
  }, { timeout: 30_000, intervals: [100] }).toBe(3)
  // Lineless motion shots play silent, as the assembly will make them.
  expect(played).toEqual(['Clip 1 of 3 · Forest · silent', 'Clip 2 of 3 · Coast · silent', 'Clip 3 of 3 · Beyond · silent'])
  await expect(preview).toHaveAttribute('src', new RegExp(takeAsset(2).split('/').pop()!))
  await page.getByRole('button', { name: 'Arrange Coast, clip 2' }).click()
  await expect(page.getByText('Clip 2 of 3 · Coast · silent')).toBeVisible()
  expect(await preview.evaluate((video: HTMLVideoElement) => video.muted)).toBe(true)
  await expect(preview).toHaveAttribute('src', new RegExp(takeAsset(0).split('/').pop()!))
  await page.screenshot({ path: testInfo.outputPath('review-sequence-desktop.png'), fullPage: true })
  await page.getByRole('button', { name: 'Save and assemble' }).click()
  await expect(page.getByLabel('Assembled movie preview')).toBeVisible({ timeout: 20_000 })
  await page.reload()
  await expect(page.getByRole('button', { name: 'Arrange Forest, clip 1' })).toBeVisible()
  const cut = (await (await page.request.get(root)).json()).cut
  expect(cut.map((clip: { shot_id: string }) => clip.shot_id)).toEqual(['shot-1', 'shot-0', 'shot-2'])
  // Review started from the server's selected-takes cut: lineless motion shots play silent.
  expect(cut.every((clip: { mute?: boolean }) => clip.mute)).toBe(true)
  await page.setViewportSize({ width: 390, height: 844 })
  await expect(sequence).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  await page.screenshot({ path: testInfo.outputPath('review-sequence-mobile.png'), fullPage: true })
})
