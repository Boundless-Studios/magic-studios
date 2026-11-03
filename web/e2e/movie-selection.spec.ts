import { expect, test } from '@playwright/test'

test('legacy shots links open the unified movie editor', async ({ page }) => {
  const created = await page.request.post('/studio/movies', { data: { name: 'Unified movie' } })
  const movie = await created.json()
  await page.goto(`/movies/${movie.movie_id}/shots`)
  await expect(page).toHaveURL(new RegExp(`/movies/${movie.movie_id}/story$`))
  await expect(page.getByRole('heading', { name: 'Movie', exact: true })).toBeVisible()
  await expect(page.getByRole('link', { name: 'Shots', exact: true })).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Approve opening frame' })).toHaveCount(0)
})
