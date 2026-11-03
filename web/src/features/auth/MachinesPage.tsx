import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Link } from 'react-router-dom'

import { ApiError, studioApi } from '../../api/client'
import type { EnrolledWorker, MachineFeature, PairingToken, PoolWorker } from '../../api/types'


/** What each verified machine configuration serves, as the machines list names it. */
const PROFILE_LABELS: Record<string, string> = {
  'h3-16gb-lightx8': 'H3 · W4A8 · 8 steps · Kitchen',
  'h3-24gb-standard': 'H3 · standard INT8 · 20 default steps',
  'flux2-klein-subject-12gb': 'Subject images · 12 GB · 1024² · 20 steps',
}

function stamp(value: string | null): string {
  return value ? new Date(value).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' }) : '—'
}

/** How recently a machine asked for work. The pool is the authority on whether a
 * machine is online; this is the last time the studio heard from it. */
function presence(worker: EnrolledWorker, live?: PoolWorker): string {
  if (worker.revoked_at) return `Removed ${stamp(worker.revoked_at)}`
  if (live) return live.status === 'busy' ? 'Generating' : live.status === 'online' ? 'Online' : live.status === 'draining' ? 'Draining' : `Offline · last heartbeat ${stamp(live.last_heartbeat_at)}`
  if (!worker.last_seen_at) return 'Not connected yet'
  const minutes = (Date.now() - new Date(worker.last_seen_at).getTime()) / 60000
  if (minutes < 3) return 'Working now'
  return `Last seen ${stamp(worker.last_seen_at)}`
}

/** Windows needs its own command: `curl … | sh` is not a thing in PowerShell, and
 * fails with "The term 'sh' is not recognized".
 *
 * This only chooses which command is shown first. It cannot be more than a
 * guess, and deliberately is not treated as more: the machine being attached is
 * usually *not* the one you are reading this on — you sit at a laptop and attach
 * the desktop with the good card. Both commands stay one click apart. */
function looksLikeWindows(): boolean {
  // userAgentData.platform is the supported replacement for navigator.platform,
  // which is deprecated and empty in some browsers. Neither is authoritative.
  const hinted = (navigator as { userAgentData?: { platform?: string } }).userAgentData
  if (hinted?.platform) return /win/i.test(hinted.platform)
  return /win/i.test(navigator.platform || navigator.userAgent)
}

