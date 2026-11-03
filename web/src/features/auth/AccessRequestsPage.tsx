import { useQuery } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'

import { ApiError, studioApi } from '../../api/client'
import type { AccessRequest, AccessRequestPage } from '../../api/types'

const PAGE = 50

function day(value: string): string {
  return new Date(value).toLocaleDateString([], { dateStyle: 'medium' })
}

function lastPage(total: number): number {
  return Math.floor(Math.max(total - 1, 0) / PAGE) * PAGE
}

/** Previous / next over a server-paged list, with where we are in it. */
function Pager({ page, offset, onOffset }: { page: AccessRequestPage | undefined; offset: number; onOffset: (next: number) => void }) {
  if (!page || (page.total <= PAGE && offset === 0)) return null
  return <p className="access-requests__pager">
    {offset + 1}–{Math.min(offset + PAGE, page.total)} of {page.total}
    <button className="button" disabled={offset === 0} onClick={() => onOffset(Math.max(0, offset - PAGE))} type="button">Previous</button>
    <button className="button" disabled={offset + PAGE >= page.total} onClick={() => onOffset(offset + PAGE)} type="button">Next</button>
  </p>
}

/** `/account/access-requests`, for admins: requests and invitations from before both were
 * closed. They grant nothing, since the shared machines come only from the allowlist, so
 * the only action left is dismissing one. */
export function AccessRequestsPage() {
  const [waitingOffset, setWaitingOffset] = useState(0)
  const [invitedOffset, setInvitedOffset] = useState(0)
  const waitingPage = useQuery({ queryKey: ['access-requests', 'waiting', waitingOffset], queryFn: () => studioApi.listAccessRequests('waiting', PAGE, waitingOffset), retry: false })
  const invitedPage = useQuery({ queryKey: ['access-requests', 'invited', invitedOffset], queryFn: () => studioApi.listAccessRequests('invited', PAGE, invitedOffset), retry: false })
  // Dismissing the last row on a later page empties it: step back to the last page that has rows.
  useEffect(() => { if (waitingPage.data && !waitingPage.data.items.length && waitingOffset > 0) setWaitingOffset(lastPage(waitingPage.data.total)) }, [waitingPage.data, waitingOffset])
  useEffect(() => { if (invitedPage.data && !invitedPage.data.items.length && invitedOffset > 0) setInvitedOffset(lastPage(invitedPage.data.total)) }, [invitedPage.data, invitedOffset])
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)

  async function dismiss(email: string) {
    setBusy(email)
    setError(null)
    try {
      await studioApi.dismissAccessRequest(email)
      await Promise.all([waitingPage.refetch(), invitedPage.refetch()])
    } catch {
      setError(`${email} could not be dismissed. Try again.`)
    } finally {
      setBusy(null)
    }
  }

  const forbidden = waitingPage.error instanceof ApiError && waitingPage.error.status === 403
  const asked = waitingPage.data?.items ?? []
  const invited = invitedPage.data?.items ?? []
  const row = (item: AccessRequest) => (
    <li key={item.email}>
      <strong>{item.email}</strong>
      <span>
        {item.invited_at
          ? `Invited ${day(item.invited_at)}${item.invited_by ? ` by ${item.invited_by}` : ''}`
          : `Asked ${day(item.first_requested_at)}${item.request_count > 1 ? ` · ${item.request_count} times, last ${day(item.last_requested_at)}` : ''}`}
      </span>
      <button aria-label={`Dismiss ${item.email}`} className="button" disabled={busy !== null} onClick={() => void dismiss(item.email)} type="button">Dismiss</button>
    </li>
  )

  return <main className="route-state"><div className="route-state__card access-requests">
    <p className="eyebrow">Admin</p>
    <h1>Past access requests</h1>
    <p>Requests and invitations from before both were closed. They grant nothing: the shared machines are only for addresses on the allowlist (<code>ALLOWED_EMAILS</code>, <code>ALLOWED_EMAIL_DOMAINS</code>, <code>infra/allowed_emails.txt</code>) and admins. Dismissing a row deletes it and changes nobody&rsquo;s access. The rest are deleted a year after the last request or invitation.</p>
    {waitingPage.isPending && <p>Loading…</p>}
    {forbidden && <p role="alert">Only admins can see past access requests.</p>}
    {(waitingPage.isError || invitedPage.isError) && !forbidden && <p role="alert">Past access requests could not be loaded. Try again.</p>}
    {waitingPage.data && invitedPage.data && <>
      <h2>Requests</h2>
      {asked.length === 0 ? <p>No past requests.</p> : <ul className="access-requests__list">{asked.map(row)}</ul>}
      <Pager page={waitingPage.data} offset={waitingOffset} onOffset={setWaitingOffset} />
      <h2>Invitations</h2>
      {invited.length === 0 ? <p>No past invitations.</p> : <ul className="access-requests__list">{invited.map(row)}</ul>}
      <Pager page={invitedPage.data} offset={invitedOffset} onOffset={setInvitedOffset} />
    </>}
    {error && <p role="alert">{error}</p>}
    <Link className="button" to="/projects">Back to projects</Link>
  </div></main>
}
