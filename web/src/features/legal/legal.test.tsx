import { render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it } from 'vitest'

import { PrivacyPolicy } from './PrivacyPolicy'
import { TermsOfService } from './TermsOfService'

function renderPage(page: React.ReactElement) {
  return render(<MemoryRouter>{page}</MemoryRouter>)
}

describe.each([
  ['privacy policy', <PrivacyPolicy key="privacy" />],
  ['terms of service', <TermsOfService key="terms" />],
])('the %s', (_name, page) => {
  it('names the operator, the contact address and the age rule', () => {
    renderPage(page)
    expect(screen.getAllByText(/Boundless Games, LLC/).length).toBeGreaterThan(0)
    for (const link of screen.getAllByRole('link', { name: 'admin@magicstudios.ai' })) {
      expect(link).toHaveAttribute('href', 'mailto:admin@magicstudios.ai')
    }
    expect(screen.getByText(/must be 18 or older/)).toBeInTheDocument()
  })
})

describe('Legal pages', () => {
  it('the terms forbid sexual content involving minors in the acceptable use list', () => {
    renderPage(<TermsOfService />)
    const section = screen.getByRole('heading', { name: 'Acceptable use' }).closest('section')!
    const rules = within(section).getAllByRole('listitem').map((item) => item.textContent)
    expect(rules[0]).toMatch(/sexual content involving minors/)
    expect(rules[0]).toMatch(/NCMEC/)
  })

  it('the privacy policy links to the terms it screens prompts against', () => {
    renderPage(<PrivacyPolicy />)
    expect(screen.getByRole('link', { name: 'terms of service' })).toHaveAttribute('href', '/terms')
    expect(screen.getByText(/do not use your content to train models/)).toBeInTheDocument()
  })

  it('the privacy policy names the cookieless visitor analytics and what they record', () => {
    renderPage(<PrivacyPolicy />)
    const section = screen.getByRole('heading', { name: 'Visitor analytics' }).closest('section')!
    expect(section).toHaveTextContent(/Cloudflare Web Analytics/)
    expect(section).toHaveTextContent(/sets no cookies and stores nothing in your browser/)
    expect(section).toHaveTextContent(/without its query string/)
    expect(section).toHaveTextContent(/link a visit to your account/)
  })

  it('the privacy policy says access requests are closed and what a past one keeps, and for how long', () => {
    renderPage(<PrivacyPolicy />)
    const section = screen.getByRole('heading', { name: 'Access requests' }).closest('section')!
    expect(section).toHaveTextContent(/no longer take requests for access or send invitations/)
    expect(section).toHaveTextContent(/It gives you no access\./)
    expect(section).toHaveTextContent(/a year after you last asked or were invited/)
    expect(section).not.toHaveTextContent(/Resend/)
  })
})

describe('Tip terms', () => {
  it('the terms say a tip is a voluntary, non-refundable payment that is not a donation', () => {
    renderPage(<TermsOfService />)
    const section = screen.getByRole('heading', { name: 'Tips' }).closest('section')!
    expect(section).toHaveTextContent(/voluntary, one-time payment/)
    expect(section).toHaveTextContent(/for the free service you have used/)
    expect(section).toHaveTextContent(/no further goods, services, features or priority/)
    expect(section).toHaveTextContent(/not refundable/)
    expect(section).toHaveTextContent(/not a charitable donation and is not tax-deductible/)
  })

  it('the privacy policy says what a tip shares with Stripe and what we keep', () => {
    renderPage(<PrivacyPolicy />)
    const section = screen.getByRole('heading', { name: 'Tips' }).closest('section')!
    expect(within(section).getByRole('link', { name: 'Stripe’s privacy policy' })).toHaveAttribute('href', 'https://stripe.com/privacy')
    expect(section).toHaveTextContent(/We never see your card/)
    expect(section).toHaveTextContent(/We don’t keep your name, email or address/)
  })
})
