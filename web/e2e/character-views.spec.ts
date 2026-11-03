import { execFileSync } from 'node:child_process'
import { expect, test } from '@playwright/test'

import { subjectPng } from './subjectImage'

// All mutations in this journey are human UI actions. API reads below verify persisted
// identity; the deterministic local renderer proves workflow, not generated art quality.
test('human builds, revises and exports two supplied views without agent tools', async ({ page }, info) => {
  info.setTimeout(180_000)
  page.setDefaultTimeout(20_000)
  const reference = subjectPng
  await page.goto('/projects/new')
  await page.getByLabel('Choose character image').setInputFiles(reference)
  await page.getByLabel('Character / project name').fill('Human supplied-view acceptance')
  await page.getByRole('button', { name: 'Continue', exact: true }).click()
  await page.getByRole('button', { name: /Four-direction/ }).click()
  await page.getByRole('button', { name: 'Continue', exact: true }).click()
  await page.getByRole('button', { name: /North · away/ }).click()
  await page.getByRole('button', { name: 'Continue', exact: true }).click()
  await page.getByRole('button', { name: 'Clear all suggestions' }).click()
  await page.getByRole('button', { name: 'Continue', exact: true }).click()
  const creation = page.waitForResponse((r) => r.url().endsWith('/studio/projects') && r.request().method() === 'POST')
  await page.getByRole('button', { name: 'Create workspace', exact: true }).click()
  const project = await (await creation).json() as { project_id: string }
  const endpoint = `/studio/projects/${project.project_id}`
  async function readProject() {
    const response = await page.request.get(endpoint, { headers: { 'X-Studio-Setup-Version': '1' } })
    expect(response.ok()).toBe(true)
    return response.json()
  }
  const directionButton = (heading: 'N' | 'S') => page.getByRole('region', { name: 'Character directions' }).getByRole('button', { name: new RegExp(` · ${heading === 'N' ? '↑' : '↓'} ${heading} ·`) })
  await expect(page.getByRole('heading', { name: 'Build the animation set' })).toBeVisible()
  await page.getByRole('button', { name: 'Edit artwork & directions' }).click()
  const editor = page.getByRole('dialog', { name: 'Edit workspace setup' })
  await editor.getByRole('button', { name: 'Remove direction · East · right' }).click()
  await expect(editor.getByRole('button', { name: 'Remove direction · East · right' })).not.toBeVisible()
  await editor.getByRole('button', { name: 'Remove direction · West · left' }).click()
  await expect(editor.getByRole('button', { name: 'Remove direction · West · left' })).not.toBeVisible()
  // Keep labels compact while explicit heading metadata remains unchanged.
  const names = editor.getByLabel('Direction name'); await names.nth(0).fill('N'); await names.nth(1).fill('S')
  await editor.getByRole('button', { name: 'Review changes' }).click()
  await editor.getByRole('button', { name: 'Confirm exact changes' }).click()
  // The dialog closes once the save lands; the save then re-selects the direction,
  // which closes an open motion picker, so nothing else may start before that.
  await expect(editor).toBeHidden()
  async function addView(view: 'N' | 'S') {
    await directionButton(view).click()
    if (view === 'N') return
    await page.getByRole('button', { name: 'Edit artwork & directions' }).click()
    await page.getByLabel('Replace art').nth(1).setInputFiles(reference)
    await page.getByRole('button', { name: 'Review changes' }).click()
    await page.getByRole('button', { name: 'Confirm exact changes' }).click()
    await expect(editor).toBeHidden()
  }
  async function addMotion(name: string) {
    await page.getByRole('button', { name: 'Add motion', exact: true }).click()
    const added = page.waitForResponse((r) => r.url().endsWith('/motions') && r.request().method() === 'POST')
    await page.getByRole('button', { name: `Add ${name}`, exact: true }).click()
    expect((await added).ok()).toBe(true)
    await expect(page.getByRole('option', { name: new RegExp(`^${name} `) })).toHaveAttribute('aria-selected', 'true')
  }
  async function generateAndSelect(name: string) {
    const expand = page.getByRole('button', { name: 'Expand', exact: true })
    if (await expand.isVisible()) await expand.click()
    const previousCount = await page.getByRole('listitem', { name: /Take/ }).count()
    await page.getByRole('button', { name: new RegExp(`^(Generate|Regenerate) ${name}$`) }).click()
    await expect.poll(() => page.getByRole('listitem', { name: /Take/ }).count(), { timeout: 20_000 }).toBeGreaterThan(previousCount)
    const take = page.getByRole('listitem', { name: /Take/ }).last()
    await expect(take).toBeVisible({ timeout: 20_000 })
    // A regeneration keeps the earlier selection; wait for the new take before selecting.
    const use = take.getByRole('button', { name: 'Use animation', exact: true })
    if (await use.count()) {
      const selected = page.waitForResponse((response) => response.url().endsWith('/select') && response.request().method() === 'POST')
      await use.click(); expect((await selected).ok()).toBe(true)
    }
  }
  for (const view of ['N', 'S'] as const) {
    await addView(view)
    for (const motion of ['Idle', 'Walk', 'Attack']) {
      await addMotion(motion)
      await generateAndSelect(motion)
    }
  }
  const before = await readProject()
  expect(before.manifest.views).toHaveLength(2)
  expect(before.manifest.motions).toHaveLength(6)
  expect(new Set(before.manifest.motions.map((m: { motion_id: string }) => m.motion_id)).size).toBe(6)
  for (const m of before.manifest.motions) expect(m.selected_candidate_id).toBeTruthy()

  await directionButton('N').click()
  await page.getByRole('option', { name: /^Walk / }).click()
  const take = page.getByRole('listitem', { name: /Take/ }).first()
  await take.getByRole('button', { name: 'Edit frames', exact: true }).click()
  await expect(page.getByRole('region', { name: 'Walk frame editor' })).toBeVisible()
  await page.getByRole('button', { name: 'Exclude frame 2', exact: true }).click()
  await page.getByText('More frame controls', { exact: true }).click()
  await page.getByLabel('Frames per second', { exact: true }).fill('10')
  await page.getByLabel('Set framing and pivot', { exact: true }).check()
  await page.getByLabel('Target pivot X', { exact: true }).fill('120')
  await page.getByLabel('Target pivot Y', { exact: true }).fill('220')
  await page.getByRole('button', { name: 'Save revision', exact: true }).click()
  await expect(page.getByText(/Saved · 16 frames/)).toBeVisible()
  const framed = await readProject()
  const originalWalk = before.manifest.motions.find((m: { view_id: string; preset: string }) => m.view_id === 'N' && m.preset === 'walk')
  const framedWalk = framed.manifest.motions.find((m: { motion_id: string }) => m.motion_id === originalWalk.motion_id)
  expect(framedWalk.motion_id).toBe(originalWalk.motion_id)
  const framedTake = framedWalk.candidates.find((c: { candidate_id: string }) => c.candidate_id === framedWalk.selected_candidate_id)
  expect(framedTake.metadata.pivot_x).not.toBeNull()
  expect(framedTake.metadata.pivot_y).not.toBeNull()
  await page.screenshot({ path: info.outputPath('manual-frame-edit.png'), fullPage: true })

  const replacement = info.outputPath('replacement.png')
  execFileSync('ffmpeg', ['-y', '-v', 'error', '-i', reference, '-vf', 'hue=h=35', '-frames:v', '1', replacement])
  const currentBeforeReplace = await readProject()
  await page.getByRole('button', { name: 'Edit artwork & directions' }).click()
  await page.getByLabel('Replace art').first().setInputFiles(replacement)
  await page.getByRole('button', { name: 'Review changes' }).click()
  await page.getByRole('button', { name: 'Confirm exact changes' }).click()
  const after = await readProject()
  for (const old of currentBeforeReplace.manifest.motions) {
    const updated = after.manifest.motions.find((m: { motion_id: string }) => m.motion_id === old.motion_id)
    expect(updated.selected_candidate_id).toBe(old.selected_candidate_id)
    expect(updated.candidates).toHaveLength(old.candidates.length)
    if (old.view_id === 'N') expect(updated.spec_digest).not.toBe(old.spec_digest)
    else expect(updated.spec_digest).toBe(old.spec_digest)
  }
  await expect(directionButton('N')).toContainText('3 animations')
  await page.reload()
  await directionButton('N').click()
  await expect(page.getByRole('button', { name: 'Edit artwork & directions' })).toBeVisible()
  await page.screenshot({ path: info.outputPath('stale-reference.png'), fullPage: true })
  for (const motion of ['Idle', 'Walk', 'Attack']) {
    await page.getByRole('option', { name: new RegExp(`^${motion} `) }).click()
    await generateAndSelect(motion)
  }
  await page.getByRole('link', { name: 'Release & export', exact: true }).click()
  const assessment = page.waitForResponse((response) => response.url().endsWith('/quality/set') && response.request().method() === 'POST')
  await page.getByRole('button', { name: 'Run quality checks', exact: true }).click(); expect((await assessment).ok()).toBe(true)
  await expect(page.getByRole('banner').getByRole('status')).toHaveText('Saved')
  const approval = page.waitForResponse((response) => response.url().endsWith('/set/approve') && response.request().method() === 'POST')
  await page.getByRole('button', { name: 'Approve animation set', exact: true }).click()
  const approvalResponse = await approval
  expect(approvalResponse.ok(), `Approve status ${approvalResponse.status()}: ${await approvalResponse.text()}`).toBe(true)
  await expect(page.getByRole('button', { name: 'Animation set approved', exact: true })).toBeVisible()
  await page.getByLabel('Release name', { exact: true }).fill('human-two-view')
  await page.getByRole('button', { name: 'Create immutable release', exact: true }).click()
  await page.getByRole('button', { name: 'Request export', exact: true }).first().click()
  const downloadLink = page.getByRole('button', { name: /Download PNG \+ JSON package/ }).first()
  await expect(downloadLink).toBeVisible({ timeout: 15_000 })
  const [download] = await Promise.all([page.waitForEvent('download'), downloadLink.click()])
  const zip = info.outputPath('human-two-view.zip')
  await download.saveAs(zip)
  const exported = JSON.parse(execFileSync('python3', ['-c', 'import json,sys,zipfile; z=zipfile.ZipFile(sys.argv[1]); print(z.read("manifest.json").decode())', zip], { encoding: 'utf8' }))
  expect(exported.motions).toHaveLength(6)
  expect(new Set(exported.motions.map((m: { view_id: string }) => m.view_id))).toEqual(new Set(['N', 'S']))
  await page.screenshot({ path: info.outputPath('complete-export.png'), fullPage: true })
})
