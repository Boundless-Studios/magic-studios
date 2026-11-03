import { expect, test } from '@playwright/test'

test('admin cluster shows another owner’s GPU without exposing removal controls', async ({ page }, testInfo) => {
  await page.route('**/pool/workers', (route) => route.fulfill({ json: { cluster_admin: true, workers: [
    { worker_id: 'colleague-worker', status: 'online', last_heartbeat_at: new Date().toISOString(), capabilities: [], gpus: [{ index: 0, name: 'RTX 3090', utilization_pct: 0, memory_used_mb: 1024, memory_total_mb: 24576 }], busy_job_id: null, draining: false },
    { worker_id: 'offline-worker', status: 'offline', last_heartbeat_at: null, capabilities: [], gpus: [], busy_job_id: null, draining: false },
  ] } }))
  await page.goto('/account/machines')
  await expect(page.getByRole('heading', { name: 'Full cluster' })).toBeVisible()
  await expect(page.getByText('colleague-worker', { exact: true })).toBeVisible()
  await expect(page.getByText('offline-worker', { exact: true })).toBeVisible()
  await expect(page.getByText(/RTX 3090.*1.0 \/ 24.0 GB/)).toBeVisible()
  await expect(page.getByRole('button', { name: 'Remove colleague-worker' })).toHaveCount(0)
  await page.screenshot({ path: testInfo.outputPath('admin-cluster.png'), fullPage: true })
})

test('movie dashboard retains an offline colleague GPU and identifies missing telemetry', async ({ page }, testInfo) => {
  await page.route('**/pool/workers', (route) => route.fulfill({ json: { cluster_admin: true, workers: [
    { worker_id: 'justin-worker', label: 'Justin desktop', status: 'offline', last_heartbeat_at: new Date().toISOString(), capabilities: [], gpus: [{ index: 0, name: 'RTX 3090', utilization_pct: 2, memory_used_mb: 1024, memory_total_mb: 24576 }], busy_job_id: null, draining: false },
    { worker_id: 'unknown-worker', label: 'Unnamed machine', status: 'online', last_heartbeat_at: null, capabilities: [], gpus: [], busy_job_id: null, draining: false },
  ] } }))
  await page.goto('/projects/new')
  await page.getByRole('button', { name: 'Make a movie instead' }).click()
  await page.getByLabel('Project name').fill('Cluster dashboard proof')
  await page.getByRole('button', { name: 'Create project' }).click()
  const dashboard = page.getByRole('link', { name: 'GPU network and machines' })
  await expect(dashboard.getByText('Justin desktop')).toBeVisible()
  await expect(dashboard.getByText('RTX 3090')).toBeVisible()
  await expect(dashboard.getByText('Offline')).toBeVisible()
  await expect(dashboard.getByText('Connected · GPU not reported')).toBeVisible()
  await expect(dashboard.getByText('2%', { exact: true })).toHaveCount(0)
  await page.screenshot({ path: testInfo.outputPath('movie-cluster-dashboard.png'), fullPage: true })
})
