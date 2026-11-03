/** The public tip endpoints (still named donations on the server). Kept apart from the studio client: a visitor who is not
 * signed in is expected here, so a 401 must never bounce them to sign-in. */
export interface DonationOptions {
  enabled: boolean
  test_mode: boolean
  currency: string
  preset_amounts_cents: number[]
  minimum_cents: number
  maximum_cents: number
}

export class DonationError extends Error {}

export async function fetchDonationOptions(): Promise<DonationOptions> {
  const response = await fetch('/studio/donations/options')
  if (!response.ok) throw new DonationError('Tips could not be loaded. Try again shortly.')
  return response.json() as Promise<DonationOptions>
}

/** Starts a Stripe-hosted checkout and returns its URL. `requestId` is Stripe's idempotency key
 * for this one request; the page mints a new one per submit, so a changed amount never collides
 * with an earlier attempt, and the button stays disabled while a request is in flight. */
export async function startDonation(amountCents: number, requestId: string): Promise<string> {
  const response = await fetch('/studio/donations/checkout', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ request_id: requestId, amount_cents: amountCents }),
  })
  let body: unknown
  try { body = await response.json() } catch { body = null }
  const record = body && typeof body === 'object' ? (body as Record<string, unknown>) : {}
  if (!response.ok) {
    throw new DonationError(typeof record.detail === 'string' ? record.detail : 'The tip could not be started. Try again shortly.')
  }
  return record.url as string
}

export function dollars(cents: number): string {
  return `$${(cents / 100).toLocaleString('en-US')}`
}