function PairingPanel({ token, onDismiss }: { token: PairingToken; onDismiss: () => void }) {
  const [copied, setCopied] = useState(false)
  const [windows, setWindows] = useState(looksLikeWindows)
  const [profile, setProfile] = useState('')
  const [comfyUrl, setComfyUrl] = useState('http://127.0.0.1:8189')
  const validComfyUrl = /^http:\/\/(127\.0\.0\.1|localhost):[0-9]{1,5}$/.test(comfyUrl)
  const profileEnv = profile && validComfyUrl
    ? (windows ? `$env:MVS_WORKER_PROFILE="${profile}"; $env:OGWP_COMFY_URL="${comfyUrl}"; `
               : `MVS_WORKER_PROFILE='${profile}' OGWP_COMFY_URL='${comfyUrl}' `)
    : ''
  // One command, run once. It installs the worker, attaches this machine, and
  // starts it; the credential it receives never passes through a person, and
  // the machine renews it on its own from then on.
  const origin = window.location.origin
  const command = windows
    ? `${profileEnv}$env:MVS_CODE="${token.token}"; irm ${origin}/studio/attach.ps1 | iex`
    : `curl -fsSL ${origin}/studio/attach.sh | ${profileEnv}sh -s -- ${token.token}`
  async function copy() {
    try {
      await navigator.clipboard.writeText(command)
      setCopied(true)
    } catch {
      setCopied(false)
    }
  }
  return (
    <div className="machines__reveal" role="status">
      <h2>Run this once on the machine you want to add</h2>
      <p>
        It installs the worker and attaches the machine. Nothing to keep afterwards — it renews
        itself. This command works once and expires {stamp(token.expires_at)}.
      </p>
      <div className="machines__platforms" role="group" aria-label="Choose the machine's system">
        <button aria-pressed={!windows} className="button" onClick={() => { setWindows(false); setCopied(false) }} type="button">macOS or Linux</button>
        <button aria-pressed={windows} className="button" onClick={() => { setWindows(true); setCopied(false) }} type="button">Windows</button>
      </div>
      <label>Machine configuration
        <select aria-label="Machine configuration" value={profile} onChange={(event) => { setProfile(event.target.value); setCopied(false) }}>
          <option value="">CPU / manual configuration</option>
          <option value="h3-16gb-lightx8">H3 16GB — W4A8, LightX2V 8 steps</option>
          <option value="h3-24gb-standard">H3 24GB — standard INT8, 20 steps</option>
          <option value="flux2-klein-subject-12gb">Subjects 12GB — FLUX.2 Klein subject images only</option>
        </select>
      </label>
      {profile && <label>Local ComfyUI URL<input aria-label="Local ComfyUI URL" value={comfyUrl} onChange={(event) => { setComfyUrl(event.target.value); setCopied(false) }} /></label>}
      {profile && <p>The required models and runtime must already be installed. Setup verifies them before using your pairing code. A Linux/WSL agent can use Windows portable ComfyUI.</p>}
      {profile && !validComfyUrl ? <p role="alert">Use a loopback HTTP address, for example http://127.0.0.1:8189.</p> : <code className="machines__plaintext">{command}</code>}
      <p className="machines__hint">
        {windows
          ? 'Paste into PowerShell on the machine you are adding.'
          : 'Paste into a terminal on the machine you are adding.'}
      </p>
      <button className="button" disabled={!!profile && !validComfyUrl} onClick={copy} type="button">{copied ? 'Copied' : 'Copy command'}</button>
      <button className="button" onClick={onDismiss} type="button">Done</button>
    </div>
  )
}

const capitalised = (text: string) => text.charAt(0).toUpperCase() + text.slice(1)

/** What this machine can run and, for the rest, why not -- so a smaller card is never a
 * silent dead end. Motions it cannot run can still go to a Higgsfield key. */
function MachineFeatures({ features }: { features: MachineFeature[] }) {
  const can = features.filter((feature) => feature.available)
  const cannot = features.filter((feature) => !feature.available)
  return <ul aria-label="What this machine can run" className="machines__features">
    <li><strong>Can run:</strong> {can.length ? can.map((feature) => feature.label).join(', ') : 'nothing yet'}</li>
    {cannot.map((feature) => <li key={feature.key}><strong>{capitalised(feature.label)}:</strong> {feature.reason}{feature.key === 'motions' ? '. A Higgsfield key below runs motions without it.' : '.'}</li>)}
  </ul>
}

function MachineRow({ worker, live, onChanged }: { worker: EnrolledWorker; live?: PoolWorker; onChanged: () => void }) {
  const [confirming, setConfirming] = useState(false)
  const [renaming, setRenaming] = useState(false)
  const [label, setLabel] = useState(worker.label)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function run(action: () => Promise<unknown>, failure: string) {
    setBusy(true)
    setError(null)
    try {
      await action()
      onChanged()
    } catch {
      setError(failure)
    } finally {
      setBusy(false)
      setConfirming(false)
      setRenaming(false)
    }
  }

  return (
    <li className="machines__row">
      {renaming ? (
        <span className="machines__rename">
          <label htmlFor={`label-${worker.worker_id}`}>Name</label>
          <input id={`label-${worker.worker_id}`} onChange={(event) => setLabel(event.target.value)} value={label} />
          <button className="button" disabled={busy} onClick={() => run(() => studioApi.renameWorker(worker.worker_id, label), 'That name could not be saved. Try again.')} type="button">Save</button>
          <button className="button" disabled={busy} onClick={() => { setLabel(worker.label); setRenaming(false) }} type="button">Cancel</button>
        </span>
      ) : (
        <strong>{worker.label}</strong>
      )}
      <span>Added {stamp(worker.created_at)}</span>
      {live?.runtime_versions?.magic_profile && <span>{PROFILE_LABELS[live.runtime_versions.magic_profile] ?? live.runtime_versions.magic_profile}</span>}
      <span className={worker.revoked_at ? 'machines__revoked' : undefined}>{presence(worker, live)}</span>
      {!worker.revoked_at && live?.features && <MachineFeatures features={live.features} />}
      {!worker.revoked_at && !renaming && (
        confirming ? (
          <span className="machines__confirm">
            Remove {worker.label}? It stops taking work immediately.
            <button className="button" disabled={busy} onClick={() => run(() => studioApi.revokeWorker(worker.worker_id), 'The machine could not be removed. Try again.')} type="button">Yes, remove</button>
            <button className="button" disabled={busy} onClick={() => setConfirming(false)} type="button">Cancel</button>
          </span>
        ) : (
          <span className="machines__actions">
            <button aria-label={`Rename ${worker.label}`} className="button" onClick={() => setRenaming(true)} type="button">Rename</button>
            <button aria-label={`Remove ${worker.label}`} className="button" onClick={() => setConfirming(true)} type="button">Remove</button>
          </span>
        )
      )}
      {error && <p role="alert">{error}</p>}
    </li>
  )
}

/** Bring your own Higgsfield key: generation runs on, and is billed to, the person's own
 * Higgsfield account. The key is write-only here; only its last characters come back. */
function HiggsfieldKeyPanel() {
  const queryClient = useQueryClient()
  const providers = useQuery({ queryKey: ['provider-keys'], queryFn: studioApi.listProviderKeys })
  const status = providers.data?.find((item) => item.provider === 'higgsfield')
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  if (providers.isError || (status && !status.available)) return null
  async function run(action: () => Promise<unknown>) {
    setBusy(true)
    setError(null)
    try {
      await action()
      setDraft('')
      await queryClient.invalidateQueries({ queryKey: ['provider-keys'] })
      await queryClient.invalidateQueries({ queryKey: ['generation-config'] })
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : 'The key could not be saved. Try again.')
    } finally {
      setBusy(false)
    }
  }
  return <section aria-label="Bring your own Higgsfield key" className="machines__provider">
    <h2>No GPU? Bring your Higgsfield key</h2>
    <p>Motions you send to Higgsfield run on your own Higgsfield account, which bills you directly. Magic Studios adds no fee. Keys are stored encrypted and never shown again.</p>
    {status?.configured && <p>Saved key ending <strong>{status.key_hint}</strong>.</p>}
    <form onSubmit={(event) => { event.preventDefault(); if (draft.trim()) void run(() => studioApi.saveProviderKey('higgsfield', draft.trim())) }}>
      <label>{status?.configured ? 'Replace key' : 'Higgsfield API key'}
        <input aria-label="Higgsfield API key" autoComplete="off" placeholder="Paste your Higgsfield API key" type="password" value={draft} onChange={(event) => setDraft(event.target.value)} />
      </label>
      <button className="button button--primary" disabled={busy || !draft.trim()} type="submit">{busy ? 'Saving…' : 'Save key'}</button>
      {status?.configured && <button className="button" disabled={busy} type="button" onClick={() => void run(() => studioApi.deleteProviderKey('higgsfield'))}>Remove key</button>}
    </form>
    {error && <p role="alert">{error}</p>}
  </section>
}

