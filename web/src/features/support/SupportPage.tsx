import { useEffect, useState } from 'react'
import { Link, Navigate, useLocation, useSearchParams } from 'react-router-dom'

import { PublicFooter, PublicNav } from '../welcome/PublicNav'
import { DonationError, type DonationOptions, dollars, fetchDonationOptions, startDonation } from './donations'

const OTHER = 'other'

/** One id per submit; see `startDonation`. */
function newRequestId(): string {
  return crypto.randomUUID()
}

/** `/support`: a one-time tip through Stripe Checkout, signed in or not. Under Stripe's terms a
 * tip pays for a service already delivered (a donation needs a charitable purpose, which we
 * don't have), so the copy thanks people for a studio they have used and calls it a tip. */
export function SupportPage({ redirect = (url: string) => window.location.assign(url) }: { redirect?: (url: string) => void }) {
  const [searchParams] = useSearchParams()
  const cancelled = searchParams.get('cancelled') === '1'
  const [options, setOptions] = useState<DonationOptions | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [choice, setChoice] = useState<string>('2500')
  const [other, setOther] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [starting, setStarting] = useState(false)

  useEffect(() => {
    let current = true
    fetchDonationOptions().then(
      (loaded) => { if (current) setOptions(loaded) },
      (failure: unknown) => { if (current) setLoadError(failure instanceof Error ? failure.message : String(failure)) },
    )
    return () => { current = false }
  }, [])

  // Whole dollars only; anything else is NaN and never in range.
  const amountCents = choice !== OTHER ? Number(choice) : /^\d+$/.test(other.trim()) ? Number(other) * 100 : Number.NaN
  const inRange = options !== null && amountCents >= options.minimum_cents && amountCents <= options.maximum_cents

  async function tip(event: React.FormEvent) {
    event.preventDefault()
    if (!inRange || starting) return
    setStarting(true)
    setError(null)
    try {
      redirect(await startDonation(amountCents, newRequestId()))
    } catch (failure) {
      setError(failure instanceof DonationError ? failure.message : 'The tip could not be started. Try again shortly.')
      setStarting(false)
    }
  }

  return <>
    <PublicNav title="Support" />
    <main className="storage-policy support" id="main-content" tabIndex={-1}>
      <header><h1>Support Magic Studios</h1>
        <p>Magic Studios is free. If it has helped you, leaving a tip helps us cover hosting costs and keeps the platform free for everyone.</p></header>
      {cancelled && <p className="notice" role="status">You cancelled the tip. Nothing was charged.</p>}
      <aside className="storage-policy__notice"><strong>A tip is a voluntary gift</strong>
        <p>It buys nothing and changes nothing about your account. Tips are non-refundable and not tax-deductible: Boundless Games, LLC is a business, not a charity. Stripe securely processes the payment, so we never see your card details. See our <Link className="policy-link" to="/privacy">privacy policy</Link> and <Link className="policy-link" to="/terms">terms</Link>.</p></aside>
      <section aria-label="Tip">
        {loadError && <p role="alert">{loadError}</p>}
        {options === null && !loadError && <p>Loading…</p>}
        {options !== null && !options.enabled && <p>Tips aren’t open yet.</p>}
        {options !== null && options.enabled && <form className="support__form" onSubmit={tip}>
          {options.test_mode && <p className="notice">Test mode: no real payment is taken.</p>}
          <fieldset>
            <legend>Tip amount (US dollars)</legend>
            <div className="support__amounts">
              {options.preset_amounts_cents.map((cents) => (
                <label key={cents} className="support__amount">
                  <input type="radio" name="amount" value={cents} checked={choice === String(cents)} onChange={() => setChoice(String(cents))} />
                  {dollars(cents)}
                </label>
              ))}
              <label className="support__amount">
                <input type="radio" name="amount" value={OTHER} checked={choice === OTHER} onChange={() => setChoice(OTHER)} />
                Other
              </label>
            </div>
            {choice === OTHER && <label className="support__other">
              Amount in dollars, {dollars(options.minimum_cents)} to {dollars(options.maximum_cents)}
              <input type="number" inputMode="numeric" min={options.minimum_cents / 100} max={options.maximum_cents / 100} step={1} value={other} onChange={(event) => setOther(event.target.value)} />
            </label>}
          </fieldset>
          {error && <p role="alert">{error}</p>}
          <button className="button button--primary button--large" type="submit" disabled={!inRange || starting}>
            {starting ? 'Opening Stripe…' : inRange ? `Tip ${dollars(amountCents)}` : 'Leave a tip'}
          </button>
          <p className="storage-policy__updated">You pay on Stripe’s page, in US dollars, once. Nothing recurs.</p>
        </form>}
      </section>
    </main>
    <PublicFooter />
  </>
}

/** `/support/thanks`: where Stripe sends someone after they tip. */
export function SupportThanksPage() {
  return <>
    <PublicNav title="Thank you" />
    <main className="storage-policy support" id="main-content" tabIndex={-1}>
      <header><h1>Thank you</h1>
        <p>Your tip helps keep Magic Studios free. Stripe emails your receipt.</p></header>
      <section>
        <p><Link className="button button--large" to="/welcome">Back to Magic Studios</Link></p>
      </section>
    </main>
    <PublicFooter />
  </>
}

/** The page was `/donate` at launch; old links and checkouts started then land here. */
export function LegacyDonateRedirect({ to }: { to: string }) {
  const { search } = useLocation()
  return <Navigate replace to={to + search} />
}
