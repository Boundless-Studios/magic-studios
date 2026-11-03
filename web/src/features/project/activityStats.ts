/** Shared formatting for the always-visible activity stats line, used both by
 * `ProjectActivity`'s trigger (computed from the operations it already polls)
 * and the home list (from the backend's `ProjectSummary.activity`). */
export interface ActivityCounts { running: number; failed: number; completed: number; lastActivityAt: string | null }

export function activityCountsLabel({ running, failed, completed }: ActivityCounts): string {
  return `${running} running · ${failed} failed · ${completed} complete`
}

export function activityLastLabel({ lastActivityAt }: ActivityCounts): string {
  if (!lastActivityAt) return 'No activity yet'
  return `Last activity ${new Date(lastActivityAt).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}`
}
