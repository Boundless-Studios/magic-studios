import { expect, test } from '@playwright/test'

test('the sign-in page offers one door and starts the Auth0 hand-off', async ({ page }) => {
  await page.goto('/login?return_to=%2Fprojects%2Fp-1%2Fmotions')

  const signIn = page.getByRole('link', { name: 'Sign in' })
  await expect(signIn).toHaveAttribute('href', '/studio/auth/login?return_to=%2Fprojects%2Fp-1%2Fmotions')

  // Assert the hand-off request, never follow it: Auth0's hosted login is off-origin.
  await page.route('**/studio/auth/login*', (route) => route.fulfill({ status: 204, body: '' }))
  const started = page.waitForRequest((request) => new URL(request.url()).pathname === '/studio/auth/login')
  await signIn.click()
  expect(new URL((await started).url()).searchParams.get('return_to')).toBe('/projects/p-1/motions')
})

test('reports a sign-in that did not complete', async ({ page }) => {
  await page.goto('/login?error=auth0')
  await expect(page.getByRole('alert')).toHaveText("Sign-in didn't complete. Try again.")
})

test('reports an address that is not on the allowlist, with no way to ask for access', async ({ page }) => {
  await page.goto('/login?error=not_allowed')
  await expect(page.getByRole('alert')).toHaveText("This address isn't on the sign-in list.")
  await expect(page.getByRole('alert').getByRole('link')).toHaveCount(0)
  await expect(page.getByRole('link', { name: 'Create an account' })).toHaveAttribute('href', '/studio/auth/login?return_to=%2F&signup=true')
})