/** `/account/machines`: the GPU machines this person has attached to the studio.
 *
 * The pairing code is shown once, in the panel that mints it. There is no route
 * to read a live code back, so "I lost it" and "it expired" have the same answer:
 * ask for another. */
export function MachinesPage() {
  const machines = useQuery({ queryKey: ['machines'], queryFn: studioApi.listWorkers, refetchInterval: 10000 })
  const cluster = useQuery({ queryKey: ['pool-workers'], queryFn: studioApi.poolWorkers, refetchInterval: 10000 })
  const isAdmin = cluster.data?.cluster_admin === true
  const [token, setToken] = useState<PairingToken | null>(null)
  const [minting, setMinting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function mint() {
    setMinting(true)
    setError(null)
    try {
      setToken(await studioApi.mintPairingToken())
    } catch {
      setError('A pairing code could not be created. Try again.')
    } finally {
      setMinting(false)
    }
  }

  return (
    <main className="route-state">
      <div className="route-state__card machines">
        <p className="eyebrow">Magic Studios</p>
        <h1>{isAdmin ? 'GPU cluster' : 'Your machines'}</h1>
        <p>
          {isAdmin ? 'Admins share this GPU cluster. Attached admin machines can run shared admin jobs; project ownership stays separate.' : 'Attach your GPU machine to run your work. Admin-owned machines also participate in the admin cluster.'}
        </p>
        <details className="machines__safety">
          <summary>What the worker can reach on your machine</summary>
          <ul>
            <li>It opens no ports and accepts no connections. It only makes outbound requests.</li>
            <li>It holds no storage or database credential — just its own, which identifies that machine and nothing else.</li>
            <li>A job it receives carries no code, no URLs and no file paths: only the settings for a generation it already knows how to run.</li>
            <li>Admin machines can run shared admin jobs. Other machines stay assigned to their owner.</li>
            <li>Remove it here at any time and it stops immediately.</li>
          </ul>
        </details>

        {token && <PairingPanel onDismiss={() => { setToken(null); machines.refetch() }} token={token} />}

        <button className="button button--primary" disabled={minting} onClick={mint} type="button">
          {minting ? 'Creating…' : 'Add a machine'}
        </button>
        {error && <p role="alert">{error}</p>}

        {machines.isPending ? <p role="status">Loading machines</p>
          : machines.isError ? <p role="alert">Your machines could not be loaded.</p>
            : machines.data.length === 0 ? <p>No machines yet. Everything runs on the shared pool.</p>
              : <ul className="machines__list">{machines.data.map((worker) => <MachineRow live={cluster.data?.workers.find((live) => live.worker_id === worker.worker_id)} key={worker.worker_id} onChanged={() => machines.refetch()} worker={worker} />)}</ul>}

        <HiggsfieldKeyPanel />

        <section aria-label="GPU cluster status">
          <h2>{isAdmin ? 'Full cluster' : 'Available GPU workers'}</h2>
          {cluster.isError && <p role="alert">GPU cluster status could not be loaded.</p>}
          {cluster.isPending && <p role="status">Loading GPU cluster</p>}
          <ul className="machines__list">{cluster.data?.workers.map((worker) => <li className="machines__row" key={worker.worker_id}>
            <strong>{machines.data?.find((machine) => machine.worker_id === worker.worker_id)?.label || worker.label || worker.worker_id}</strong>
            <span>Worker ID: {worker.worker_id}</span>
            {worker.capabilities.some((capability) => ['movie.ffmpeg.process.v1', 'movie.ffmpeg.process.v2'].includes(capability)) && <span>CPU movie processing available · stitching, trims, audio & frame extraction</span>}
            <span>{worker.status} · Last heartbeat {stamp(worker.last_heartbeat_at)}</span>
            {worker.gpus.length ? worker.gpus.map((gpu) => <span key={gpu.index}>{gpu.name} · {gpu.utilization_pct === null ? 'Utilization unavailable' : `${gpu.utilization_pct}% utilization`} · {gpu.memory_used_mb === null || gpu.memory_total_mb === null ? 'Memory unavailable' : `${(gpu.memory_used_mb / 1024).toFixed(1)} / ${(gpu.memory_total_mb / 1024).toFixed(1)} GB`}</span>) : <span>No GPU telemetry reported. A worker heartbeat does not confirm GPU readiness.</span>}
          </li>)}</ul>
        </section>
        <Link className="machines__back" to="/">Back to projects</Link>
      </div>
    </main>
  )
}
