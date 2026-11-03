import { useQuery } from '@tanstack/react-query'
import { useState, type FormEvent } from 'react'
import { Link } from 'react-router-dom'

import { studioApi } from '../../api/client'
import type { CreatedApiKey } from '../../api/types'

const API_DOCS_URL = '/studio/api.md'
const API_REFERENCE_URL = '/studio/reference'

function stamp(value: string | null): string {
  return value ? new Date(value).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' }) : '—'
}

function RevealPanel({ created, onDismiss }: { created: CreatedApiKey; onDismiss: () => void }) {
  const [copied, setCopied] = useState(false)
  async function copy() {
    try {
      await navigator.clipboard.writeText(created.key)
      setCopied(true)
    } catch {
      setCopied(false)
    }
  }
  return (
    <div className="api-keys__reveal" role="status">
      <h2>{created.name} is ready</h2>
      <p>Copy it now — this is the only time it is shown.</p>
      <code className="api-keys__plaintext">{created.key}</code>
      <button className="button" onClick={copy} type="button">{copied ? 'Copied' : 'Copy'}</button>
      <button className="button" onClick={onDismiss} type="button">Done</button>
    </div>
  )
}

function KeyRow({ apiKey, onRevoked }: { apiKey: { key_id: string; name: string; created_at: string; last_used_at: string | null; revoked_at: string | null }; onRevoked: () => void }) {
  const [confirming, setConfirming] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  async function revoke() {
    setBusy(true)
    setError(null)
    try {
      await studioApi.revokeApiKey(apiKey.key_id)
      onRevoked()
    } catch {
      setError('The key could not be revoked. Try again.')
    } finally {
      setBusy(false)
      setConfirming(false)
    }
  }
  return (
    <li className="api-keys__row">
      <strong>{apiKey.name}</strong>
      <span>Created {stamp(apiKey.created_at)}</span>
      <span>Last used {stamp(apiKey.last_used_at)}</span>
      {apiKey.revoked_at ? (
        <span className="api-keys__revoked">Revoked {stamp(apiKey.revoked_at)}</span>
      ) : confirming ? (
        <span className="api-keys__confirm">
          Revoke {apiKey.name}?
          <button className="button" disabled={busy} onClick={revoke} type="button">Yes, revoke</button>
          <button className="button" disabled={busy} onClick={() => setConfirming(false)} type="button">Cancel</button>
        </span>
      ) : (
        <button aria-label={`Revoke ${apiKey.name}`} className="button" onClick={() => setConfirming(true)} type="button">Revoke</button>
      )}
      {error && <p role="alert">{error}</p>}
    </li>
  )
}

/** `/account/api-keys`: the keys this actor can use as bearer tokens against the public API
 * (PR 117). The plaintext exists only in the reveal panel, never in the list. */
export function ApiKeysPage() {
  const keys = useQuery({ queryKey: ['api-keys'], queryFn: studioApi.listApiKeys })
  const [name, setName] = useState('')
  const [creating, setCreating] = useState(false)
  const [created, setCreated] = useState<CreatedApiKey | null>(null)
  const [error, setError] = useState<string | null>(null)

  async function create(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const trimmed = name.trim()
    if (!trimmed) return
    setCreating(true)
    setError(null)
    try {
      setCreated(await studioApi.createApiKey(trimmed))
      setName('')
      await keys.refetch()
    } catch {
      setError('The key could not be created. Try again.')
    } finally {
      setCreating(false)
    }
  }

  return (
    <main className="route-state">
      <div className="route-state__card api-keys">
        <p className="eyebrow">Magic Studios</p>
        <h1>API keys</h1>
        <p>Use a key as a bearer token to drive the studio from your own tools. <a href={API_DOCS_URL} rel="noreferrer" target="_blank">API documentation</a> · <a href={API_REFERENCE_URL} rel="noreferrer" target="_blank">API reference</a></p>

        {created && <RevealPanel created={created} onDismiss={() => setCreated(null)} />}

        <form className="api-keys__create" onSubmit={create}>
          <label htmlFor="api-key-name">Key name</label>
          <input id="api-key-name" onChange={(event) => setName(event.target.value)} required value={name} />
          <button className="button button--primary" disabled={creating || !name.trim()} type="submit">
            {creating ? 'Creating…' : 'Create key'}
          </button>
        </form>
        {error && <p role="alert">{error}</p>}

        {keys.isPending ? <p role="status">Loading keys</p>
          : keys.isError ? <p role="alert">The keys could not be loaded.</p>
            : keys.data.length === 0 ? <p>No keys yet.</p>
              : <ul className="api-keys__list">{keys.data.map((apiKey) => <KeyRow apiKey={apiKey} key={apiKey.key_id} onRevoked={() => keys.refetch()} />)}</ul>}

        <Link className="api-keys__back" to="/">Back to projects</Link>
      </div>
    </main>
  )
}
