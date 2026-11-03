/** Remember successful workspace visits per signed-in account on this browser. */
export function recentProjectKey(actorId: string) {
  return `studio:${actorId}:last-project`
}

export function readRecentProject(actorId: string): string | null {
  try { return window.localStorage.getItem(recentProjectKey(actorId)) } catch { return null }
}

export function rememberProject(actorId: string, path: string) {
  try { window.localStorage.setItem(recentProjectKey(actorId), path) } catch { /* Storage may be disabled. */ }
}

export function availableRecentProject(path: string | null, projectIds: string[], movieIds: string[]): string | null {
  if (!path) return null
  const match = /^\/(projects|movies)\/([^/]+)\/(subject|motions|exports|history|story|shots|cut|release)$/.exec(path)
  if (!match) return null
  return (match[1] === 'projects' ? projectIds : movieIds).includes(match[2]) ? path : null
}
