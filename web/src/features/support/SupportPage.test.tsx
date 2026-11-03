import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { LegacyDonateRedirect, SupportPage, SupportThanksPage } from './SupportPage'

const OPTIONS = { enabled: true, test_mode: true, currency: 'usd', preset_amounts_cents: [500, 1000, 2500, 5000], minimum_cents: 200, maximum_cents: 100000 }

function stubFetch(options: object, checkout: { status: number; body: object } = { status: 200, body: { url: 'https://checkout.stripe.com/c/pay/cs_test_1' } }) {
  const calls: { path: string; init?: RequestInit }[] = []
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    calls.push({ path, init })
    if (path === '/studio/donations/options') return new Response(JSON.stringify(options), { status: 200 })
    return new Response(JSON.stringify(checkout.body), { status: checkout.status })
  }))
  return calls
}

function renderSupport(path = '/support') {
  const redirect = vi.fn()
  render(<MemoryRouter initialEntries={[path]}><SupportPage redirect={redirect} /></MemoryRouter>)
  return redirect
}

afterEach(() => vi.unstubAllGlobals())

describe('SupportPage', () => {
  it('offers the preset amounts and sends the chosen one to Stripe Checkout', async () => {
    const user = userEvent.setup()
    const calls = stubFetch(OPTIONS)
    const redirect = renderSupport()

    const amounts = await screen.findByRole('group', { name: 'Tip amount (US dollars)' })
    expect(within(amounts).getAllByRole('radio').map((radio) => radio.parentElement?.textContent)).toEqual(['$5', '$10', '$25', '$50', 'Other'])
    expect(screen.getByText('Test mode: no real payment is taken.')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Support Magic Studios' })).toBeInTheDocument()
    expect(screen.getByText(/Tips are non-refundable and not tax-deductible: Boundless Games, LLC is a business, not a charity/)).toBeInTheDocument()
    // Stripe's terms reserve "donation" for charitable purposes.
    expect(document.body.textContent).not.toMatch(/donat/i)
    await user.click(screen.getByRole('radio', { name: '$10' }))
    await user.click(screen.getByRole('button', { name: 'Tip $10' }))

    const checkout = calls.find((call) => call.path === '/studio/donations/checkout')!
    expect(checkout.init?.method).toBe('POST')
    const body = JSON.parse(checkout.init?.body as string)
    expect(body.amount_cents).toBe(1000)
    expect(body.request_id).toMatch(/^[0-9a-f-]{36}$/)
    expect(redirect).toHaveBeenCalledWith('https://checkout.stripe.com/c/pay/cs_test_1')
  })

  it('takes a custom whole-dollar amount only within the published range', async () => {
    const user = userEvent.setup()
    const calls = stubFetch(OPTIONS)
    renderSupport()

    await user.click(await screen.findByRole('radio', { name: 'Other' }))
    const input = screen.getByRole('spinbutton', { name: /Amount in dollars, \$2 to \$1,000/ })
    for (const bad of ['1', '1.5', '1001']) {
      await user.clear(input)
      await user.type(input, bad)
      expect(screen.getByRole('button', { name: 'Leave a tip' })).toBeDisabled()
    }
    await user.clear(input)
    await user.type(input, '7')
    await user.click(screen.getByRole('button', { name: 'Tip $7' }))

    const checkout = calls.find((call) => call.path === '/studio/donations/checkout')!
    expect(JSON.parse(checkout.init?.body as string).amount_cents).toBe(700)
  })

  it('shows the server’s reason when a tip cannot start and lets the payer retry', async () => {
    const user = userEvent.setup()
    stubFetch(OPTIONS, { status: 503, body: { detail: 'Payments are unavailable right now. Try again shortly.' } })
    const redirect = renderSupport()

    await user.click(await screen.findByRole('button', { name: 'Tip $25' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('Payments are unavailable right now.')
    expect(redirect).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: 'Tip $25' })).toBeEnabled()
  })

  it('says tips are not open when the server has no Stripe configuration', async () => {
    stubFetch({ ...OPTIONS, enabled: false, test_mode: false })
    renderSupport()

    expect(await screen.findByText('Tips aren’t open yet.')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Tip/ })).not.toBeInTheDocument()
  })

  it('confirms nothing was charged after a cancelled checkout', async () => {
    stubFetch(OPTIONS)
    renderSupport('/support?cancelled=1')

    expect(screen.getByRole('status')).toHaveTextContent('You cancelled the tip. Nothing was charged.')
    await screen.findByRole('button', { name: 'Tip $25' })
  })

  it('sends the launch-time /donate addresses to /support, keeping the cancelled flag', async () => {
    stubFetch(OPTIONS)
    render(<MemoryRouter initialEntries={['/donate?cancelled=1']}><Routes>
      <Route path="/donate" element={<LegacyDonateRedirect to="/support" />} />
      <Route path="/support" element={<SupportPage redirect={vi.fn()} />} />
    </Routes></MemoryRouter>)

    expect(screen.getByRole('status')).toHaveTextContent('You cancelled the tip. Nothing was charged.')
    await screen.findByRole('button', { name: 'Tip $25' })
  })

  it('thanks the payer after Stripe sends them back', () => {
    render(<MemoryRouter><SupportThanksPage /></MemoryRouter>)
    expect(screen.getByRole('heading', { name: 'Thank you' })).toBeInTheDocument()
    expect(screen.getByRole('contentinfo')).toBeInTheDocument()
  })
})
