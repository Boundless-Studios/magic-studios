import { expect, test } from '@playwright/test'

test('cost lives on the about page; old pricing and cost links land on it', async ({ page }) => {
  const apiRequests: string[] = []
  await page.route('**/studio/**', route => { apiRequests.push(route.request().url()); return route.fulfill({ status: 401 }) })
  await page.goto('/cost')
  await expect(page).toHaveURL(/\/about#cost$/)
  await page.goto('/pricing')
  await expect(page).toHaveURL(/\/about#cost$/)
  await expect(page.getByRole('heading', { name: 'About', exact: true })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Cost', exact: true })).toBeInViewport()
  await expect(page.getByText('Open to everyone', { exact: true })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Your own GPU: free' })).toBeVisible()
  await expect(page.getByText(/hosted generation/i)).toHaveCount(0)
  await expect(page.getByRole('link', { name: /usage/i })).toHaveCount(0)
  await page.reload()
  await expect(page.getByRole('heading', { name: 'Limits', exact: true })).toBeVisible()
  expect(apiRequests).toEqual([])
  await page.setViewportSize({ width: 375, height: 812 })
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
})


test('storage policy is public and linked from login', async ({ page }) => {
  await page.goto('/login')
  await page.getByRole('link', { name: 'Storage policy', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Storage policy', exact: true })).toBeVisible()
  await page.reload()
  await expect(page.getByRole('heading', { name: 'Storage allowance' })).toBeVisible()
})

test('a signed-out visit to the front page meets the welcome page, which leads to the guide', async ({ page }) => {
  await page.route('**/studio/**', route => route.fulfill({ status: 401 }))
  await page.goto('/')
  await expect(page).toHaveURL(/\/welcome$/)
  await expect(page.getByRole('heading', { level: 1, name: 'A free AI game asset studio for agents' })).toBeVisible()
  await expect(page.getByRole('main').getByRole('link', { name: 'Sign in' })).toHaveAttribute('href', '/studio/auth/login?return_to=%2F')
  await expect(page.getByRole('main').getByRole('link', { name: 'Sign up' })).toHaveAttribute('href', '/studio/auth/login?return_to=%2F&signup=true')
  await page.setViewportSize({ width: 375, height: 812 })
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.getByRole('link', { name: 'guide', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Guide', exact: true })).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.reload()
  await expect(page.getByRole('heading', { name: 'Attach your own machine' })).toBeVisible()
})

test('privacy policy and terms are public and fit a phone screen', async ({ page }) => {
  const apiRequests: string[] = []
  await page.route('**/studio/**', route => { apiRequests.push(route.request().url()); return route.fulfill({ status: 401 }) })
  await page.setViewportSize({ width: 375, height: 812 })
  await page.goto('/privacy')
  await expect(page.getByRole('heading', { name: 'Privacy policy', exact: true })).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.getByRole('link', { name: 'Terms', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Acceptable use' })).toBeVisible()
  await page.reload()
  await expect(page.getByRole('heading', { name: 'Terms of service', exact: true })).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  expect(apiRequests).toEqual([])
})

test('shared machines are allowlist-only: the public pages offer no way to ask for them', async ({ page }) => {
  const asked: string[] = []
  await page.route('**/studio/auth/session', route => route.fulfill({ status: 401 }))
  await page.route('**/studio/access-requests', route => { asked.push(route.request().method()); return route.fulfill({ status: 410 }) })
  await page.goto('/about')
  const notice = page.locator('aside').filter({ hasText: 'Open to everyone' })
  await expect(notice).toContainText('Anyone can sign up and use their own GPU or Higgsfield key.')
  await expect(notice).toContainText('The studio’s shared machines are for a small allowlist and aren’t available on request.')
  await expect(page.getByRole('link', { name: /invitation|request access/i })).toHaveCount(0)
  for (const path of ['/about', '/welcome', '/login', '/login?error=not_allowed']) {
    await page.goto(path)
    await expect(page.locator('a[href*="request-access"]')).toHaveCount(0)
  }
  await page.setViewportSize({ width: 375, height: 812 })
  await page.goto('/about')
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  expect(asked).toEqual([])
})

test('a signed-out visitor can reach the support page from the top bar and is sent to Stripe Checkout', async ({ page }) => {
  test.skip(process.env.VITE_HOSTED_SERVICE !== 'true', 'Tips are a hosted-service page; a self-hosted build has none (src/hostedService.ts).')
  const checkouts: unknown[] = []
  await page.route('**/studio/donations/options', route => route.fulfill({ json: { enabled: true, test_mode: true, currency: 'usd', preset_amounts_cents: [500, 1000, 2500, 5000], minimum_cents: 200, maximum_cents: 100000 } }))
  await page.route('**/studio/donations/checkout', route => { checkouts.push(route.request().postDataJSON()); return route.fulfill({ json: { url: 'https://checkout.stripe.com/c/pay/cs_test_e2e' } }) })
  await page.route('https://checkout.stripe.com/**', route => route.fulfill({ contentType: 'text/html', body: '<h1>Stripe test checkout</h1>' }))
  await page.goto('/about')
  await page.getByRole('navigation', { name: 'Magic Studios' }).getByRole('link', { name: 'Support', exact: true }).click()
  await expect(page).toHaveURL(/\/support$/)
  await expect(page.getByRole('heading', { name: 'Support Magic Studios', exact: true })).toBeVisible()
  await expect(page.getByText('Test mode: no real payment is taken.')).toBeVisible()
  await page.setViewportSize({ width: 375, height: 812 })
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await page.getByText('$10', { exact: true }).click()
  await page.getByRole('button', { name: 'Tip $10' }).click()
  await expect(page).toHaveURL('https://checkout.stripe.com/c/pay/cs_test_e2e')
  expect(checkouts).toEqual([expect.objectContaining({ amount_cents: 1000 })])
  // Checkouts started before the rename return to the old addresses.
  await page.goto('/donate/thanks')
  await expect(page).toHaveURL(/\/support\/thanks$/)
  await expect(page.getByRole('heading', { name: 'Thank you', exact: true })).toBeVisible()
  await page.goto('/donate?cancelled=1')
  await expect(page).toHaveURL(/\/support\?cancelled=1$/)
  await expect(page.getByRole('status')).toHaveText('You cancelled the tip. Nothing was charged.')
  // Nothing a visitor reads calls it a donation; Stripe's terms reserve that for charities.
  await expect(page.getByText(/donat/i)).toHaveCount(0)
})
