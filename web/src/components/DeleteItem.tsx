import { useState } from 'react'

/** A permanent delete behind a typed confirmation: the button stays disabled until the
 * person types the item's exact name, so a stray click can never destroy work. */
export function DeleteItem({ kind, name, onDelete, onDeleted }: { kind: string; name: string; onDelete: () => Promise<void>; onDeleted: () => void }) {
  const [open, setOpen] = useState(false)
  const [typed, setTyped] = useState('')
  const [deleting, setDeleting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function remove() {
    setDeleting(true)
    setError(null)
    try {
      await onDelete()
      onDeleted()
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : `The ${kind} could not be deleted. Try again.`)
      setDeleting(false)
    }
  }

  if (!open) return <button aria-label={`Delete ${name}`} className="button delete-item__open" type="button" onClick={() => setOpen(true)}>Delete</button>
  return <div className="delete-item" role="group" aria-label={`Delete ${name}`}>
    <p>Delete {kind} <strong>{name}</strong> permanently? Every file, take, release and export in it is removed and cannot be recovered.</p>
    <label>Type the name to confirm<input autoComplete="off" value={typed} onChange={(event) => setTyped(event.target.value)} /></label>
    <div className="delete-item__actions">
      <button className="button delete-item__confirm" type="button" disabled={typed !== name || deleting} onClick={remove}>{deleting ? 'Deleting…' : 'Delete permanently'}</button>
      <button className="button" type="button" disabled={deleting} onClick={() => { setOpen(false); setTyped(''); setError(null) }}>Cancel</button>
    </div>
    {error && <p role="alert">{error}</p>}
  </div>
}
