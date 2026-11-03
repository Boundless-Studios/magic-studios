import { useRef } from 'react'

/** Rebase an editing draft only when its persisted fields are unchanged.
 * Job progress and unrelated edits can advance a movie revision safely; edits
 * to this draft's fields must still fail CAS against its original revision.
 */
export function useDraftRevision<T>(current: T, revision: number) {
  const base = useRef({ json: JSON.stringify(current), revision })
  return {
    expectedRevision: JSON.stringify(current) === base.current.json ? revision : base.current.revision,
    matches: (value: T) => JSON.stringify(value) === base.current.json,
    committed: (value: T, nextRevision: number) => { base.current = { json: JSON.stringify(value), revision: nextRevision } },
  }
}
