import { createContext, useContext } from 'react'

import type { StudioSession } from '../../api/auth'

/** The gate already fetched the session; the header reads it from here rather than
 * re-requesting `/studio/auth/session` just to label a button. */
export const StudioSessionContext = createContext<StudioSession | null>(null)

export function useStudioSession(): StudioSession | null {
  return useContext(StudioSessionContext)
}
